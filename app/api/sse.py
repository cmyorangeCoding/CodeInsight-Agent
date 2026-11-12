"""SSE 事件流核心逻辑（与 Web 框架解耦，便于故障注入测试）。

语义：
- cursor 是「客户端最后收到的 sequence」，服务端只发 sequence > cursor 的事件；
- 先补发持久化历史（replay），再排空实时队列（drain），两边都按 sequence 去重；
- 任务进入终态后再补发一轮，然后发送 run_status 终止事件并结束流。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "success", "partial")


async def stream_events(
    run_id: str,
    cursor: int,
    replay: Callable[[int], Awaitable[list[Any]]],
    drain: Callable[[], list[Any]],
    status: Callable[[], Awaitable[str | None]],
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    poll_interval: float = 0.2,
    heartbeat_interval: float = 15.0,
):
    yield {
        "event": "connected",
        "data": json.dumps({"run_id": run_id, "after": cursor}, ensure_ascii=False),
    }
    last_heartbeat = time.monotonic()
    last_status_check = 0.0
    terminal: str | None = None

    while True:
        if is_disconnected is not None and await is_disconnected():
            return

        # 1) 历史补发
        for evt in await replay(cursor):
            if evt.sequence <= cursor:
                continue
            cursor = evt.sequence
            yield evt.to_sse_dict()

        # 2) 实时队列
        for evt in drain():
            if evt.sequence <= cursor:
                continue
            cursor = evt.sequence
            yield evt.to_sse_dict()

        # 3) 终态判断（节流后查库）
        if time.monotonic() - last_status_check > 1.0:
            last_status_check = time.monotonic()
            current = await status()
            if current in TERMINAL_STATUSES:
                terminal = current

        if terminal:
            for evt in await replay(cursor):
                if evt.sequence <= cursor:
                    continue
                cursor = evt.sequence
                yield evt.to_sse_dict()
            yield {
                "event": "run_status",
                "data": json.dumps(
                    {"run_id": run_id, "status": terminal, "last_sequence": cursor},
                    ensure_ascii=False,
                ),
            }
            return

        if time.monotonic() - last_heartbeat > heartbeat_interval:
            last_heartbeat = time.monotonic()
            yield {"comment": "keep-alive"}
        await asyncio.sleep(poll_interval)
