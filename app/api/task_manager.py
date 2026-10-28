"""任务管理器 + Tracer 事件桥接 + Orchestrator 异步执行。

核心设计：
- TracerCallback 把 LangChain 事件同步写入线程安全队列
- Orchestrator 在 ThreadPoolExecutor 里跑（保持同步调用）
- 独立的事件队列桥接线程把 TracerCallback 的事件 + 编排层结果 → RunEvent
- SSE endpoint 直接消费 RunEvent 队列
- 支持取消：Event + 轮询检查
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.api.events import RunEvent, RunState, RunStatus, EventType


class _TracerBridge:
    """把 Tracer / TraceCallback 产生的事件同步到 RunEvent 队列。

    不是 LangChain Callback —— 是在 Orchestrator.run() 调用前后，
    用 RunState + queue.Queue 手动桥接。
    """

    def __init__(self, run_id: str, event_queue: queue.Queue, cancel_event: threading.Event):
        self.run_id = run_id            # 绑定所属任务id
        self._queue = event_queue       # 这个任务专属的事件队列，SSE 去读这个队列
        self._cancel = cancel_event     # 线程取消信号
        self._seq = 0                   # 事件自增序列号，对应 RunEvent.sequence
        self._lock = threading.Lock()   # 序列号自增的线程锁，防止并发emit时序号重复

    def emit(self, event_type: EventType, data: dict | None = None) -> None:
        """同步发出一个 RunEvent（线程安全）。
        编排器、回调都调用这个方法向外推送事件。
        """
        with self._lock:
            seq = self._seq
            self._seq += 1
        # 构造标准RunEvent，丢进队列
        evt = RunEvent(
            run_id=self.run_id,
            sequence=seq,
            type=event_type,
            timestamp=time.time(),
            data=data or {},
        )
        self._queue.put_nowait(evt)

    def cancelled(self) -> bool:
        """供编排器内部轮询，判断任务是否被用户取消"""
        return self._cancel.is_set()


class RunManager:
    """全局任务管理器。

    每个任务由一个 RunState + 一个事件队列 + 一个取消信号 + 一个 Future 组成。
    所有访问通过 run_id 索引。
    """

    def __init__(self, max_concurrent: int = 5, task_timeout: float = 300.0):
        # 存储各个任务的状态对象 RunState
        self._states: dict[str, RunState] = {}
        # 每个任务单独一个队列，隔离事件流，避免A任务事件串到B任务SSE
        self._queues: dict[str, queue.Queue] = {}
        # 线程取消信号：threading.Event，用来通知工作线程停止
        self._cancel_events: dict[str, threading.Event] = {}
        # ThreadPoolExecutor 提交任务返回的 Future，可拿到执行结果/异常
        self._futures: dict[str, Future] = {}
        # 每个任务绑定一个 _TracerBridge 实例
        self._bridges: dict[str, _TracerBridge] = {}
        # 多线程读写上面这些字典的全局锁
        self._lock = threading.Lock()
        # 同步编排器的执行线程池，控制最大并发任务数
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        # 任务超时阈值（秒），上层逻辑自行判断超时
        self._task_timeout = task_timeout


    # --- 生命周期 ---
    def create_run(self, question: str, repo_source: str, repo_identifier: str,
                   repository_id: str) -> RunState:
        """新建一条任务：初始化RunState、队列、取消信号、bridge，存入管理器"""
        state = RunState(
            question=question,
            repo_source=repo_source,
            repo_identifier=repo_identifier,
            repository_id=repository_id,
            start_time=time.time(),
        )
        self._queues[state.run_id] = queue.Queue()
        self._cancel_events[state.run_id] = threading.Event()
        self._bridges[state.run_id] = _TracerBridge(
            state.run_id, self._queues[state.run_id], self._cancel_events[state.run_id]
        )
        with self._lock:
            self._states[state.run_id] = state
        return state

    def get_state(self, run_id: str) -> RunState | None:
        """查询任务状态，加锁保证线程安全"""
        with self._lock:
            return self._states.get(run_id)

    def cancel_run(self, run_id: str) -> bool:
        """用户触发取消：设置Event标记，修改状态，推送 run_cancelled 事件"""
        if run_id not in self._cancel_events:
            return False
        # 把取消信号置为True，工作线程里 bridge.cancelled() 就会检测到
        self._cancel_events[run_id].set()
        state = self.get_state(run_id)
        if state and state.status in ("pending", "running"):
            state.status = "cancelled"
            state.end_time = time.time()
        bridge = self._bridges.get(run_id)
        if bridge:
            bridge.emit("run_cancelled", {"reason": "user_requested"})
        return True

    def bridge(self, run_id: str) -> _TracerBridge | None:
        """给编排器获取桥接器实例，编排器调用bridge.emit发事件"""
        return self._bridges.get(run_id)

    # --- 事件流 ---
    def queue(self, run_id: str) -> queue.Queue | None:
        """SSE接口调用这个方法拿到任务专属队列，循环get()消费事件"""
        return self._queues.get(run_id)

    # --- 清理 ---
    def cleanup(self, run_id: str) -> None:
        """任务结束/取消后，清理所有这个run对应的内存资源，防止内存泄漏"""
        with self._lock:
            self._states.pop(run_id, None)
            self._queues.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            self._futures.pop(run_id, None)
            self._bridges.pop(run_id, None)


# 全局单例：整个 FastAPI 进程共享这一个管理器。
# ≈ Spring 里容器管理的 @Service 单例 —— Python 没有 IoC 容器，
# 就用"模块级变量 + from app.api.task_manager import run_manager"来模拟
# （import 有缓存，所有模块拿到的是同一个实例）。
run_manager = RunManager()
