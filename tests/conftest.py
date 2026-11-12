"""断点恢复测试的公共夹具。

用文件型 SQLite 替代 PostgreSQL：
- 条件 UPDATE + rowcount 语义与 PostgreSQL 一致，可验证认领/租约/终态保护；
- 不需要外部服务，CI 可直接跑。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.db.run_events import RunEventRecord  # noqa: F401 — 注册 ORM
from app.db.runs import Run  # noqa: F401
from app.db.tool_executions import ToolExecutionRecord  # noqa: F401


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False,
                           expire_on_commit=False)
    yield factory
    engine.dispose()
