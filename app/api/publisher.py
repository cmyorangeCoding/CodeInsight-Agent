"""RunEvent 事件发布器：先持久化，再推送 SSE。

顺序保证：
  1. 先写 run_events（sequence 由 DB 唯一约束分配）；
  2. 再放入内存队列供 SSE 实时消费。
这样即使进程在「写库后、推送前」崩溃，重启后也能从 DB 补发；
不会出现「前端看到了事件、库里没有」的不可恢复状态。

数据库不可用时降级为仅内存推送（旧行为），此时不具备重启补发能力。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

from app.api.events import RunEvent
from app.event_mapper import map_tracer_to_run_event


MAX_DATA_SIZE = 2048  # 单字段最大字符数，防止大文件内容撑爆事件表


def _truncate_data(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > MAX_DATA_SIZE:
            out[key] = text[:MAX_DATA_SIZE] + f"...(truncated {len(text)} chars)"
        else:
            out[key] = value
    return out


def serialize_tracer_events(tracer) -> str:
    """把 Tracer 事件映射成 RunEvent 快照（兼容 events_json 历史格式）。"""
    truncated = []
    for ev in getattr(tracer, "events", []):
        mapped = map_tracer_to_run_event(ev.get("type", ""), ev)
        if mapped is None:
            continue
        event_type, data = mapped
        truncated.append({"type": event_type, "data": _truncate_data(data)})
    return json.dumps(truncated, ensure_ascii=False) if truncated else "[]"


class RunEventPublisher:
    def __init__(self, run_id: str, event_queue: queue.Queue | None = None,
                 persist: bool = True, session_factory=None):
        self.run_id = run_id
        self._queue = event_queue
        self._persist = persist
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._fallback_seq = 0
        self._db_ok = persist
        self._last_seq = -1

    # ---------------------------------------------------------------- 写入
    def _append_db(self, event_type: str, data: dict) -> int | None:
        if not self._persist or not self._db_ok:
            return None
        try:
            from app.db import SessionLocal
            from app.db.run_events import RunEventDAO

            factory = self._session_factory or SessionLocal
            session = factory()
            try:
                return RunEventDAO(session).append(self.run_id, event_type, data)
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001 - 事件持久化失败不能阻断任务
            print(f"[event] WARNING: persist failed for {self.run_id}: {exc}")
            return None

    def emit(self, event_type: str, data: dict | None = None) -> RunEvent:
        payload = _truncate_data(data or {})
        seq = self._append_db(event_type, payload)
        with self._lock:
            if seq is None:
                seq = max(self._fallback_seq, self._last_seq + 1)
                self._fallback_seq = seq + 1
            self._last_seq = max(self._last_seq, seq)
        evt = RunEvent(
            run_id=self.run_id,
            sequence=seq,
            type=event_type,
            timestamp=time.time(),
            data=payload,
        )
        if self._queue is not None:
            self._queue.put_nowait(evt)
        return evt

    # ---------------------------------------------------------------- 读取
    @property
    def last_sequence(self) -> int:
        return self._last_seq

    def replay(self, after_sequence: int = -1, limit: int = 1000) -> list[RunEvent]:
        """从 run_events 读取 sequence > after_sequence 的历史事件。"""
        if not self._persist:
            return []
        try:
            from app.db import SessionLocal
            from app.db.run_events import RunEventDAO

            factory = self._session_factory or SessionLocal
            session = factory()
            try:
                rows = RunEventDAO(session).list_after(self.run_id, after_sequence, limit)
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[event] WARNING: replay failed for {self.run_id}: {exc}")
            return []

        events: list[RunEvent] = []
        for row in rows:
            try:
                data = json.loads(row.data) if row.data else {}
            except Exception:  # noqa: BLE001
                data = {}
            created = row.created_at or datetime.now(timezone.utc)
            events.append(RunEvent(
                run_id=self.run_id,
                sequence=int(row.sequence),
                type=row.event_type,
                timestamp=created.replace(tzinfo=timezone.utc).timestamp()
                if created.tzinfo is None else created.timestamp(),
                data=data,
            ))
            self._last_seq = max(self._last_seq, int(row.sequence))
        return events

    def snapshot_json(self, fallback_events_json: str | None = None) -> str:
        """终态快照：优先用 run_events 重建，失败则回落到 tracer 快照。"""
        if self._persist:
            try:
                from app.db import SessionLocal
                from app.db.run_events import RunEventDAO

                factory = self._session_factory or SessionLocal
                session = factory()
                try:
                    dao = RunEventDAO(session)
                    if dao.count(self.run_id) > 0:
                        return dao.snapshot_json(self.run_id)
                finally:
                    session.close()
            except Exception:  # noqa: BLE001
                pass
        return fallback_events_json or "[]"
