"""run_events 表：任务执行事件的逐条持久化。

为什么需要它（替代原来只在任务结束时写 events_json 的做法）：
- 服务崩溃时，events_json 还没写入 → 整段执行事件丢失；
- 逐条落库后，重启可以按 sequence 补发历史事件，SSE 也能断线续传；
- sequence 在同一 run 内单调递增，浏览器可用 Last-Event-ID 精确续传。

events_json 仍然保留：作为历史回放的快照，兼容现有前端与历史接口。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RunEventRecord(Base):
    __tablename__ = "run_events"

    # 自增主键只用于 DB 层排序；业务排序一律用 (run_id, sequence)
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(32), nullable=False, index=True, comment="任务 ID")
    sequence = Column(Integer, nullable=False, comment="同一 run 内单调递增序号，从 0 开始")
    event_type = Column(String(32), nullable=False, comment="RunEvent.type")
    data = Column(Text, nullable=True, comment="事件负载 JSON")
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_seq"),
        Index("idx_run_events_run_seq", "run_id", "sequence"),
    )


class RunEventDAO:
    """run_events 数据访问层。"""

    def __init__(self, db: Session):
        self.db = db

    def max_sequence(self, run_id: str) -> int:
        value = (
            self.db.query(func.max(RunEventRecord.sequence))
            .filter(RunEventRecord.run_id == run_id)
            .scalar()
        )
        return -1 if value is None else int(value)

    def append(self, run_id: str, event_type: str, data: dict[str, Any] | None = None,
               max_retries: int = 5) -> int:
        """追加一条事件，返回分配到的 sequence。

        并发写入时靠 (run_id, sequence) 唯一约束兜底：冲突就重新取 max 再试。
        单 run 通常只有一个 Worker 在写，重试次数很小。
        """
        payload = json.dumps(data or {}, ensure_ascii=False, default=str)
        last_error: Exception | None = None
        for _ in range(max_retries):
            seq = self.max_sequence(run_id) + 1
            row = RunEventRecord(
                run_id=run_id,
                sequence=seq,
                event_type=event_type[:32],
                data=payload,
                created_at=_utcnow(),
            )
            self.db.add(row)
            try:
                self.db.commit()
                return seq
            except IntegrityError as exc:  # 序号被别的写入者抢占
                last_error = exc
                self.db.rollback()
                continue
        raise RuntimeError(f"append run event failed after retries: {last_error}")

    def list_after(self, run_id: str, after_sequence: int, limit: int = 1000) -> list[RunEventRecord]:
        return (
            self.db.query(RunEventRecord)
            .filter(
                RunEventRecord.run_id == run_id,
                RunEventRecord.sequence > after_sequence,
            )
            .order_by(RunEventRecord.sequence.asc())
            .limit(limit)
            .all()
        )

    def count(self, run_id: str) -> int:
        return (
            self.db.query(RunEventRecord)
            .filter(RunEventRecord.run_id == run_id)
            .count()
        )

    def snapshot_json(self, run_id: str) -> str:
        """把 run_events 还原成兼容 events_json 的 [{type, data}] 快照。"""
        rows = (
            self.db.query(RunEventRecord)
            .filter(RunEventRecord.run_id == run_id)
            .order_by(RunEventRecord.sequence.asc())
            .all()
        )
        snapshot = []
        for row in rows:
            try:
                data = json.loads(row.data) if row.data else {}
            except Exception:  # noqa: BLE001
                data = {}
            snapshot.append({"type": row.event_type, "data": data})
        return json.dumps(snapshot, ensure_ascii=False)
