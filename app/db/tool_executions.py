"""tool_executions 表：有副作用工具的幂等执行记录。

背景：
  Agent 的工具调用与 LangGraph checkpoint 落盘之间存在异常窗口：
  工具已经产生外部副作用，但 checkpoint 还没保存，进程崩溃后恢复会再次调用该工具。

做法（不是严格 exactly-once）：
  - 执行前用幂等键 (run_id + tool_name + 参数摘要) 抢占记录；
  - 执行成功后写入 completed + 结果；
  - 恢复后同一键再次调用时直接返回缓存结果，避免重复副作用。
  - 若崩溃发生在「副作用已发生、completed 尚未写入」之间，仍可能重复执行；
    这个窗口无法仅靠数据库消除，属于明确的可靠性边界。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ToolExecutionRecord(Base):
    __tablename__ = "tool_executions"

    idempotency_key = Column(String(160), primary_key=True,
                             comment="run_id + tool_name + 参数摘要")
    run_id = Column(String(32), nullable=False, index=True)
    tool_name = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="running",
                    comment="running / completed / failed")
    result = Column(Text, nullable=True, comment="工具执行结果（用于恢复时回放）")
    error = Column(Text, nullable=True)
    attempt = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)


class ToolExecutionStore:
    """幂等记录存储。session_factory 可注入（测试用 SQLite）。"""

    def __init__(self, session_factory: Callable[[], Session] | None = None):
        self._session_factory = session_factory or SessionLocal

    def _session(self) -> Session | None:
        try:
            return self._session_factory()
        except Exception:  # noqa: BLE001 - 无数据库时降级为「不缓存」
            return None

    def get(self, key: str) -> ToolExecutionRecord | None:
        session = self._session()
        if session is None:
            return None
        try:
            return session.query(ToolExecutionRecord).filter(
                ToolExecutionRecord.idempotency_key == key
            ).first()
        finally:
            session.close()

    def begin(self, key: str, run_id: str, tool_name: str) -> None:
        """抢占/更新执行记录。记录已存在时把状态改回 running 并累加 attempt。"""
        session = self._session()
        if session is None:
            return
        now = _utcnow()
        try:
            row = ToolExecutionRecord(
                idempotency_key=key,
                run_id=run_id,
                tool_name=tool_name,
                status="running",
                attempt=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                session.query(ToolExecutionRecord).filter(
                    ToolExecutionRecord.idempotency_key == key
                ).update({
                    "status": "running",
                    "attempt": ToolExecutionRecord.attempt + 1,
                    "updated_at": now,
                })
                session.commit()
        except Exception:  # noqa: BLE001 - 幂等记录失败不能阻断工具调用
            session.rollback()
        finally:
            session.close()

    def complete(self, key: str, result: str) -> None:
        session = self._session()
        if session is None:
            return
        try:
            session.query(ToolExecutionRecord).filter(
                ToolExecutionRecord.idempotency_key == key
            ).update({
                "status": "completed",
                "result": result,
                "error": None,
                "updated_at": _utcnow(),
            })
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
        finally:
            session.close()

    def fail(self, key: str, error: str) -> None:
        session = self._session()
        if session is None:
            return
        try:
            session.query(ToolExecutionRecord).filter(
                ToolExecutionRecord.idempotency_key == key
            ).update({
                "status": "failed",
                "error": error[:2000],
                "updated_at": _utcnow(),
            })
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
        finally:
            session.close()

    def is_stale(self, key: str, stale_seconds: float = 300.0) -> bool:
        row = self.get(key)
        if row is None or row.updated_at is None:
            return True
        return row.updated_at < _utcnow() - timedelta(seconds=stale_seconds)
