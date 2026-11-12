"""历史任务 CRUD API —— 独立 Router

路由前缀：/api/history

和 /api/runs 的分工：
  /api/runs       → 运行时控制（创建任务、SSE 事件流、取消）
  /api/history    → 历史管理（列表、详情、删除、仓库分组）

"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.runs import RunDAO, run_to_dict


router = APIRouter(prefix="/api/history", tags=["history"])


# ------------------------------- Response DTO -------------------------------

class RunResponse(BaseModel):
    """单条 run 记录的返回 DTO。

    events 字段：orchestrator.run() 返回后把 tracer.events 序列化存进 DB，
    前端历史查看时直接拿这列表复用现有模板渲染（执行进度 + 代码证据一起恢复）。
    """
    run_id: str
    task_id: str | None = None
    repo_source: str
    repo_identifier: str
    repository_id: str | None = None
    question: str
    answer: str = ""
    status: str
    strategy: str | None = None
    duration_ms: float | None = None
    created_at: str | None = None
    updated_at: str | None = None
    events: list[dict] | None = None  # 历史查看时恢复执行进度 + 证据


class ListRunsResponse(BaseModel):
    """列表返回 DTO（带分页元信息）。"""
    total: int
    items: list[RunResponse]


class RepoGroupResponse(BaseModel):
    """仓库分组项。"""
    repo_source: str
    repo_identifier: str
    run_count: int


class ListReposResponse(BaseModel):
    """所有仓库分组列表。"""
    items: list[RepoGroupResponse]


class TaskResponse(BaseModel):
    """任务摘要：同一 task_id 下所有轮次的聚合。"""
    task_id: str
    repo_source: str
    repo_identifier: str
    repository_id: str | None = None
    title: str = ""
    run_count: int = 0
    status: str = "pending"
    created_at: str | None = None
    updated_at: str | None = None


class ListTasksResponse(BaseModel):
    items: list[TaskResponse]


class TaskDetailResponse(BaseModel):
    """任务详情：任务摘要 + 按时间正序的每一轮（对话顺序）。"""
    task: TaskResponse
    runs: list[RunResponse]


# ------------------------------- Routes -------------------------------

@router.get("/repos", response_model=ListReposResponse)
def list_repos(db: Session = Depends(get_db)):
    """
    列出所有出现过的仓库分组（按最近活跃时间排序）。

    前端左侧栏可以先调这个拿"有哪些仓库"，
    再按用户选中的仓库调 /api/history/runs?repo_source=...&repo_identifier=...
    列出该仓库下的所有 run。
    """
    dao = RunDAO(db)
    groups = dao.list_repos()
    return ListReposResponse(items=[
        RepoGroupResponse(
            repo_source=s, repo_identifier=i, run_count=c,
        )
        for s, i, c in groups
    ])


@router.get("/tasks", response_model=ListTasksResponse)
def list_tasks(
    repo_source: str | None = Query(None, description="local / github"),
    repo_identifier: str | None = Query(None, description="本地路径 或 owner/repo"),
    db: Session = Depends(get_db),
):
    """列出任务（按 task_id 聚合，最近活跃的在前）。"""
    items = RunDAO(db).list_tasks(
        repo_source=repo_source, repo_identifier=repo_identifier
    )
    return ListTasksResponse(items=[TaskResponse(**t) for t in items])


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
def get_task(task_id: str, db: Session = Depends(get_db)):
    """任务详情：摘要 + 按时间正序的全部轮次。"""
    runs = RunDAO(db).list_by_task(task_id)
    if not runs:
        raise HTTPException(404, f"task {task_id} not found")
    first, last = runs[0], runs[-1]
    task = TaskResponse(
        task_id=task_id,
        repo_source=first.repo_source,
        repo_identifier=first.repo_identifier,
        repository_id=first.repository_id,
        title=(first.question or "")[:120],
        run_count=len(runs),
        status=last.status,
        created_at=first.created_at.isoformat() if first.created_at else None,
        updated_at=last.updated_at.isoformat() if last.updated_at else None,
    )
    return TaskDetailResponse(
        task=task,
        runs=[RunResponse(**run_to_dict(r)) for r in runs],
    )


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, db: Session = Depends(get_db)):
    """删除一个任务的全部轮次。"""
    n = RunDAO(db).delete_by_task(task_id)
    if n == 0:
        raise HTTPException(404, f"task {task_id} not found")
    return {"task_id": task_id, "deleted_count": n}


@router.get("/runs", response_model=ListRunsResponse)
def list_runs(
    repo_source: str | None = Query(None, description="local / github"),
    repo_identifier: str | None = Query(None, description="本地路径 或 owner/repo"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    列出 run 历史（按 created_at DESC，最新的在前）。

    过滤参数：
      - 不传 repo_source/repo_identifier → 全局历史
      - 传了 → 只看某个仓库下的历史
    """
    dao = RunDAO(db)
    items = dao.list(
        repo_source=repo_source,
        repo_identifier=repo_identifier,
        limit=limit,
        offset=offset,
    )
    # total 简化处理：不单独查 count，返回当前页条数
    # 如果将来要真分页（显示"共 N 条"），DAO.list 里加个 count 查询
    return ListRunsResponse(
        total=len(items),
        items=[RunResponse(**run_to_dict(r)) for r in items],
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, db: Session = Depends(get_db)):
    """按 run_id 查历史详情。"""
    dao = RunDAO(db)
    run = dao.get(run_id)
    if not run:
        raise HTTPException(404, f"run {run_id} not found")
    return RunResponse(**run_to_dict(run))


@router.delete("/runs/{run_id}")
def delete_run(run_id: str, db: Session = Depends(get_db)):
    """删除一条历史。"""
    dao = RunDAO(db)
    ok = dao.delete(run_id)
    if not ok:
        raise HTTPException(404, f"run {run_id} not found")
    return {"run_id": run_id, "deleted": True}


@router.delete("/runs")
def delete_runs_by_repo(
    repo_source: str = Query(..., description="local / github"),
    repo_identifier: str = Query(..., description="本地路径 或 owner/repo"),
    db: Session = Depends(get_db),
):
    """清空某个仓库下的所有历史。"""
    dao = RunDAO(db)
    n = dao.delete_by_repo(repo_source, repo_identifier)
    return {"repo_source": repo_source, "repo_identifier": repo_identifier, "deleted_count": n}
