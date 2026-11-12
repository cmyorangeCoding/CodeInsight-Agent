"""FastAPI 主应用 —— 整个 Web 后端的"启动类 + Controller 层"。

请求处理流程
  POST /api/runs  →  建任务 + 提交线程池（立即返回 run_id，异步执行）
  GET  /api/runs/{id}/events  →  SSE 长连接，实时推送执行事件（工具调用/检索/回答）

路由清单：
- GET  /api/health                健康检查
- POST /api/runs                  创建分析任务
- GET  /api/runs/{run_id}/events  SSE 事件流
- GET  /api/runs/{run_id}         查询任务状态
- POST /api/runs/{run_id}/cancel  请求取消

核心架构：
1. 接口全部使用`async def`，保证主事件循环不被阻塞；
2. 同步阻塞的 Orchestrator 编排业务，提交到自定义线程池执行，等价于 Spring `@Async`；
3. 任务执行产生的事件写入独立队列，SSE 长连接从队列读取并向前端推送；
4. 全局单例`run_manager`统一管控任务生命周期、事件队列、取消信号；
5. 前后端解耦：创建任务接口立刻返回 run_id，执行进度靠 SSE 流式推送，状态可单独查询。
"""
from __future__ import annotations

import asyncio
import os
import queue as queue_module
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.api.task_manager import run_manager, execution_thread_id
from app.config import settings


# --------------------------------------------------------------------------- Request / Response


class CreateRunRequest(BaseModel):
    """创建分析任务的请求体 DTO。

    BaseModel ≈ 一个带校验的 @RequestBody DTO：FastAPI 自动完成
    JSON → 对象的反序列化 + 字段校验（min_length/max_length 相当于 @Size），
    校验失败自动返回 422，不用手写 BindingResult 判断。
    """

    question: str = Field(..., min_length=1, max_length=5000, description="用户问题")
    repo_source: str = Field("local", description="local 或 github")
    repo_path: str | None = Field(None, description="本地仓库路径（local 模式）")
    github_url: str | None = Field(None, description="GitHub 仓库地址，如 owner/repo 或完整 URL")
    strategy: str = Field("auto", description="auto / direct / react / plan")
    thread_id: str | None = None
    user_id: str | None = None


class CreateRunResponse(BaseModel):
    """创建任务成功后的响应体（自动序列化为 JSON）。"""
    run_id: str
    status: str = "pending"
    repository_id: str


class RunStatusResponse(BaseModel):
    """任务状态查询返回DTO"""
    run_id: str
    status: str
    question: str
    repo_source: str
    repo_identifier: str
    repository_id: str
    strategy: str | None = None
    answer: str = ""
    error: str | None = None
    elapsed_ms: float | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    python_version: str
    repository_id: str
    github_mcp_available: bool


# --------------------------------------------------------------------------- App


# app 启动类实例：
# FastAPI 从这个对象出发注册路由、中间件，uvicorn 加载 "app.api.main:app" 即启动它
app = FastAPI(title="CodeInsight-Agent", version="1.0.0")

# CORS：允许前端 dev server (5173) 跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.web.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------- 启动事件 -----------------------------
# SpringBoot 的 @PostConstruct 等价：应用启动时自动建表（幂等，DROP 不会丢已有数据）
@app.on_event("startup")
def _on_startup():
    try:
        from app.db import init_tables
        init_tables()
    except Exception as exc:
        # 数据库连不上不能让整个应用挂掉（本地开发没 Postgres 时降级）
        print(f"[startup] WARNING: DB init failed (running without persistence): {exc}")

    # 启动恢复调度器：扫描租约过期的未完成任务，按 checkpoint 续跑
    try:
        from app.api.recovery import recovery_scheduler
        recovery_scheduler.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] WARNING: recovery scheduler failed to start: {exc}")


# ----------------------------- 注册子 Router -----------------------------
# history.py 里的 /api/history/* 路由挂到主 app 上，职责分离
from app.api.history import router as _history_router  # noqa: E402
app.include_router(_history_router)


# --------------------------------------------------------------------------- Helpers

#解析github仓库地址
def _parse_github_url(url: str) -> tuple[str, str]:
    """把 owner/repo 或 https://github.com/owner/repo → (owner, repo)。"""
    import re
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)", url)
    if m:
        return m.group(1), m.group(2).rstrip(".git")
    if "/" in url:
        parts = url.strip().split("/", 1)
        return parts[0], parts[1].rstrip(".git")
    raise ValueError(f"无法解析 GitHub 仓库地址: {url}")


# 事件快照 / 实时映射逻辑已迁移到 app.api.publisher（先持久化再推送）


