"""RecoveryScheduler：服务启动与运行期间自动接管崩溃遗留的 Run。

安全规则：
1. 数据库递归认领（条件 UPDATE），同一 Run 同时只有一个 Worker；
2. 租约未过期的 running 任务视为「原 Worker 可能还活着」，绝不动它；
3. 有 checkpoint + 已知 direct/react 策略 → 用原 thread_id 续跑；
4. 没有 checkpoint 但从未产生任何事件（attempt 仅首次）→ 允许安全地首次执行；
5. 其余不可恢复情况（旧版本遗留、Plan 模式、无 checkpoint 但有执行痕迹）
   → 标记 failed 并写入原因，不伪装成恢复成功；
6. 进程内 _inflight / run_manager.is_inflight 双重去重，重复触发调度器不会重复提交。
"""
from __future__ import annotations

import os
import threading
import time
from functools import partial

from app.api.run_executor import LEASE_SECONDS, execute_run, new_worker_id
from app.api.task_manager import run_manager, execution_thread_id

RECOVERY_ENABLED = os.getenv("RECOVERY_ENABLED", "true").lower() != "false"
RECOVERY_INTERVAL = float(os.getenv("RECOVERY_INTERVAL_SECONDS", "30"))
RECOVERY_BATCH = int(os.getenv("RECOVERY_BATCH", "10"))


def _checkpoint_exists_in_db(thread_id: str) -> bool:
    """直接查 LangGraph checkpoints 表，避免为存在性检查初始化 embeddings。"""
    if not thread_id:
        return False
    try:
        from sqlalchemy import text

        from app.db import engine

        if engine is None:
            return False
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM checkpoints WHERE thread_id = :tid LIMIT 1"),
                {"tid": thread_id},
            ).first()
            return row is not None
    except Exception as exc:  # noqa: BLE001 - 表不存在/无库都按不存在处理
        print(f"[recovery] WARNING: checkpoint probe failed for {thread_id}: {exc}")
        return False


def _event_count(run_id: str) -> int:
    try:
        from app.db import SessionLocal
        from app.db.run_events import RunEventDAO

        with SessionLocal() as session:
            return RunEventDAO(session).count(run_id)
    except Exception:  # noqa: BLE001
        return 0


class RecoveryScheduler:
    def __init__(self, interval: float = RECOVERY_INTERVAL,
                 batch: int = RECOVERY_BATCH,
                 session_factory=None,
                 checkpoint_probe=None,
                 event_counter=None,
                 submitter=None):
        self.interval = interval
        self.batch = batch
        self.worker_id = new_worker_id()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        # 以下依赖可注入，便于用 SQLite 做故障注入测试
        self._session_factory = session_factory
        self._checkpoint_probe = checkpoint_probe or _checkpoint_exists_in_db
        self._event_counter = event_counter or _event_count
        self._submitter = submitter or self._default_submit

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from app.db import SessionLocal

        return SessionLocal()

    def _default_submit(self, run_id: str, resume: bool) -> None:
        fn = partial(execute_run, run_id, resume=resume, worker_id=self.worker_id)
        run_manager.submit(run_id, fn)

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if not RECOVERY_ENABLED:
            print("[recovery] disabled by RECOVERY_ENABLED=false")
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="recovery-scheduler")
        self._thread.start()
        print(f"[recovery] scheduler started (interval={self.interval}s, "
              f"worker={self.worker_id})")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 启动先跑一轮：尽快接管崩溃遗留任务
        while not self._stop.is_set():
            try:
                summary = self.run_once()
                if summary.get("recovered") or summary.get("failed"):
                    print(f"[recovery] pass: {summary}")
            except Exception as exc:  # noqa: BLE001
                print(f"[recovery] WARNING: pass failed: {exc}")
            self._stop.wait(self.interval)

    # ------------------------------------------------------------------ 单轮扫描
    def run_once(self) -> dict:
        summary = {"scanned": 0, "recovered": 0, "failed": 0, "skipped": 0}
        try:
            from app.db.runs import RunDAO

            with self._session() as session:
                candidates = RunDAO(session).list_stale(limit=self.batch)
                rows = [
                    {
                        "run_id": r.run_id,
                        "thread_id": r.thread_id,
                        "status": r.status,
                        "strategy": r.strategy,
                        "attempt": r.attempt or 0,
                    }
                    for r in candidates
                ]
        except Exception as exc:  # noqa: BLE001 - 无数据库无法恢复
            print(f"[recovery] WARNING: cannot scan runs: {exc}")
            return summary

        summary["scanned"] = len(rows)
        for row in rows:
            run_id = row["run_id"]
            with self._lock:
                if run_id in self._inflight or run_manager.is_inflight(run_id):
                    summary["skipped"] += 1
                    continue
                self._inflight.add(run_id)
            try:
                outcome = self._recover_one(run_id, row)
                summary[outcome] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[recovery] WARNING: recover {run_id} failed: {exc}")
                summary["skipped"] += 1
            finally:
                with self._lock:
                    self._inflight.discard(run_id)
        return summary

    def _recover_one(self, run_id: str, row: dict) -> str:
        from app.db.runs import RunDAO

        # 1) 原子认领（recovering）；租约未过期 / 终态都会失败
        try:
            with self._session() as session:
                claimed = RunDAO(session).claim(
                    run_id, self.worker_id, LEASE_SECONDS,
                    target_status="recovering",
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[recovery] WARNING: claim {run_id} failed: {exc}")
            return "skipped"
        if not claimed:
            return "skipped"

        thread_id = row["thread_id"] or execution_thread_id(run_id)
        strategy = row["strategy"]

        # 2) 缺 thread_id 的旧版本任务：没有稳定 checkpoint 关联，不能恢复
        if not row["thread_id"]:
            return self._fail(run_id, "历史任务缺少 thread_id，无法关联 checkpoint")

        has_ckpt = self._checkpoint_probe(thread_id)

        # 3) 有 checkpoint：只支持 direct/react（Plan 的 step agent 无 checkpointer）
        if has_ckpt:
            if strategy not in ("direct", "react"):
                return self._fail(
                    run_id,
                    f"checkpoint 存在但执行策略为 {strategy!r}，"
                    "仅 direct/react 支持 checkpoint 恢复",
                )
            return self._submit(run_id, resume=True)

        # 4) 无 checkpoint：只有「从未产生任何事件」才允许安全地首次执行
        if self._event_counter(run_id) == 0:
            return self._submit(run_id, resume=False)

        return self._fail(run_id, "checkpoint 不存在且任务已有执行事件，无法安全恢复")

    def _submit(self, run_id: str, resume: bool) -> str:
        self._submitter(run_id, resume)
        print(f"[recovery] {'resumed' if resume else 'fresh-restarted'} {run_id}")
        return "recovered"

    def _fail(self, run_id: str, reason: str) -> str:
        from app.db.runs import RunDAO

        try:
            with self._session() as session:
                RunDAO(session).fail_unrecoverable(run_id, self.worker_id, reason)
        except Exception as exc:  # noqa: BLE001
            print(f"[recovery] WARNING: mark failed {run_id} failed: {exc}")
        print(f"[recovery] cannot recover {run_id}: {reason}")
        return "failed"


recovery_scheduler = RecoveryScheduler()
