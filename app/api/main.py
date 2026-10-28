"""FastAPI 主应用 —— 整个 Web 后端的"启动类 + Controller 层"。

请求处理流程
  POST /api/runs  →  建任务 + 提交线程池（立即返回 run_id，异步执行）
  GET  /api/runs/{id}/events  →  SSE 长连接，实时推送执行事件（工具调用/检索/回答）

路由清单：
- GET  /api/health                健康检查
- POST /api/runs                  创建分析任务
- GET  /api/runs/{run_id}/events  SSE 事件流
- GET  /api/runs/{run_id}         查询任务状态
- POST /api/runs/{run_id}/cancel  请求取消

核心架构：
1. 接口全部使用`async def`，保证主事件循环不被阻塞；
2. 同步阻塞的 Orchestrator 编排业务，提交到自定义线程池执行，等价于 Spring `@Async`；
3. 任务执行产生的事件写入独立队列，SSE 长连接从队列读取并向前端推送；
4. 全局单例`run_manager`统一管控任务生命周期、事件队列、取消信号；
5. 前后端解耦：创建任务接口立刻返回 run_id，执行进度靠 SSE 流式推送，状态可单独查询。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.api.events import RunEvent, RunState
from app.api.task_manager import RunManager, run_manager
from app.config import settings
from app.event_mapper import map_tracer_to_run_event


# --------------------------------------------------------------------------- Request / Response


class CreateRunRequest(BaseModel):
    """创建分析任务的请求体 DTO。

    BaseModel ≈ 一个带校验的 @RequestBody DTO：FastAPI 自动完成
    JSON → 对象的反序列化 + 字段校验（min_length/max_length 相当于 @Size），
    校验失败自动返回 422，不用手写 BindingResult 判断。
    """

    question: str = Field(..., min_length=1, max_length=5000, description="用户问题")
    repo_source: str = Field("local", description="local 或 github")
    repo_path: str | None = Field(None, description="本地仓库路径（local 模式）")
    github_url: str | None = Field(None, description="GitHub 仓库地址，如 owner/repo 或完整 URL")
    strategy: str = Field("auto", description="auto / direct / react / plan")
    thread_id: str | None = None
    user_id: str | None = None


class CreateRunResponse(BaseModel):
    """创建任务成功后的响应体（自动序列化为 JSON）。"""
    run_id: str
    status: str = "pending"
    repository_id: str


class RunStatusResponse(BaseModel):
    """任务状态查询返回DTO"""
    run_id: str
    status: str
    question: str
    repo_source: str
    repo_identifier: str
    repository_id: str
    strategy: str | None = None
    answer: str = ""
    error: str | None = None
    elapsed_ms: float | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    python_version: str
    repository_id: str
    github_mcp_available: bool


# --------------------------------------------------------------------------- App


# app 启动类实例：
# FastAPI 从这个对象出发注册路由、中间件，uvicorn 加载 "app.api.main:app" 即启动它
app = FastAPI(title="CodeInsight-Agent", version="1.0.0")

# CORS：允许前端 dev server (5173) 跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.web.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------- 启动事件 -----------------------------
# SpringBoot 的 @PostConstruct 等价：应用启动时自动建表（幂等，DROP 不会丢已有数据）
@app.on_event("startup")
def _on_startup():
    try:
        from app.db import init_tables
        init_tables()
    except Exception as exc:
        # 数据库连不上不能让整个应用挂掉（本地开发没 Postgres 时降级）
        print(f"[startup] WARNING: DB init failed (running without persistence): {exc}")


# ----------------------------- 注册子 Router -----------------------------
# history.py 里的 /api/history/* 路由挂到主 app 上，职责分离
from app.api.history import router as _history_router  # noqa: E402
app.include_router(_history_router)


# --------------------------------------------------------------------------- Helpers

#解析github仓库地址
def _parse_github_url(url: str) -> tuple[str, str]:
    """把 owner/repo 或 https://github.com/owner/repo → (owner, repo)。"""
    import re
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)", url)
    if m:
        return m.group(1), m.group(2).rstrip(".git")
    if "/" in url:
        parts = url.strip().split("/", 1)
        return parts[0], parts[1].rstrip(".git")
    raise ValueError(f"无法解析 GitHub 仓库地址: {url}")


