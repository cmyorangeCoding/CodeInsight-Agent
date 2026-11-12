"""数据库基础设施：SQLAlchemy engine + session 工厂 + Base。

SpringBoot 对照：
- create_engine() ≈ DataSource（连接池）
- SessionLocal ≈ JpaRepository 的 EntityManager
- Base ≈ @MappedSuperclass 注册中心

为什么放 app/db/ 而不是 app/rag/models.py 里？
  因为 app/rag/models.py 的 Base 是 pgvector 场景专用的（有 Vector 列），
  我们的 runs 表只是普通字段，不依赖 pgvector，单独一份更干净。
"""
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


# ------------------------------- Engine -------------------------------
# 全局唯一 engine，生命周期等于整个 FastAPI 进程
# pool_pre_ping=True：每次取连接先 ping 一下，PostgreSQL 重启后自动恢复
#
# 没有配置 DATABASE_URL 时不创建 engine（纯单元测试 / 本地无库降级场景），
# 此时需要持久化的功能（runs / run_events / 任务恢复）会显式抛错或跳过。
engine = (
    create_engine(
        settings.db.url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=False,  # True 会打印所有 SQL，调试时开
    )
    if settings.db.url
    else None
)


# ------------------------------- Session -------------------------------
# SpringBoot 的 @Transactional 效果：每个请求从 SessionLocal() 拿一个 Session，
# 请求结束后 session.close() 自动归还连接池。
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # commit 后对象还能访问属性（JPA 默认行为）
)


# ------------------------------- Base -------------------------------
class Base(DeclarativeBase):
    """所有 ORM 实体的基类（等价于 JPA 的 @MappedSuperclass）。"""
    pass


def db_available() -> bool:
    """是否配置了数据库（不代表连接可用，只代表有连接串）。"""
    return engine is not None


# ------------------------------- 依赖注入 -------------------------------
def get_db():
    """FastAPI 依赖注入：每个请求拿一个 Session，请求结束自动关闭。

    用法：
        @app.get("/runs")
        def list_runs(db: Session = Depends(get_db)):
            ...
    """
    if engine is None:
        raise RuntimeError("DATABASE_URL 未配置，数据库功能不可用")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ------------------------------- 表自动创建 -------------------------------
# runs 表在 2026-11 的断点恢复改造中新增的列。
# create_all 不会给已存在的表补列，这里用 ADD COLUMN IF NOT EXISTS 做增量迁移。
_RUNS_NEW_COLUMNS = [
    ("task_id", "VARCHAR(32)"),
    ("thread_id", "VARCHAR(64)"),
    ("requested_strategy", "VARCHAR(32)"),
    ("user_id", "VARCHAR(64)"),
    ("attempt", "INTEGER NOT NULL DEFAULT 0"),
    ("worker_id", "VARCHAR(64)"),
    ("lease_until", "TIMESTAMP"),
    ("cancel_requested", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("last_error", "TEXT"),
]


def init_tables() -> None:
    """启动时确保所有 ORM 对应的表存在（CREATE TABLE IF NOT EXISTS）。"""
    if engine is None:
        print("[db] DATABASE_URL 未配置，跳过建表与迁移")
        return

    from app.db.runs import Run  # noqa: F401 — 触发 ORM 注册
    from app.db.run_events import RunEventRecord  # noqa: F401
    from app.db.tool_executions import ToolExecutionRecord  # noqa: F401

    Base.metadata.create_all(bind=engine)

    # 增量迁移：兼容改造前已有的 runs 表
    with engine.connect() as conn:
        for column, ddl in _RUNS_NEW_COLUMNS:
            try:
                conn.execute(text(f"ALTER TABLE runs ADD COLUMN IF NOT EXISTS {column} {ddl}"))
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - 列已存在 / 方言不支持都忽略
                print(f"[db] WARNING: add column runs.{column} failed: {exc}")
                conn.rollback()
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_runs_status_lease ON runs (status, lease_until)",
            "CREATE INDEX IF NOT EXISTS idx_runs_task_time ON runs (task_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_run_events_run_seq ON run_events (run_id, sequence)",
            # 迁移前的历史数据：每条 run 自成一个单轮任务
            "UPDATE runs SET task_id = run_id WHERE task_id IS NULL",
        ):
            try:
                conn.execute(text(ddl))
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()
