"""任务管理器 + RunEvent 发布桥接 + Orchestrator 异步执行。

核心设计：
- 每个 Run 有独立的内存状态、事件队列、取消信号、Future；
- 事件经 RunEventPublisher 先写 run_events 再入队，支持崩溃后补发；
- 数据库保存 thread_id / worker_id / lease_until / attempt，
  由 run_executor + RecoveryScheduler 负责跨进程的认领与恢复；
- 内存只做「当前进程的加速层」，不再作为恢复依据。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from app.api.events import RunEvent, RunState
from app.api.publisher import RunEventPublisher


def task_thread_id(task_id: str) -> str:
    """Task → LangGraph 线程 ID 的稳定映射。

    一个任务（多轮会话）只对应一条线程，同一任务的所有轮次共用它，
    这样多轮上下文与断点恢复都以「任务」为单位。
    迁移前的历史任务 task_id == run_id，直接复用原有线程，避免断链。
    """
    return task_id if task_id.startswith(("task-", "run-")) else f"task-{task_id}"


class _TracerBridge:
    """把 Tracer / Orchestrator 产生的事件同步到 RunEvent 发布器。

    保留原有 bridge 接口（emit / cancelled），内部把事件交给 RunEventPublisher：
    先持久化、再推 SSE 队列。
    """

    def __init__(self, run_id: str, publisher: RunEventPublisher,
                 cancel_event: threading.Event):
        self.run_id = run_id
        self._publisher = publisher
        self._cancel = cancel_event

    def emit(self, event_type, data: dict | None = None) -> None:
        self._publisher.emit(event_type, data)

    def cancelled(self) -> bool:
        return self._cancel.is_set()


class RunManager:
    """全局任务管理器。"""

    def __init__(self, max_concurrent: int = 5, task_timeout: float = 300.0):
        self._states: dict[str, RunState] = {}
        self._queues: dict[str, queue.Queue] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self._bridges: dict[str, _TracerBridge] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._task_timeout = task_timeout

    # --- 生命周期 ---
    def create_run(self, question: str, repo_source: str, repo_identifier: str,
                   repository_id: str, run_id: str | None = None,
                   thread_id: str | None = None, strategy: str | None = None,
                   user_id: str | None = None,
                   status: str = "pending") -> RunState:
        """新建/接管一条任务的运行时状态（恢复时 run_id 由 DB 提供）。"""
        state = RunState(
            run_id=run_id or f"run-{uuid.uuid4().hex[:12]}",
            question=question,
            repo_source=repo_source,
            repo_identifier=repo_identifier,
            repository_id=repository_id,
            thread_id=thread_id or "",
            attempt=0,
            status=status,
            start_time=time.time(),
        )

        event_queue: queue.Queue = queue.Queue()
        cancel_event = threading.Event()
        publisher = RunEventPublisher(state.run_id, event_queue)
        bridge = _TracerBridge(state.run_id, publisher, cancel_event)

        with self._lock:
            self._states[state.run_id] = state
            self._queues[state.run_id] = event_queue
            self._cancel_events[state.run_id] = cancel_event
            self._bridges[state.run_id] = bridge
        return state

    def attach_run(self, run_id: str, **kwargs) -> RunState:
        """恢复场景：内存里没有该 Run 时，从 DB 记录重建运行时状态（幂等）。"""
        existing = self.get_state(run_id)
        if existing is not None:
            return existing
        return self.create_run(run_id=run_id, **kwargs)

    def get_state(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._states.get(run_id)

    def mark_terminal(self, run_id: str, status: str, *,
                      answer: str | None = None,
                      strategy: str | None = None,
                      error: str | None = None) -> None:
        state = self.get_state(run_id)
        if state is None:
            return
        state.status = status  # type: ignore[assignment]
        if answer is not None:
            state.answer = answer
        if strategy is not None:
            state.strategy = strategy
        if error is not None:
            state.error = error
        state.end_time = time.time()
        if status == "cancelled":
            event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()

    def cancel_run(self, run_id: str) -> bool:
        """取消：内存信号 + 数据库状态双写，终态任务不会被改写。"""
        state = self.get_state(run_id)
        if state is not None and state.status in ("completed", "failed", "cancelled"):
            return False
        event = self._cancel_events.get(run_id)

        db_checked = False
        db_ok = False
        try:
            from app.db import SessionLocal
            from app.db.runs import RunDAO

            with SessionLocal() as session:
                db_ok = RunDAO(session).request_cancel(run_id)
            db_checked = True
        except Exception as exc:  # noqa: BLE001 - 无库时降级为仅内存取消
            print(f"[cancel] WARNING: DB cancel failed for {run_id}: {exc}")

        # 数据库可达且拒绝取消 → 任务已进入终态，不能覆盖
        if db_checked and not db_ok:
            return False
        if state is None and not db_checked:
            return False
        if event is not None:
            event.set()
        bridge = self._bridges.get(run_id)
        if bridge is not None:
            bridge.emit("run_cancelled", {"reason": "user_requested"})
        self.mark_terminal(run_id, "cancelled")
        return True

    def bridge(self, run_id: str) -> _TracerBridge | None:
        return self._bridges.get(run_id)

    def publisher(self, run_id: str) -> RunEventPublisher | None:
        bridge = self._bridges.get(run_id)
        return bridge._publisher if bridge else None

    # --- 事件流 ---
    def queue(self, run_id: str) -> queue.Queue | None:
        return self._queues.get(run_id)

    # --- 线程池 ---
    def submit(self, run_id: str, fn: Callable[..., Any], *args, **kwargs) -> Future:
        future = self._executor.submit(fn, *args, **kwargs)
        with self._lock:
            self._futures[run_id] = future
        return future

    def is_inflight(self, run_id: str) -> bool:
        future = self._futures.get(run_id)
        return future is not None and not future.done()

    # --- 清理 ---
    def cleanup(self, run_id: str) -> None:
        with self._lock:
            self._states.pop(run_id, None)
            self._queues.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            self._futures.pop(run_id, None)
            self._bridges.pop(run_id, None)


# 全局单例：整个 FastAPI 进程共享这一个管理器。
run_manager = RunManager()