def _serialize_tracer_events(tracer) -> str:
    """
    把 tracer.events 列表序列化成 JSON 字符串，存进 runs.events_json 列。

    关键：**用 `_map_tracer_to_run_event` 做映射**，跟实时 SSE 推送走同一条链路。
    这样历史查看拿到的 events 和实时看到的 events 格式完全一致，
    前端模板不用关心是实时流还是历史回放。

    为什么要截断？
      tracer.events 里可能有 read_file 的完整文件内容（几 KB 甚至几十 KB），
      直接存的话 runs 表会膨胀很快。所以每个事件的 data 字段超过 2KB 就截断。
    """
    import json as _json

    MAX_DATA_SIZE = 2048  # 每个事件 data 的最大字符数

    truncated = []
    for ev in tracer.events:
        event_type = ev.get("type", "")
        # 用映射函数把 Tracer raw type → RunEvent type
        mapped = map_tracer_to_run_event(event_type, ev)
        if mapped is None:
            continue  # span_* 等内部观测标签不存
        run_event_type, mapped_data = mapped

        # 截断大 payload
        data_copy = {}
        for k, v in mapped_data.items():
            s = _json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            if len(s) > MAX_DATA_SIZE:
                data_copy[k] = s[:MAX_DATA_SIZE] + f"...(truncated {len(s)} chars)"
            else:
                data_copy[k] = v

        truncated.append({
            "type": run_event_type,
            "data": data_copy,
        })

    return _json.dumps(truncated, ensure_ascii=False) if truncated else "[]"


# --------------------------------------------------------------------------- Tracer 实时事件 → RunEvent 映射



def _live_emit_tracer_event(bridge, event_type: str, tracer_data: dict) -> None:
    """Tracer.on_event 回调实现：实时把 tracer 事件推送到 SSE 队列。

    这就是"实时流式"的关键 —— 原来 orchestrator.run() 全部返回后才回放，
    现在每一次 tracer.add_event 都实时走这里 → bridge.emit → SSE 队列 → 前端立即收到。
    """
    mapped = map_tracer_to_run_event(event_type, tracer_data)
    if mapped is not None:
        run_event_type, run_data = mapped
        bridge.emit(run_event_type, run_data)


#根据入参 `repo_source` 选择仓库实现
def _build_provider(req: CreateRunRequest):
    """根据请求构建 RepositoryProvider。"""
    from app.repository import LocalRepositoryProvider

    if req.repo_source == "github":
        if not req.github_url:
            raise HTTPException(400, "github_url 是 github 模式的必填参数")
        from app.repository import GitHubRepositoryProvider
        owner, repo = _parse_github_url(req.github_url)
        return GitHubRepositoryProvider(owner=owner, repo=repo)

    # local 模式
    path = req.repo_path or settings.repo.default_local_path
    if not path:
        path = os.getcwd()
    rid = settings.repo.default_repository_id
    # 本地目录名作为 repository_id
    return LocalRepositoryProvider(path, repository_id=rid)


