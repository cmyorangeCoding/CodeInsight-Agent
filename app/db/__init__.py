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

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


# ------------------------------- Engine -------------------------------
# 全局唯一 engine，生命周期等于整个 FastAPI 进程
# pool_pre_ping=True：每次取连接先 ping 一下，PostgreSQL 重启后自动恢复
engine = create_engine(
    settings.db.url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=False,  # True 会打印所有 SQL，调试时开
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


# ------------------------------- 依赖注入 -------------------------------
def get_db():
    """FastAPI 依赖注入：每个请求拿一个 Session，请求结束自动关闭。

    用法：
        @app.get("/runs")
        def list_runs(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ------------------------------- 表自动创建 -------------------------------
def init_tables() -> None:
    """启动时确保所有 ORM 对应的表存在（CREATE TABLE IF NOT EXISTS）。

    注意：只有被 import 进来的实体类才会被 Base 注册，
    所以这里需要先 import 所有 models 模块。
    """
    from sqlalchemy import text

    from app.db.runs import Run  # noqa: F401 — 触发 ORM 注册

    Base.metadata.create_all(bind=engine)

    # 兼容：如果 runs 表是老版本（没 events_json 列），create_all 不会给已有表加新列，
    # 需要手动 ALTER TABLE 补上。try/except 兜底：列已存在就忽略错误。
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE runs ADD COLUMN events_json TEXT NULL"
            ))
            conn.commit()
    except Exception:
        # 列已存在或表不存在（create_all 刚创建的）都正常，忽略
        pass