#根据入参 `repo_source` 选择仓库实现
def _build_provider(req: CreateRunRequest):
    """根据请求构建 RepositoryProvider。"""
    from app.repository import LocalRepositoryProvider

    if req.repo_source == "github":
        if not req.github_url:
            raise HTTPException(400, "github_url 是 github 模式的必填参数")
        from app.repository import GitHubRepositoryProvider
        owner, repo = _parse_github_url(req.github_url)
        return GitHubRepositoryProvider(owner=owner, repo=repo)

    # local 模式
    path = req.repo_path or settings.repo.default_local_path
    if not path:
        path = os.getcwd()
    rid = settings.repo.default_repository_id
    # 本地目录名作为 repository_id
    return LocalRepositoryProvider(path, repository_id=rid)


# 任务执行逻辑已迁移到 app.api.run_executor（统一首次执行 / checkpoint 恢复）


# --------------------------------------------------------------------------- Routes


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """健康检查。"""
    return HealthResponse(
        python_version=sys.version,
        repository_id=settings.repo.default_repository_id,
        github_mcp_available=True,  # mcp SDK 已安装
    )


# --- 运行时配置：允许前端动态更新，不用重启 ---

class UpdateConfigRequest(BaseModel):
    github_token: str | None = Field(None, description="GitHub Personal Access Token，留空表示清除")

class ConfigResponse(BaseModel):
    github_token_set: bool      # 是否已配置 token
    github_token_masked: str   # 脱敏显示（前6后4）


def _mask_token(token: str) -> str:
    if not token:
        return "（未配置）"
    if len(token) <= 10:
        return "****"
    return token[:6] + "****" + token[-4:]