def _run_orchestrator_sync(run_id: str, question: str, provider, strategy: str,
                           thread_id: str, user_id: str, bridge) -> None:
    """
    在后台线程里同步跑编排层。

    这是所有真实业务的核心 —— 保持同步不改成 async，
    只在外层用 ThreadPoolExecutor 包装，避免重写 Orchestrator。
    """
    try:
        from app.models.llm import get_llm, get_summarization_llm
        from app.memory.postgres_resources import open_memory_resources
        from app.rag.code_vector_store import create_session_factory, CodeVectorStore
        from app.rag.retriever import CodeRetriever
        from app.agents.repository_agent import build_repository_tools, create_repository_agent_factory
        from app.observability.callback import TraceCallback
        from app.observability.token_budget import TokenBudget
        from app.observability.tracer import Tracer
        from app.orchestration.executor import Orchestrator
        from app.orchestration.limits import OrchestrationLimits
        from app.orchestration.models import Strategy
        from app.orchestration.verifier import AnalyzerRepoAccess, ProviderRepoAccess, Verifier
        from app.indexing import ensure_index

        # 先查任务状态：任务已经被取消，直接退出，不继续执行
        state = run_manager.get_state(run_id)
        if state is None or state.status == "cancelled":
            return

        # 推送两个初始化事件给前端
        bridge.emit("run_started", {
            "question": question[:200],
            "repo_source": state.repo_source,
            "repo_identifier": state.repo_identifier,
        })
        bridge.emit("repo_connected", provider.repo_info)


        # 策略映射：把前端字符串参数转成内部枚举Strategy
        _FORCE_MAP = {
            "direct": Strategy.DIRECT,
            "react": Strategy.REACT,
            "plan": Strategy.PLAN_EXECUTE,
            "plan_execute": Strategy.PLAN_EXECUTE,
        }
        #`forced=None` 代表 `auto`，交给编排器内部自动选路由策略
        forced = _FORCE_MAP.get(strategy) if strategy and strategy != "auto" else None

        # 内存/向量库资源上下文管理器，退出时自动释放资源
        with open_memory_resources() as (checkpointer, store, embeddings):
            SessionFactory = create_session_factory()
            session = SessionFactory()

            # GitHub特殊逻辑：Github API有请求限额，不走API拉文件，优先git浅克隆到本地
            if getattr(provider, "source", "local") == "github" and getattr(provider, "_clone_path", None) is None:
                try:
                    bridge.emit("indexing_completed", {"note": "cloning repository for deep analysis..."})
                    # 注意：这是同步线程里调用async函数，用asyncio.run开一个独立事件循环
                    # 克隆仓库到本地
                    asyncio.run(provider.prepare_deep_analysis())
                except Exception as e:
                    # 克隆失败则回退为 MCP 按需读取（不索引、无 RAG）
                    bridge.emit("indexing_completed", {"note": f"clone failed, fallback to on-demand MCP: {e}"})

            # 确保索引（每次都跑增量检查，按文件 hash 判断变更）
            try:
                def _index_progress(seq, total, file_path, indexed, skipped):
                    bridge.emit("indexing_progress", {
                        "seq": seq, "total": total, "file": file_path,
                        "indexed": indexed, "skipped": skipped,
                    })

                ensure_index(session, embeddings, provider, force=False, progress_callback=_index_progress)
                bridge.emit("indexing_completed", {"note": "索引同步完成（增量检查）"})
            except Exception as e:
                # 索引失败不致命 —— 远程按需模式下没有本地路径
                bridge.emit("indexing_completed", {"note": f"index skipped: {e}"})

            ## 初始化观测、限流、检索组件
            # 关键：on_event= 把 Tracer 接到 SSE 队列 —— 工具 start/end、RAG 检索、LLM 用量
            # 全部实时推送，不再等 orchestrator.run() 返回后回放。
            tracer = Tracer(on_event=lambda et, data: _live_emit_tracer_event(bridge, et, data))
            budget = TokenBudget(
                max_total_tokens=500000, max_input_tokens=60000, reserved_output_tokens=2000,
            )
            retriever = CodeRetriever(
                session=session, embeddings=embeddings,
                repository_id=provider.repository_id, tracer=tracer,
            )
            # 构造Agent可用工具集 + 记忆对象
            tools, memory = build_repository_tools(
                provider=provider, token_budget=budget, store=store,
                repository_id=provider.repository_id,
                retriever=retriever, user_id=user_id or "default-user",
                tracer=tracer,
            )

            ## 统一仓库访问抽象，给Verifier验证模块使用
            analyzer = getattr(provider, "_analyzer", None)
            repo_access = (
                AnalyzerRepoAccess(analyzer) if analyzer is not None
                else ProviderRepoAccess(provider)
            )
            verifier = Verifier(
                repo=repo_access,
                llm=get_llm(), limits=OrchestrationLimits(), tracer=tracer,
            )
            llm = get_llm()

            # 实例化编排核心 Orchestrator
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

            # --- 执行 Orchestrator.run() ---
            bridge.emit("routing_selected", {"strategy": strategy})
            state = run_manager.get_state(run_id)
            if state:
                state.status = "running"
                state.start_time = time.time()

            ## 执行前再检查一次取消标记
            if bridge.cancelled():
                bridge.emit("run_cancelled", {"reason": "before_run"})
                if state:
                    state.status = "cancelled"
                    state.end_time = time.time()
                return

            ## 【核心阻塞调用】运行Agent编排逻辑
            # tracer.on_event 回调已经在上面注入 → 工具/检索/LLM 事件实时推送到 SSE 队列，
            # orchestrator.run() 返回后只需要推送最终结果（验证结论、答案、汇总指标）
            #
            # thread_id 决定短期记忆（LangGraph checkpointer 对话续接）：
            #   - 前端传了就用前端的（支持跨 run 续接同一仓库的多轮对话）
            #   - 没传 → 按仓库标识自动生成稳定 thread_id，保证同一仓库多次提问自动续接上下文
            stable_thread_id = thread_id or f"repo-{provider.repository_id}"
            result = orchestrator.run(question, force_strategy=forced, thread_id=stable_thread_id)

            # 推送验证模块结果（编排层主动推送，不走 Tracer）
            if result.verification is not None:
                bridge.emit("verification_updated", {
                    "passed": result.verification.passed,
                    "issues": result.verification.issues[:5],
                    "missing_evidence": result.verification.missing_evidence[:5],
                })

            # 推送最终回答
            bridge.emit("answer_final", {"answer": result.answer})

            # 推送任务完成汇总指标
            record = result.record
            bridge.emit("run_completed", {
                "status": record.status,
                "duration_ms": record.duration_ms,
                "tool_call_count": record.tool_call_count,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "failure_reason": record.failure_reason,
            })

            # 更新 RunState（status 归一到 RunStatus 协议的五个值）
            state = run_manager.get_state(run_id)
            final_status = (
                "completed" if record.status in ("success", "partial") else "failed"
            )
            if state:
                state.status = final_status
                state.answer = result.answer
                state.strategy = str(result.strategy)
                state.end_time = time.time()

            # 写入 runs 表（成功结束 → update answer/status/duration/events_json）
            try:
                from app.db import SessionLocal as _DBSession
                from app.db.runs import RunDAO as _RunDAO
                with _DBSession() as _s:
                    _RunDAO(_s).update_status(
                        run_id=run_id,
                        status=final_status,
                        answer=result.answer,
                        strategy=str(result.strategy),
                        duration_ms=record.duration_ms,
                        events_json=_serialize_tracer_events(tracer),
                    )
            except Exception as _dbe:
                print(f"[_run] WARNING: DB update_status (success) failed: {_dbe}")

            session.close()

    except Exception as exc:
        # 全局兜底异常捕获：任何异常都不会让线程直接崩掉
        error_detail = f"{type(exc).__name__}: {exc}"
        bridge.emit("run_failed", {"error": error_detail})
        state = run_manager.get_state(run_id)
        if state:
            state.status = "failed"
            state.error = error_detail
            state.end_time = time.time()
        # 写入 runs 表（失败结束 → update status=failed + 已有事件快照）
        try:
            from app.db import SessionLocal as _DBSession2
            from app.db.runs import RunDAO as _RunDAO2
            with _DBSession2() as _s2:
                _RunDAO2(_s2).update_status(
                    run_id=run_id,
                    status="failed",
                    answer=error_detail,
                    duration_ms=0.0,
                    events_json=_serialize_tracer_events(tracer),
                )
        except Exception as _dbe2:
            print(f"[_run] WARNING: DB update_status (failed) failed: {_dbe2}")
        # 不 raise —— 后台线程需要优雅退出
        traceback.print_exc()


