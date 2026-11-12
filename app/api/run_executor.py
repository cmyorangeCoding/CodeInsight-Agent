"""统一任务执行入口：首次执行与断点恢复共用同一条代码路径。

关键点：
- execution_thread_id 固定为 run-{run_id}，首次执行与恢复使用同一个 LangGraph 线程；
- 恢复时先看 checkpoint 的 `next`：非空 → `invoke(None)` 续跑；为空 → 图已完成，直接取最终状态；
- 没有 checkpoint 时才按首次执行处理（并明确记录日志，不伪装成恢复）；
- Plan 模式（step Agent 无 checkpointer）不支持 checkpoint 恢复，直接标记失败并说明原因；
- 执行期间后台线程定期续租；租约丢失后不再写入任何终态，避免覆盖新 Worker。
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import traceback
import uuid

from app.api.task_manager import execution_thread_id, run_manager
from app.db.runs import Run
from app.event_mapper import map_tracer_to_run_event


LEASE_SECONDS = float(os.getenv("RUN_LEASE_SECONDS", "90"))
RENEW_INTERVAL = max(5.0, LEASE_SECONDS / 3)


class RecoverUnsupported(RuntimeError):
    """当前任务/策略无法安全恢复。"""


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class LeaseRenewer(threading.Thread):
    """后台续租线程。续租失败说明执行权已被别的 Worker 接管。"""

    def __init__(self, run_id: str, worker_id: str):
        super().__init__(daemon=True, name=f"lease-{run_id}")
        self.run_id = run_id
        self.worker_id = worker_id
        self.lost = threading.Event()
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(RENEW_INTERVAL):
            try:
                from app.db import SessionLocal
                from app.db.runs import RunDAO

                with SessionLocal() as session:
                    ok = RunDAO(session).renew_lease(
                        self.run_id, self.worker_id, LEASE_SECONDS
                    )
            except Exception as exc:  # noqa: BLE001 - 数据库抖动不立即判定丢失
                print(f"[lease] WARNING: renew failed for {self.run_id}: {exc}")
                continue
            if not ok:
                print(f"[lease] WARNING: lease lost for {self.run_id} ({self.worker_id})")
                self.lost.set()
                return

    def stop(self) -> None:
        self._stop.set()


class CancellationProbe:
    """协作式取消探测：内存信号 + 数据库标记（带节流，避免每步都查库）。"""

    def __init__(self, run_id: str, event: threading.Event | None,
                 lost: threading.Event, interval: float = 2.0):
        self.run_id = run_id
        self.event = event
        self.lost = lost
        self.interval = interval
        self._last = 0.0
        self._cached = False

    def __call__(self) -> bool:
        if self.lost.is_set():
            return True
        if self.event is not None and self.event.is_set():
            return True
        now = time.monotonic()
        if now - self._last >= self.interval:
            self._last = now
            try:
                from app.db import SessionLocal
                from app.db.runs import RunDAO

                with SessionLocal() as session:
                    self._cached = RunDAO(session).is_cancel_requested(self.run_id)
            except Exception:  # noqa: BLE001
                self._cached = False
        return self._cached


def _parse_github_url(url: str) -> tuple[str, str]:
    import re

    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)", url)
    if match:
        return match.group(1), match.group(2).rstrip(".git")
    if "/" in url:
        owner, repo = url.strip().split("/", 1)
        return owner, repo.rstrip(".git")
    raise ValueError(f"无法解析 GitHub 仓库地址: {url}")


def build_provider_from_record(record: Run):
    """从数据库记录重建 RepositoryProvider（恢复时没有原始请求对象）。"""
    from app.config import settings

    if record.repo_source == "github":
        from app.repository import GitHubRepositoryProvider

        owner, repo = _parse_github_url(record.repo_identifier or "")
        return GitHubRepositoryProvider(owner=owner, repo=repo)

    from app.repository import LocalRepositoryProvider

    path = record.repo_identifier or settings.repo.default_local_path or os.getcwd()
    return LocalRepositoryProvider(
        path, repository_id=record.repository_id or settings.repo.default_repository_id
    )


def _load_run_record(run_id: str) -> Run | None:
    try:
        from app.db import SessionLocal
        from app.db.runs import RunDAO

        with SessionLocal() as session:
            return RunDAO(session).get(run_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[run] WARNING: load run {run_id} failed: {exc}")
        return None


def _db_finish(run_id: str, worker_id: str, status: str, **kwargs) -> bool | None:
    """写终态。返回 True/False 表示数据库判定结果；None 表示数据库不可用。"""
    try:
        from app.db import SessionLocal
        from app.db.runs import RunDAO

        with SessionLocal() as session:
            return RunDAO(session).finish(run_id, worker_id, status, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"[run] WARNING: DB finish failed for {run_id}: {exc}")
        return None


def _checkpoint_exists(checkpointer, thread_id: str) -> bool:
    if checkpointer is None:
        return False
    try:
        return checkpointer.get_tuple({"configurable": {"thread_id": thread_id}}) is not None
    except Exception:  # noqa: BLE001
        return False


def _load_conversation_messages(checkpointer, thread_id: str | None) -> list:
    """加载会话线程的历史消息，作为新任务的上下文种子（不写入执行线程）。"""
    if not thread_id or checkpointer is None:
        return []
    try:
        tup = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        if tup is None:
            return []
        messages = (tup.checkpoint or {}).get("channel_values", {}).get("messages", [])
        from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

        history = [
            m for m in messages
            if isinstance(m, BaseMessage) and isinstance(m, (HumanMessage, AIMessage))
        ]
        return history[-6:]
    except Exception:  # noqa: BLE001
        return []


def execute_run(run_id: str, *, resume: bool, provider=None,
                worker_id: str | None = None) -> None:
    """执行（或恢复）一个 Run。所有路径最终都会写入明确的终态。"""
    record = _load_run_record(run_id)
    if record is not None and record.status in ("completed", "failed", "cancelled"):
        print(f"[run] {run_id} already terminal ({record.status}), skip execution")
        return
    if record is not None and record.status == "cancelled":
        return
    if provider is None:
        if record is None:
            print(f"[run] ERROR: run {run_id} not found in DB, cannot recover")
            return
        provider = build_provider_from_record(record)

    thread_id = (record.thread_id if record and record.thread_id
                 else execution_thread_id(run_id))

    # 认领/接管：恢复路径由调度器先认领并传入 worker_id；首次执行在这里原子认领
    owns_lease = True
    if worker_id is None:
        worker_id = new_worker_id()
        try:
            from app.db import SessionLocal
            from app.db.runs import RunDAO

            with SessionLocal() as session:
                owns_lease = RunDAO(session).claim(
                    run_id, worker_id, LEASE_SECONDS, target_status="running"
                )
            if not owns_lease:
                print(f"[run] {run_id} already claimed by another worker, skip")
                return
        except Exception as exc:  # noqa: BLE001 - 无库降级为单进程执行
            print(f"[run] WARNING: claim failed, degrade to in-process: {exc}")
    else:
        try:
            from app.db import SessionLocal
            from app.db.runs import RunDAO

            with SessionLocal() as session:
                owns_lease = RunDAO(session).mark_running(
                    run_id, worker_id, LEASE_SECONDS
                )
            if not owns_lease:
                print(f"[run] {run_id} lease not owned by {worker_id}, skip")
                return
        except Exception as exc:  # noqa: BLE001
            print(f"[run] WARNING: mark_running failed: {exc}")

    # 认领后重新读取：attempt / worker_id / status 以数据库最新值为准
    latest = _load_run_record(run_id)
    if latest is not None:
        record = latest
    question = record.question if record else ""
    state = run_manager.attach_run(
        run_id=run_id,
        question=question,
        repo_source=record.repo_source if record else "local",
        repo_identifier=record.repo_identifier if record else "",
        repository_id=record.repository_id if record else provider.repository_id,
        thread_id=thread_id,
        status="recovering" if resume else "running",
    )
    state.status = "recovering" if resume else "running"
    state.thread_id = thread_id
    state.attempt = (record.attempt if record else 0) or 1
    state.worker_id = worker_id
    state.start_time = state.start_time or time.time()
    if record and record.strategy:
        state.strategy = record.strategy

    bridge = run_manager.bridge(run_id)
    publisher = run_manager.publisher(run_id)
    cancel_event = run_manager._cancel_events.get(run_id)
    renewer = LeaseRenewer(run_id, worker_id)
    renewer.start()
    probe = CancellationProbe(run_id, cancel_event, renewer.lost)
    tracer = None

    def _on_tracer_event(event_type: str, data: dict) -> None:
        mapped = map_tracer_to_run_event(event_type, data)
        if mapped is not None:
            run_event_type, run_data = mapped
            bridge.emit(run_event_type, run_data)
        # 路由决策一刻就落库：崩溃后恢复必须知道该用哪个 Agent 图
        if event_type == "route_decision":
            chosen = str(data.get("strategy") or "")
            if chosen:
                state.strategy = chosen
                try:
                    from app.db import SessionLocal
                    from app.db.runs import RunDAO

                    with SessionLocal() as session:
                        RunDAO(session).update_strategy(run_id, chosen)
                except Exception:  # noqa: BLE001
                    pass

    def _finish(status: str, answer: str | None = None,
                events_json: str | None = None, error: str | None = None,
                duration_ms: float | None = None) -> None:
        if renewer.lost.is_set():
            print(f"[run] {run_id} lease lost, refuse to overwrite state")
            return
        db_result = _db_finish(
            run_id, worker_id, status, answer=answer,
            strategy=state.strategy, events_json=events_json,
            last_error=error, duration_ms=duration_ms,
        )
        if db_result is False:
            # 条件更新未命中：数据库里已是终态（被取消或已被新 Worker 完成）→ 以库为准
            latest = _load_run_record(run_id)
            if latest is not None:
                run_manager.mark_terminal(run_id, latest.status, answer=latest.answer)
            return
        # True（写成功）或 None（数据库不可用）都维护本进程内存状态
        run_manager.mark_terminal(run_id, status, answer=answer, error=error)

    try:
        if probe():
            bridge.emit("run_cancelled", {"reason": "before_run"})
            _finish("cancelled", answer="（任务已取消）")
            return

        bridge.emit("run_started", {
            "question": question[:200],
            "repo_source": state.repo_source,
            "repo_identifier": state.repo_identifier,
            "resume": resume,
            "attempt": state.attempt,
        })
        bridge.emit("repo_connected", provider.repo_info)

        _FORCE_MAP = {
            "direct": "direct",
            "react": "react",
            "plan": "plan_execute",
            "plan_execute": "plan_execute",
        }
        # 恢复时必须强制使用「已落库的实际策略」，否则重新路由可能切到另一张图，
        # 在含 react checkpoint 的线程上开新一轮，等于假恢复。
        requested = (
            state.strategy if resume
            else (record.requested_strategy if record else None)
        )
        forced_name = _FORCE_MAP.get(requested or "", None)
        forced = None
        if forced_name:
            from app.orchestration.models import Strategy

            forced = Strategy(forced_name)

        from app.models.llm import get_llm, get_summarization_llm
        from app.memory.postgres_resources import open_memory_resources
        from app.rag.code_vector_store import create_session_factory
        from app.rag.retriever import CodeRetriever
        from app.agents.repository_agent import build_repository_tools, create_repository_agent_factory
        from app.observability.callback import TraceCallback
        from app.observability.token_budget import TokenBudget
        from app.observability.tracer import Tracer
        from app.orchestration.executor import Orchestrator
        from app.orchestration.limits import OrchestrationLimits
        from app.orchestration.verifier import AnalyzerRepoAccess, ProviderRepoAccess, Verifier
        from app.indexing import ensure_index
        from app.tools.idempotency import tool_execution_context

        with open_memory_resources() as (checkpointer, store, embeddings):
            resume_mode: str | None = None
            if resume:
                if not _checkpoint_exists(checkpointer, thread_id):
                    raise RecoverUnsupported(
                        f"thread {thread_id} 不存在可恢复 checkpoint"
                    )
                resume_mode = state.strategy
                if resume_mode not in ("direct", "react"):
                    raise RecoverUnsupported(
                        f"策略 {resume_mode!r} 不支持 checkpoint 恢复（仅 direct/react）"
                    )

            session = create_session_factory()()
            try:
                if getattr(provider, "source", "local") == "github" and getattr(provider, "_clone_path", None) is None:
                    try:
                        bridge.emit("indexing_completed", {"note": "cloning repository for deep analysis..."})
                        asyncio.run(provider.prepare_deep_analysis())
                    except Exception as exc:  # noqa: BLE001
                        bridge.emit("indexing_completed", {"note": f"clone failed, fallback to on-demand MCP: {exc}"})

                try:
                    def _index_progress(seq, total, file_path, indexed, skipped):
                        bridge.emit("indexing_progress", {
                            "seq": seq, "total": total, "file": file_path,
                            "indexed": indexed, "skipped": skipped,
                        })

                    ensure_index(session, embeddings, provider, force=False,
                                 progress_callback=_index_progress)
                    bridge.emit("indexing_completed", {"note": "索引同步完成（增量检查）"})
                except Exception as exc:  # noqa: BLE001
                    bridge.emit("indexing_completed", {"note": f"index skipped: {exc}"})

                tracer = Tracer(on_event=_on_tracer_event)
                budget = TokenBudget(
                    max_total_tokens=500000, max_input_tokens=60000, reserved_output_tokens=2000,
                )
                retriever = CodeRetriever(
                    session=session, embeddings=embeddings,
                    repository_id=provider.repository_id, tracer=tracer,
                )
                tools, memory = build_repository_tools(
                    provider=provider, token_budget=budget, store=store,
                    repository_id=provider.repository_id,
                    retriever=retriever,
                    user_id=(record.user_id if record and record.user_id else "default-user"),
                    tracer=tracer,
                )
                analyzer = getattr(provider, "_analyzer", None)
                repo_access = (
                    AnalyzerRepoAccess(analyzer) if analyzer is not None
                    else ProviderRepoAccess(provider)
                )
                llm = get_llm()
                verifier = Verifier(
                    repo=repo_access, llm=llm,
                    limits=OrchestrationLimits(), tracer=tracer,
                )
                orchestrator = Orchestrator(
                    llm=llm, routing_llm=get_summarization_llm(),
                    agent_factory=create_repository_agent_factory(
                        llm, tools, budget, tracer, memory, checkpointer,
                    ),
                    budget=budget, tracer=tracer,
                    limits=OrchestrationLimits(),
                    verifier=verifier,
                    callbacks=[TraceCallback(tracer=tracer, token_budget=budget)],
                    memory=memory,
                )

                bridge.emit("routing_selected", {"strategy": str(state.strategy or requested or "auto")})
                conversation_thread = record.requested_thread_id if record else None
                initial_messages = []
                if not resume and conversation_thread and conversation_thread != thread_id:
                    initial_messages = _load_conversation_messages(checkpointer, conversation_thread)

                with tool_execution_context(run_id):
                    result = orchestrator.run(
                        question,
                        force_strategy=forced,
                        thread_id=thread_id,
                        resume_mode=resume_mode,
                        initial_messages=initial_messages,
                        cancel_check=probe,
                    )

                if result.verification is not None:
                    bridge.emit("verification_updated", {
                        "passed": result.verification.passed,
                        "issues": result.verification.issues[:5],
                        "missing_evidence": result.verification.missing_evidence[:5],
                    })
                bridge.emit("answer_final", {"answer": result.answer})

                rec = result.record
                bridge.emit("run_completed", {
                    "status": rec.status,
                    "duration_ms": rec.duration_ms,
                    "tool_call_count": rec.tool_call_count,
                    "input_tokens": rec.input_tokens,
                    "output_tokens": rec.output_tokens,
                    "failure_reason": rec.failure_reason,
                })

                state.strategy = str(result.strategy)
                final_status = "completed" if rec.status in ("success", "partial") else "failed"
                if probe():
                    final_status = "cancelled"

                snapshot = None
                if tracer is not None:
                    from app.api.publisher import serialize_tracer_events

                    snapshot = publisher.snapshot_json(serialize_tracer_events(tracer))
                _finish(
                    final_status,
                    answer=result.answer,
                    events_json=snapshot,
                    error=rec.failure_reason,
                    duration_ms=rec.duration_ms,
                )
            finally:
                session.close()

    except RecoverUnsupported as exc:
        reason = f"{exc}"
        print(f"[run] {run_id} not recoverable: {reason}")
        bridge.emit("run_failed", {"error": reason})
        _finish("failed", answer=f"（任务无法安全恢复：{reason}）", error=reason)
    except Exception as exc:  # noqa: BLE001
        error_detail = f"{type(exc).__name__}: {exc}"
        bridge.emit("run_failed", {"error": error_detail})
        snapshot = None
        if tracer is not None:
            try:
                from app.api.publisher import serialize_tracer_events

                snapshot = publisher.snapshot_json(serialize_tracer_events(tracer))
            except Exception:  # noqa: BLE001
                snapshot = None
        _finish("failed", answer=error_detail, events_json=snapshot, error=error_detail)
        traceback.print_exc()
    finally:
        renewer.stop()