def _update_env_file(key: str, value: str) -> None:
    """把 key=value 写进 .env 文件（已存在则覆盖，不存在则追加）。

    路径锚定到项目根目录（main.py 位于 app/api/ 下，parents[2] 即项目根），
    不能用 Path.cwd() —— 它取决于启动后端时的工作目录，
    工作目录不对就会把 .env 写歪到别处（比如 app/api/.env）。
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.get("/api/config", response_model=ConfigResponse)
async def get_config():
    """读取当前运行时配置（脱敏）。"""
    token = settings.github.token
    return ConfigResponse(
        github_token_set=bool(token),
        github_token_masked=_mask_token(token),
    )


@app.post("/api/config", response_model=ConfigResponse)
async def update_config(req: UpdateConfigRequest):
    """更新运行时配置（立即生效 + 写入 .env 持久化）。

    目前只支持动态更新 ``github_token``。下次创建 GitHub 仓库分析任务时，
    新建的 MCP 连接会自动使用新 token —— **不需要重启后端**。
    """
    new_token = (req.github_token or "").strip()
    # 1. 更新内存配置
    settings.github.token = new_token
    os.environ["GITHUB_TOKEN"] = new_token
    # 2. 持久化到 .env 文件
    _update_env_file("GITHUB_TOKEN", new_token)

    token_display = _mask_token(new_token) if new_token else "（已清除）"
    return ConfigResponse(
        github_token_set=bool(new_token),
        github_token_masked=token_display,
    )


@app.post("/api/runs", response_model=CreateRunResponse)
async def create_run(req: CreateRunRequest):
    """创建分析任务（在后台线程跑，不等待完成）。

    结果通过 SSE（/events）或轮询（GET /runs/{id}）获取 —— 前后端职责分离。
    """
    provider = _build_provider(req)
    repo_identifier = req.github_url or req.repo_path or settings.repo.default_local_path or ""

    state = run_manager.create_run(
        question=req.question,
        repo_source=req.repo_source,
        repo_identifier=repo_identifier,
        repository_id=provider.repository_id,
        strategy=req.strategy,
    )
    state.thread_id = execution_thread_id(state.run_id)

    # 写入 runs 表（DB 连不上不影响主流程，降级为内存状态）
    # thread_id / requested_* 落库是断点恢复的前提：恢复调度器只认数据库里的关联
    try:
        from app.db import SessionLocal
        from app.db.runs import RunDAO
        with SessionLocal() as _s:
            RunDAO(_s).create(
                run_id=state.run_id,
                repo_source=req.repo_source,
                repo_identifier=repo_identifier,
                question=req.question,
                repository_id=provider.repository_id,
                thread_id=state.thread_id,
                requested_strategy=req.strategy,
                requested_thread_id=req.thread_id,
                user_id=req.user_id,
            )
    except Exception as _dbe:
        print(f"[create_run] WARNING: DB insert failed: {_dbe}")

    bridge = run_manager.bridge(state.run_id)
    if bridge is None:
        raise HTTPException(500, "任务初始化失败")

    # 统一执行入口：首次执行与恢复共用 execute_run。
    # 认领（pending → running）在 execute_run 内部用条件 UPDATE 原子完成。
    from app.api.run_executor import execute_run

    run_manager.submit(state.run_id, execute_run, state.run_id,
                       resume=False, provider=provider)

    return CreateRunResponse(
        run_id=state.run_id,
        status=state.status,
        repository_id=state.repository_id,
    )


TERMINAL_STATUSES = ("completed", "failed", "cancelled", "success", "partial")


def _load_run_from_db(run_id: str):
    """同步查库（在线程池里调用，避免阻塞事件循环）。"""
    try:
        from app.db import SessionLocal
        from app.db.runs import RunDAO

        with SessionLocal() as session:
            return RunDAO(session).get(run_id)
    except Exception:  # noqa: BLE001
        return None


def _db_status(run_id: str) -> str | None:
    record = _load_run_from_db(run_id)
    return record.status if record is not None else None


@app.get("/api/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str):
    """查询任务状态。内存没有时回落到数据库（服务重启后的任务仍可查询）。"""
    state = run_manager.get_state(run_id)
    if state is not None:
        return RunStatusResponse(
            run_id=state.run_id,
            status=state.status,
            question=state.question[:200],
            repo_source=state.repo_source,
            repo_identifier=state.repo_identifier,
            repository_id=state.repository_id,
            strategy=state.strategy,
            answer=state.answer,
            error=state.error,
            elapsed_ms=state.elapsed_ms(),
        )

    record = await asyncio.to_thread(_load_run_from_db, run_id)
    if record is None:
        raise HTTPException(404, f"run {run_id} not found")
    elapsed_ms = None
    if record.duration_ms is not None:
        elapsed_ms = record.duration_ms
    elif record.created_at is not None:
        end = record.updated_at or datetime.now(timezone.utc).replace(tzinfo=None)
        elapsed_ms = max(0.0, (end - record.created_at).total_seconds() * 1000)
    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status,
        question=(record.question or "")[:200],
        repo_source=record.repo_source,
        repo_identifier=record.repo_identifier or "",
        repository_id=record.repository_id or "",
        strategy=record.strategy,
        answer=record.answer or "",
        error=record.last_error,
        elapsed_ms=elapsed_ms,
    )


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """请求取消任务。终态任务不会被改写（数据库条件更新兜底）。"""
    ok = run_manager.cancel_run(run_id)
    if not ok:
        raise HTTPException(404, f"run {run_id} not found or already finished")
    return {"run_id": run_id, "cancelled": True}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request,
                     after: int | None = None,
                     last_sequence: int | None = None):
    """SSE 事件流，支持断线续传。

    续传游标优先级：``?after=`` > ``?last_sequence=`` > ``Last-Event-ID`` 头 > -1。
    服务端先补发 run_events 中 sequence > cursor 的历史事件，再衔接实时队列；
    两边都按 sequence 去重，避免补发与实时交叉导致重复/遗漏。
    """
    from app.api.publisher import RunEventPublisher

    state = run_manager.get_state(run_id)
    publisher = run_manager.publisher(run_id)
    if state is None and publisher is None:
        record = await asyncio.to_thread(_load_run_from_db, run_id)
        if record is None:
            raise HTTPException(404, f"run {run_id} not found")

    cursor = after if after is not None else last_sequence
    if cursor is None:
        header = request.headers.get("last-event-id")
        try:
            cursor = int(header) if header else -1
        except (TypeError, ValueError):
            cursor = -1
    replayer = publisher or RunEventPublisher(run_id)

    async def _replay(after: int):
        return await asyncio.to_thread(replayer.replay, after)

    def _drain():
        q = run_manager.queue(run_id)
        if q is None:
            return []
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue_module.Empty:
                return out

    async def _status():
        s = run_manager.get_state(run_id)
        if s is not None and s.status in TERMINAL_STATUSES:
            return s.status
        # 内存里是 running/recovering 时也查一次库：可能被其他进程取消或已由新 Worker 结束
        db_status = await asyncio.to_thread(_db_status, run_id)
        if db_status:
            return db_status
        return s.status if s is not None else None

    from app.api.sse import stream_events

    return EventSourceResponse(
        stream_events(run_id, cursor, _replay, _drain, _status,
                      is_disconnected=request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def run_server(host: str | None = None, port: int | None = None, **kwargs) -> None:
    """启动 uvicorn。"""
    import uvicorn

    h = host or settings.web.host
    p = port or settings.web.port
    uvicorn.run("app.api.main:app", host=h, port=p, reload=False, **kwargs)


if __name__ == "__main__":
    run_server()