import time  # noqa: E402 —— 需要在函数内部用到


# --------------------------------------------------------------------------- Routes


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """健康检查。"""
    return HealthResponse(
        python_version=sys.version,
        repository_id=settings.repo.default_repository_id,
        github_mcp_available=True,  # mcp SDK 已安装
    )


# --- 运行时配置：允许前端动态更新，不用重启 ---

class UpdateConfigRequest(BaseModel):
    github_token: str | None = Field(None, description="GitHub Personal Access Token，留空表示清除")

class ConfigResponse(BaseModel):
    github_token_set: bool      # 是否已配置 token
    github_token_masked: str   # 脱敏显示（前6后4）


def _mask_token(token: str) -> str:
    if not token:
        return "（未配置）"
    if len(token) <= 10:
        return "****"
    return token[:6] + "****" + token[-4:]


def _update_env_file(key: str, value: str) -> None:
    """把 key=value 写进 .env 文件（已存在则覆盖，不存在则追加）。

    路径锚定到项目根目录（main.py 位于 app/api/ 下，parents[2] 即项目根），
    不能用 Path.cwd() —— 它取决于启动后端时的工作目录，
    工作目录不对就会把 .env 写歪到别处（比如 app/api/.env）。
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.get("/api/config", response_model=ConfigResponse)
async def get_config():
    """读取当前运行时配置（脱敏）。"""
    token = settings.github.token
    return ConfigResponse(
        github_token_set=bool(token),
        github_token_masked=_mask_token(token),
    )


@app.post("/api/config", response_model=ConfigResponse)
async def update_config(req: UpdateConfigRequest):
    """更新运行时配置（立即生效 + 写入 .env 持久化）。

    目前只支持动态更新 ``github_token``。下次创建 GitHub 仓库分析任务时，
    新建的 MCP 连接会自动使用新 token —— **不需要重启后端**。
    """
    new_token = (req.github_token or "").strip()
    # 1. 更新内存配置
    settings.github.token = new_token
    os.environ["GITHUB_TOKEN"] = new_token
    # 2. 持久化到 .env 文件
    _update_env_file("GITHUB_TOKEN", new_token)

    token_display = _mask_token(new_token) if new_token else "（已清除）"
    return ConfigResponse(
        github_token_set=bool(new_token),
        github_token_masked=token_display,
    )


@app.post("/api/runs", response_model=CreateRunResponse)
async def create_run(req: CreateRunRequest):
    """创建分析任务（在后台线程跑，不等待完成）。

    结果通过 SSE（/events）或轮询（GET /runs/{id}）获取 —— 前后端职责分离。
    """
    provider = _build_provider(req)

    state = run_manager.create_run(
        question=req.question,
        repo_source=req.repo_source,
        repo_identifier=req.github_url or req.repo_path or settings.repo.default_local_path,
        repository_id=provider.repository_id,
    )

    # 写入 runs 表（DB 连不上不影响主流程，降级为内存状态）
    try:
        from app.db import SessionLocal
        from app.db.runs import RunDAO
        _repo_id = req.github_url or req.repo_path or settings.repo.default_local_path or ""
        with SessionLocal() as _s:
            RunDAO(_s).create(
                run_id=state.run_id,
                repo_source=req.repo_source,
                repo_identifier=_repo_id,
                question=req.question,
                repository_id=provider.repository_id,
            )
    except Exception as _dbe:
        print(f"[create_run] WARNING: DB insert failed: {_dbe}")

    bridge = run_manager.bridge(state.run_id)
    if bridge is None:
        raise HTTPException(500, "任务初始化失败")

    # 提交任务进线程池，立刻返回run_id，不等分析结束
    future = run_manager._executor.submit(
        _run_orchestrator_sync,
        state.run_id, req.question, provider, req.strategy,
        req.thread_id, req.user_id, bridge,
    )
    run_manager._futures[state.run_id] = future

    return CreateRunResponse(
        run_id=state.run_id,
        status=state.status,
        repository_id=state.repository_id,
    )


@app.get("/api/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str):
    """查询任务状态。"""
    state = run_manager.get_state(run_id)
    if state is None:
        raise HTTPException(404, f"run {run_id} not found")
    return RunStatusResponse(
        run_id=state.run_id,
        status=state.status,
        question=state.question[:200],
        repo_source=state.repo_source,
        repo_identifier=state.repo_identifier,
        repository_id=state.repository_id,
        strategy=state.strategy,
        answer=state.answer,
        error=state.error,
        elapsed_ms=state.elapsed_ms(),
    )


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """请求取消任务。"""
    ok = run_manager.cancel_run(run_id)
    if not ok:
        raise HTTPException(404, f"run {run_id} not found")
    return {"run_id": run_id, "cancelled": True}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    """SSE 事件流 —— 订阅该任务的所有执行事件。
    """
    state = run_manager.get_state(run_id)
    if state is None:
        raise HTTPException(404, f"run {run_id} not found")

    q = run_manager.queue(run_id)

    async def event_generator():
        # 握手事件，告诉前端连接成功
        yield {"event": "connected", "data": json.dumps({"run_id": run_id}, ensure_ascii=False)}

        while True:
            # 用非阻塞轮询 + asyncio.sleep 避免阻塞事件循环
            try:
                #队列有数据：取出事件，转成 SSE 协议字典抛出；
                evt = q.get_nowait()
                # 必须用 dict 形式（event/data/id），直接 yield 字符串会被 sse-starlette 二次包装
                yield evt.to_sse_dict()
            except Exception:
                # 判断任务是否已经结束、队列排空；满足就跳出循环，关闭长连接；
                s = run_manager.get_state(run_id)
                if s and s.status in ("completed", "failed", "cancelled", "success", "partial") and q.empty():
                    break
                await asyncio.sleep(0.1)

        # 完成后保留 60 秒再清理，允许断线重连
        try:
            await asyncio.sleep(60)
        except Exception:
            pass
        run_manager.cleanup(run_id)

    return EventSourceResponse(
        event_generator(),
        media_type="text/event-stream",
    )


def run_server(host: str | None = None, port: int | None = None, **kwargs) -> None:
    """启动 uvicorn。"""
    import uvicorn

    h = host or settings.web.host
    p = port or settings.web.port
    uvicorn.run("app.api.main:app", host=h, port=p, reload=False, **kwargs)


if __name__ == "__main__":
    run_server()
