"""Run 实体 + DAO（数据访问对象）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Double,
    Index,
    Integer,
    String,
    Text,
    and_,
    desc,
    or_,
    update,
)
from sqlalchemy.orm import Session

from app.db import Base


def utcnow() -> datetime:
    """统一使用 naive UTC，和现有 created_at/updated_at 的存储方式一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------- ORM 实体 -------------------------------

class Run(Base):
    """
    runs 表：每条记录 = 任务里的一轮提问（一次代码分析请求）。

    层级：仓库 → 任务（task_id，多轮会话）→ run（每一轮提问）。
    同一 task_id 的多轮 run 共享一条 LangGraph 线程（thread_id = task-{task_id}）。

    字段设计：
      - run_id（PK）：32 位 UUID 字符串，和前端 /api/runs/{id} 的 id 一致
      - task_id：会话任务 ID，同一任务的多轮 run 相同；恢复与上下文都以任务为单位
      - repo_source / repo_identifier：仓库来源分组（local 路径 或 GitHub owner/repo）
      - question / answer：用户输入 + Agent 最终回答（answer 可能很长，用 Text）
      - status：五态（pending→running→completed/failed/cancelled）
      - strategy：路由策略（auto/direct/react/plan_execute）
      - duration_ms：完成后填，方便前端排序/统计
      - created_at / updated_at：UTC 时间戳，前端按这个倒序列
    """
    __tablename__ = "runs"

    run_id = Column(String(32), primary_key=True, comment="单轮请求 ID（32 位）")
    task_id = Column(String(32), nullable=True,
                     comment="会话任务 ID：同一任务的多轮 run 共享一条 LangGraph 线程")

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
    # 为什么还新增了 run_events 表？
    #   events_json 只在任务结束时写一次，服务崩溃时整段事件会丢失；
    #   run_events 逐条持久化，用于崩溃后的事件补发与 SSE 续传，
    #   events_json 继续保留作为历史回放的兼容快照。
    events_json = Column(Text, nullable=True, comment="执行事件流 JSON 快照")

    # ---------------- 断点恢复相关（2026-11 增量） ----------------
    # thread_id 不是 task_id 的重复列：它是 LangGraph checkpoint 的线程命名空间键。
    # 当前 task_thread_id() 对后端生成的 task_id 做 1:1 归一化（task-{task_id}），
    # 所以取值看起来和 task_id 一样；单独留一列是为了：
    #   1) 客户端可传自定义 task_id，由 task_thread_id() 统一加前缀/归一化，
    #      业务任务 ID 不必绑死在 checkpoint 的命名规则上；
    #   2) 未来做「任务内重置上下文」或租户/环境隔离时，只换线程不换任务。
    thread_id = Column(String(64), nullable=True,
                       comment="LangGraph 线程命名空间键（当前 task-{task_id}，同任务多轮共用）")
    requested_strategy = Column(String(32), nullable=True,
                                comment="创建任务时请求的策略（auto/direct/react/plan）")
    user_id = Column(String(64), nullable=True, comment="用户标识（长期记忆隔离用）")
    attempt = Column(Integer, nullable=False, default=0, server_default="0",
                     comment="执行/恢复次数")
    worker_id = Column(String(64), nullable=True, comment="当前执行者标识")
    lease_until = Column(DateTime, nullable=True, comment="执行租约到期时间（UTC）")
    cancel_requested = Column(Boolean, nullable=False, default=False,
                              server_default="false", comment="是否已被请求取消")
    last_error = Column(Text, nullable=True, comment="最近一次异常信息")

    __table_args__ = (
        # 按仓库查历史 + 按时间倒序（最常用的查询模式）
        Index("idx_runs_repo_time", "repo_source", "repo_identifier", "created_at"),
        # 任务内多轮按时间正序
        Index("idx_runs_task_time", "task_id", "created_at"),
        # 全局时间倒序
        Index("idx_runs_created", "created_at"),
        # 恢复调度器：按状态 + 租约筛选未完成任务
        Index("idx_runs_status_lease", "status", "lease_until"),
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
               question: str, repository_id: str | None = None,
               task_id: str | None = None,
               thread_id: str | None = None,
               requested_strategy: str | None = None,
               user_id: str | None = None) -> Run:
        """新建一条 run 记录（任务刚创建时调用，status=pending）。

        task_id 缺省时退化为 run_id：等价于「这条 run 自成一个单轮任务」，
        兼容迁移前的历史数据。
        """
        run = Run(
            run_id=run_id,
            task_id=task_id or run_id,
            repo_source=repo_source,
            repo_identifier=repo_identifier,
            repository_id=repository_id,
            question=question,
            status="pending",
            thread_id=thread_id,
            requested_strategy=requested_strategy,
            user_id=user_id,
            attempt=0,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    # ---------- 任务认领 / 租约 / 终态保护 ----------
    # 全部用「条件 UPDATE + rowcount」实现，不依赖 Python 锁：
    # 多进程、多 Worker 下只有一个 UPDATE 能命中，天然原子。

    def claim(self, run_id: str, worker_id: str, lease_seconds: float = 60.0,
              target_status: str = "running", now: datetime | None = None) -> bool:
        """原子认领一个 Run。

        可认领条件（终态 completed/failed/cancelled 永远不满足）：
          - pending / recovering：只要没有有效租约
          - running：只有租约已过期或从未设置（原 Worker 可能已崩溃）

        返回 True 表示本 Worker 拿到执行权。
        """
        now = now or utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        claimable = or_(
            and_(
                Run.status.in_(("pending", "recovering")),
                or_(Run.lease_until.is_(None), Run.lease_until < now),
            ),
            and_(
                Run.status == "running",
                or_(Run.lease_until.is_(None), Run.lease_until < now),
            ),
        )
        result = self.db.execute(
            update(Run)
            .where(Run.run_id == run_id, claimable)
            .values(
                status=target_status,
                worker_id=worker_id,
                lease_until=lease_until,
                attempt=Run.attempt + 1,
                last_error=None,
                updated_at=now,
            )
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_running(self, run_id: str, worker_id: str,
                     lease_seconds: float = 60.0) -> bool:
        """recovering → running；只允许当前 Worker 操作。"""
        now = utcnow()
        result = self.db.execute(
            update(Run)
            .where(
                Run.run_id == run_id,
                Run.worker_id == worker_id,
                Run.status.in_(("recovering", "running")),
            )
            .values(
                status="running",
                lease_until=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        self.db.commit()
        return result.rowcount == 1

    def renew_lease(self, run_id: str, worker_id: str,
                    lease_seconds: float = 60.0) -> bool:
        """续租。返回 False 说明执行权已丢失（被别的 Worker 接管或已终态）。"""
        now = utcnow()
        result = self.db.execute(
            update(Run)
            .where(
                Run.run_id == run_id,
                Run.worker_id == worker_id,
                Run.status.in_(("running", "recovering")),
            )
            .values(lease_until=now + timedelta(seconds=lease_seconds), updated_at=now)
        )
        self.db.commit()
        return result.rowcount == 1

    def finish(self, run_id: str, worker_id: str, status: str,
               answer: str | None = None,
               strategy: str | None = None,
               duration_ms: float | None = None,
               events_json: str | None = None,
               last_error: str | None = None) -> bool:
        """终态写入（只允许 running/recovering → 终态）。

        校验 worker_id，防止旧 Worker / 延迟执行覆盖新 Worker 的结果；
        条件里排除终态，保证「取消与完成竞争」时只有一个生效。
        """
        values: dict = {"status": status, "updated_at": utcnow()}
        if answer is not None:
            values["answer"] = answer
        if strategy is not None:
            values["strategy"] = strategy
        if duration_ms is not None:
            values["duration_ms"] = duration_ms
        if events_json is not None:
            values["events_json"] = events_json
        if last_error is not None:
            values["last_error"] = last_error
        result = self.db.execute(
            update(Run)
            .where(
                Run.run_id == run_id,
                Run.worker_id == worker_id,
                Run.status.in_(("running", "recovering")),
            )
            .values(**values)
        )
        self.db.commit()
        return result.rowcount == 1

    def fail_unrecoverable(self, run_id: str, worker_id: str, reason: str) -> bool:
        """无法安全恢复：标记 failed 并记录原因（绝不伪装成恢复成功）。"""
        return self.finish(run_id, worker_id, "failed", last_error=reason,
                           answer=f"（任务无法安全恢复：{reason}）")

    def request_cancel(self, run_id: str) -> bool:
        """取消请求：只对非终态任务生效，终态永远不会被改写。"""
        now = utcnow()
        result = self.db.execute(
            update(Run)
            .where(
                Run.run_id == run_id,
                Run.status.in_(("pending", "running", "recovering")),
            )
            .values(status="cancelled", cancel_requested=True, updated_at=now)
        )
        self.db.commit()
        return result.rowcount == 1

    def is_cancel_requested(self, run_id: str) -> bool:
        row = self.db.query(Run.cancel_requested, Run.status).filter(
            Run.run_id == run_id
        ).first()
        if row is None:
            return False
        return bool(row[0]) or row[1] == "cancelled"

    def update_strategy(self, run_id: str, strategy: str) -> None:
        """路由决策确定后立即落库：恢复时需要知道该用哪个 Agent 图续跑。"""
        self.db.execute(
            update(Run)
            .where(Run.run_id == run_id)
            .values(strategy=strategy, updated_at=utcnow())
        )
        self.db.commit()

    def list_stale(self, now: datetime | None = None, limit: int = 20) -> list[Run]:
        """列出可能由崩溃 Worker 留下的未完成任务（租约为空或已过期）。"""
        now = now or utcnow()
        return (
            self.db.query(Run)
            .filter(
                Run.status.in_(("pending", "running", "recovering")),
                or_(Run.lease_until.is_(None), Run.lease_until < now),
            )
            .order_by(Run.created_at.asc())
            .limit(limit)
            .all()
        )

    def count_events(self, run_id: str) -> int:
        from app.db.run_events import RunEventRecord
        return (
            self.db.query(RunEventRecord)
            .filter(RunEventRecord.run_id == run_id)
            .count()
        )

    # ---------- 任务（task）维度 ----------
    def has_active_run(self, task_id: str) -> bool:
        """同一任务是否还有未结束的一轮（防止多轮并发写同一条线程）。"""
        return (
            self.db.query(Run.run_id)
            .filter(
                Run.task_id == task_id,
                Run.status.in_(("pending", "running", "recovering")),
            )
            .first()
            is not None
        )

    def list_by_task(self, task_id: str) -> list[Run]:
        """任务内所有轮次，按时间正序（前端按对话顺序渲染）。"""
        return (
            self.db.query(Run)
            .filter(Run.task_id == task_id)
            .order_by(Run.created_at.asc(), Run.run_id.asc())
            .all()
        )

    def list_tasks(self, repo_source: str | None = None,
                   repo_identifier: str | None = None,
                   limit: int = 500) -> list[dict]:
        """按 task_id 聚合出任务列表（标题取第一轮的问题）。

        只查渲染任务列表需要的列，不把 events_json 这种大字段读出来。
        返回按最近活跃时间倒序的任务摘要。
        """
        q = self.db.query(
            Run.task_id,
            Run.run_id,
            Run.repo_source,
            Run.repo_identifier,
            Run.repository_id,
            Run.question,
            Run.status,
            Run.created_at,
            Run.updated_at,
        )
        if repo_source:
            q = q.filter(Run.repo_source == repo_source)
        if repo_identifier:
            q = q.filter(Run.repo_identifier == repo_identifier)

        tasks: dict[str, dict] = {}
        for row in q.order_by(Run.created_at.asc()).limit(limit).all():
            task_id = row.task_id or row.run_id
            created = row.created_at.isoformat() if row.created_at else None
            updated = row.updated_at.isoformat() if row.updated_at else None
            item = tasks.get(task_id)
            if item is None:
                tasks[task_id] = {
                    "task_id": task_id,
                    "repo_source": row.repo_source,
                    "repo_identifier": row.repo_identifier,
                    "repository_id": row.repository_id,
                    "title": (row.question or "")[:120],
                    "run_count": 1,
                    "status": row.status,
                    "created_at": created,
                    "updated_at": updated,
                }
            else:
                item["run_count"] += 1
                item["status"] = row.status
                item["updated_at"] = updated

        return sorted(tasks.values(), key=lambda t: t["updated_at"] or "", reverse=True)

    def delete_by_task(self, task_id: str) -> int:
        """删除一个任务的全部轮次。"""
        n = self.db.query(Run).filter(Run.task_id == task_id).delete()
        self.db.commit()
        return n

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
        "task_id": run.task_id,
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
