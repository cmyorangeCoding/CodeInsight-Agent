"""故障注入测试五：事件持久化、服务重启后的补发与 SSE 续传。"""
from __future__ import annotations

import asyncio
import json
import queue

from app.api.events import RunEvent
from app.api.publisher import RunEventPublisher
from app.api.sse import stream_events
from app.db.run_events import RunEventDAO


def test_event_sequence_is_monotonic_and_persisted_before_push(session_factory):
    q: queue.Queue = queue.Queue()
    publisher = RunEventPublisher("run-evt", q, session_factory=session_factory)

    seqs = []
    for i in range(5):
        evt = publisher.emit("info", {"i": i})
        seqs.append(evt.sequence)
        queued = q.get_nowait()
        assert queued.sequence == evt.sequence
        # 「先持久化、再推送」：事件已经在 DB 中
        with session_factory() as session:
            rows = RunEventDAO(session).list_after("run-evt", -1)
            assert [r.sequence for r in rows] == seqs

    assert seqs == [0, 1, 2, 3, 4]


def test_restart_continues_sequence_and_replays_history(session_factory):
    first = RunEventPublisher("run-evt", None, session_factory=session_factory)
    for i in range(3):
        first.emit("info", {"i": i})

    # 模拟服务重启：新的 Publisher 实例从 DB 的 max(sequence) 继续
    restarted = RunEventPublisher("run-evt", None, session_factory=session_factory)
    assert restarted.emit("info", {"i": 3}).sequence == 3

    replayed = restarted.replay(after_sequence=1)
    assert [e.sequence for e in replayed] == [2, 3]

    snapshot = json.loads(restarted.snapshot_json())
    assert [item["type"] for item in snapshot] == ["info", "info", "info", "info"]


def test_publisher_falls_back_to_memory_when_db_unavailable():
    q: queue.Queue = queue.Queue()
    publisher = RunEventPublisher(
        "run-degraded", q,
        session_factory=lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    e0 = publisher.emit("info", {"a": 1})
    e1 = publisher.emit("info", {"a": 2})
    assert (e0.sequence, e1.sequence) == (0, 1)
    assert q.get_nowait().sequence == 0
    assert publisher.replay(0) == []


def test_sse_stream_replays_history_then_switches_to_live_without_gaps():
    history = [
        RunEvent(run_id="run-sse", sequence=i, type="info", timestamp=0.0,
                 data={"i": i})
        for i in range(3)
    ]
    live = RunEvent(run_id="run-sse", sequence=3, type="info", timestamp=0.0,
                    data={"i": 3})
    state = {"polls": 0}

    async def replay(after: int):
        await asyncio.sleep(0)
        pool = history + [live]
        return [e for e in pool if e.sequence > after]

    def drain():
        # 故意返回与历史重叠的事件，验证 sequence 去重
        state["polls"] += 1
        return [history[1], history[2]]

    async def status():
        return "completed" if state["polls"] >= 2 else "running"

    async def collect():
        out = []
        async for item in stream_events(
            "run-sse", cursor=0, replay=replay, drain=drain, status=status,
            poll_interval=0.01, heartbeat_interval=100,
        ):
            out.append(item)
        return out

    items = asyncio.run(asyncio.wait_for(collect(), timeout=5))

    connected, *rest = items
    assert connected["event"] == "connected"
    event_ids = [int(it["id"]) for it in rest if it.get("event") != "run_status"]
    assert event_ids == [1, 2, 3]  # 从 cursor=0 之后开始，严格递增且无重复

    terminal = rest[-1]
    assert terminal["event"] == "run_status"
    assert json.loads(terminal["data"])["status"] == "completed"
    assert json.loads(terminal["data"])["last_sequence"] == 3
