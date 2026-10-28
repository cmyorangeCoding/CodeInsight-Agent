"""Run 实体 + DAO（数据访问对象）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, String, Text, Double, DateTime, Index, desc
from sqlalchemy.orm import Session

from app.db import Base


# ------------------------------- ORM 实体 -------------------------------

class Run(Base):
    """
    runs 表：每条记录 = 一次代码分析任务。

    字段设计：
      - run_id（PK）：32 位 UUID 字符串，和前端 /api/runs/{id} 的 id 一致
      - repo_source / repo_identifier：仓库来源分组（local 路径 或 GitHub owner/repo）
      - question / answer：用户输入 + Agent 最终回答（answer 可能很长，用 Text）
      - status：五态（pending→running→completed/failed/cancelled）
      - strategy：路由策略（auto/direct/react/plan_execute）
      - duration_ms：完成后填，方便前端排序/统计
      - created_at / updated_at：UTC 时间戳，前端按这个倒序列
    """
    __tablename__ = "runs"

    run_id = Column(String(32), primary_key=True, comment="任务唯一 ID（32 位）")

    # 仓库分组（联合索引加速按仓库查历史）
    repo_source = Column(String(16), nullable=False, comment="local / github")
    repo_identifier = Column(String(512), nullable=False, comment="本地路径 或 owner/repo")
    repository_id = Column(String(128), nullable=True, comment="RAG 索引隔离 ID")

    # 问答内容
    question = Column(Text, nullable=False, comment="用户原始问题")
    answer = Column(Text, nullable=True, comment="Agent 最终回答 Markdown")

    # 任务状态
    status = Column(String(16), nullable=False, default="pending",
                   comment="pending/running/completed/failed/cancelled")
    strategy = Column(String(32), nullable=True, comment="auto/direct/react/plan_execute")
    duration_ms = Column(Double, nullable=True, comment="总耗时（毫秒）")

    # 时间戳（UTC，数据库层面不存时区）
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # 事件流快照：orchestrator.run() 返回后把 tracer.events 序列化成 JSON 存这里。
    # 为什么一列就够？
    #   运行时前端靠 SSE 实时推送 events；历史查看时从 DB 读这列反序列化，
    #   执行进度 + 代码证据（retrieval_completed 事件里带 chunks）自动恢复，
    #   前端复用现有模板渲染。
    # 为什么不新建 run_events 表？
    #   一条 run 几百个事件 → 几百行数据，写起来麻烦查起来也麻烦。
    #   Tracer 本来就有 self.events 列表，直接 json.dumps() 最省事。
    events_json = Column(Text, nullable=True, comment="执行事件流 JSON 快照")

    __table_args__ = (
        # 按仓库查历史 + 按时间倒序（最常用的查询模式）
        Index("idx_runs_repo_time", "repo_source", "repo_identifier", "created_at"),
        # 全局时间倒序
        Index("idx_runs_created", "created_at"),
    )

    def __repr__(self):
        q = (self.question or "")[:30]
        return f"<Run(run_id={self.run_id[:8]}..., status={self.status}, q='{q}...')>"


# ------------------------------- DAO -------------------------------

class RunDAO:
    """runs 表的数据访问层。

    所有方法都接收一个 sqlalchemy Session，由 Router 通过 Depends(get_db) 注入。
    SpringBoot 对照：这就是一个 @Repository，只不过我们手动传 Session。
    """

    def __init__(self, db: Session):
        self.db = db

    # ---------- C ----------
    def create(self, run_id: str, repo_source: str, repo_identifier: str,
               question: str, repository_id: str | None = None) -> Run:
        """新建一条 run 记录（任务刚创建时调用，status=pending）。"""
        run = Run(
            run_id=run_id,
            repo_source=repo_source,
            repo_identifier=repo_identifier,
            repository_id=repository_id,
            question=question,
            status="pending",
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    # ---------- R ----------
    def get(self, run_id: str) -> Optional[Run]:
        """按 run_id 查单条。"""
        return self.db.query(Run).filter(Run.run_id == run_id).first()

    def list(self, repo_source: str | None = None,
             repo_identifier: str | None = None,
             limit: int = 100, offset: int = 0) -> list[Run]:
        """
        列出 run 历史（按 created_at DESC，最新的在前）。

        :param repo_source: 过滤仓库来源（local/github），None = 全部
        :param repo_identifier: 过滤仓库标识，None = 全部
        :param limit: 每页条数（默认 100）
        :param offset: 分页偏移
        """
        q = self.db.query(Run)
        if repo_source:
            q = q.filter(Run.repo_source == repo_source)
        if repo_identifier:
            q = q.filter(Run.repo_identifier == repo_identifier)
        return q.order_by(desc(Run.created_at)).limit(limit).offset(offset).all()

    def list_repos(self) -> list[tuple[str, str, int]]:
        """
        列出所有"出现过的仓库"，以及每个仓库下有多少条 run。

        返回 [(repo_source, repo_identifier, run_count), ...]
        前端左侧栏可以用这个做仓库分组标题。
        """
        from sqlalchemy import func
        rows = (
            self.db.query(
                Run.repo_source,
                Run.repo_identifier,
                func.count(Run.run_id).label("cnt"),
            )
            .group_by(Run.repo_source, Run.repo_identifier)
            .order_by(func.max(Run.created_at).desc())  # 最近活跃的仓库排前面
            .all()
        )
        return [(r[0], r[1], r[2]) for r in rows]

    # ---------- U ----------
    def update_status(self, run_id: str, status: str,
                      answer: str | None = None,
                      strategy: str | None = None,
                      duration_ms: float | None = None,
                      events_json: str | None = None) -> Optional[Run]:
        """任务结束时更新：status + 可选 answer/strategy/duration/events_json。

        events_json 是 tracer.events 序列化后的 JSON 字符串（带大 payload 截断）。
        存了它 = 历史查看时能恢复完整的执行进度 + 代码证据。
        """
        run = self.get(run_id)
        if not run:
            return None
        run.status = status
        if answer is not None:
            run.answer = answer
        if strategy is not None:
            run.strategy = strategy
        if duration_ms is not None:
            run.duration_ms = duration_ms
        if events_json is not None:
            run.events_json = events_json
        self.db.commit()
        self.db.refresh(run)
        return run

    def patch(self, run_id: str, **fields) -> Optional[Run]:
        """通用补丁更新（内部用，不对外暴露）。"""
        run = self.get(run_id)
        if not run:
            return None
        for k, v in fields.items():
            if hasattr(run, k):
                setattr(run, k, v)
        self.db.commit()
        self.db.refresh(run)
        return run

    # ---------- D ----------
    def delete(self, run_id: str) -> bool:
        """按 run_id 删除，返回是否成功。"""
        run = self.get(run_id)
        if not run:
            return False
        self.db.delete(run)
        self.db.commit()
        return True

    def delete_by_repo(self, repo_source: str, repo_identifier: str) -> int:
        """删除某个仓库下的所有 run（返回删除条数）。"""
        n = self.db.query(Run).filter(
            Run.repo_source == repo_source,
            Run.repo_identifier == repo_identifier,
        ).delete()
        self.db.commit()
        return n


def run_to_dict(run: Run) -> dict:
    """ORM → dict（给 FastAPI response_model 序列化用）。

    events_json 是 TEXT 列里存的 JSON 字符串，这里反序列化成 Python list。
    兼容两种格式：
    - 新格式：map_tracer_to_run_event 映射后的 RunEvent（推荐）
    - 老格式：直接透传 Tracer raw event type（历史兼容）
    用 remap_events_if_needed 做运行时重映射，不管 DB 里是啥格式，
    返回给前端的全是 RunEvent，前端模板不用区分。
    """
    import json as _json

    events = None
    if run.events_json:
        try:
            raw_events = _json.loads(run.events_json)
            # 运行时重映射：老 raw type → 新 RunEvent type（新格式自动跳过）
            from app.event_mapper import remap_events_if_needed
            events = remap_events_if_needed(raw_events)
        except Exception:
            events = None
    return {
        "run_id": run.run_id,
        "repo_source": run.repo_source,
        "repo_identifier": run.repo_identifier,
        "repository_id": run.repository_id,
        "question": run.question,
        "answer": run.answer or "",
        "status": run.status,
        "strategy": run.strategy,
        "duration_ms": run.duration_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "events": events,
    }
