"""故障注入测试二：真实进程 kill -9 → 重启 → RecoveryScheduler 自动接管。

默认跳过（需要 PostgreSQL、可用 LLM 凭据，且会启动真实 uvicorn 进程）。
启用方式：
    PowerShell: $env:RUN_RECOVERY_IT="1"; pytest tests/test_fault_injection_integration.py -q
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_RECOVERY_IT") != "1",
    reason="需要 RUN_RECOVERY_IT=1 + 可用 PostgreSQL/LLM，默认跳过",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("RUN_RECOVERY_IT_PORT", "8231"))
BASE = f"http://127.0.0.1:{PORT}"


def _db_connect_or_skip():
    import psycopg

    from app.config import settings

    try:
        return psycopg.connect(settings.db.url, connect_timeout=3)
    except Exception:  # noqa: BLE001
        pytest.skip("PostgreSQL 不可用，跳过进程级故障注入测试")


def _http_json(path: str, method: str = "GET", payload: dict | None = None,
               timeout: float = 10.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_health(proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early: {proc.returncode}")
        try:
            _http_json("/api/health", timeout=2)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise TimeoutError("server not ready")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int, log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["RECOVERY_INTERVAL_SECONDS"] = "1"
    env["RUN_LEASE_SECONDS"] = "6"
    log = log_path.open("ab")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env, stdout=log, stderr=log,
    )


def _make_tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tinyrepo"
    repo.mkdir()
    (repo / "service.py").write_text(
        "def load_user(user_id):\n"
        "    return {'id': user_id, 'name': 'demo'}\n\n"
        "def render_user(user):\n"
        "    return f\"{user['id']}:{user['name']}\"\n",
        encoding="utf-8",
    )
    (repo / "api.py").write_text(
        "from service import load_user, render_user\n\n"
        "def handler(user_id):\n"
        "    return render_user(load_user(user_id))\n",
        encoding="utf-8",
    )
    return repo


def test_kill_restart_recovers_run_from_checkpoint(tmp_path):
    conn = _db_connect_or_skip()
    port = _free_port()
    global BASE
    BASE = f"http://127.0.0.1:{port}"
    repo = _make_tiny_repo(tmp_path)
    log_path = tmp_path / "server.log"

    proc = _start_server(port, log_path)
    try:
        _wait_health(proc)
        created = _http_json("/api/runs", "POST", {
            "question": "请用 read_file 依次阅读 service.py 和 api.py，说明 handler 的完整调用链。",
            "repo_source": "local",
            "repo_path": str(repo),
            "strategy": "react",
            "user_id": "it-user",
        })
        run_id = created["run_id"]
        thread_id = f"run-{run_id}"

        # 等待 Agent 真正开始执行并写入 checkpoint
        deadline = time.time() + 180
        checkpoint_seen = False
        while time.time() < deadline:
            row = conn.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,)
            ).fetchone()
            checkpoint_seen = bool(row and row[0])
            if checkpoint_seen:
                break
            status = _http_json(f"/api/runs/{run_id}")["status"]
            if status in ("completed", "failed", "cancelled"):
                pytest.skip(f"任务在注入故障前已结束（{status}），本次无法验证恢复")
            time.sleep(0.5)
        assert checkpoint_seen, "未观察到 checkpoint，无法验证断点恢复"

        time.sleep(3)  # 让执行推进到下一个 checkpoint 之前
        proc.kill()    # 模拟进程被强制终止（非优雅退出）
        proc.wait(timeout=15)

        proc2 = _start_server(port, log_path)
        try:
            _wait_health(proc2)
            deadline = time.time() + 300
            status = "unknown"
            while time.time() < deadline:
                status = _http_json(f"/api/runs/{run_id}")["status"]
                if status in ("completed", "failed", "cancelled"):
                    break
                time.sleep(1)
            assert status == "completed", f"恢复后任务未成功：{status}\n{log_path.read_text()}"

            row = conn.execute(
                "SELECT attempt, worker_id FROM runs WHERE run_id = %s", (run_id,)
            ).fetchone()
            assert row[0] >= 2, "恢复后 attempt 应递增"

            # 运行事件里必须出现 resume=true 的执行轮次（证明是续跑而非重新跑一遍）
            resumed = conn.execute(
                "SELECT count(*) FROM run_events WHERE run_id = %s "
                "AND event_type = 'run_started' AND data LIKE %s",
                (run_id, '%"resume": true%'),
            ).fetchone()[0]
            assert resumed >= 1, "未找到 resume=true 的执行事件"

            # 事件序号连续，SSE 可从任意游标续传
            seqs = [r[0] for r in conn.execute(
                "SELECT sequence FROM run_events WHERE run_id = %s ORDER BY sequence",
                (run_id,),
            ).fetchall()]
            assert seqs == list(range(len(seqs))), "run_events 序号不连续"

            # 从 sequence=2 重连 SSE，应补发历史并最终收到 run_status 终止事件
            req = urllib.request.Request(
                f"{BASE}/api/runs/{run_id}/events?after=2",
                headers={"Accept": "text/event-stream"},
            )
            events, last_id = [], -1
            with urllib.request.urlopen(req, timeout=30) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("id:"):
                        last_id = int(line.split(":", 1)[1].strip())
                        assert last_id > 0
                    if line.startswith("event:"):
                        events.append(line.split(":", 1)[1].strip())
            assert "run_status" in events, "SSE 未发送终止事件"
            assert last_id >= 2, "SSE 未补发历史事件"
        finally:
            proc2.kill()
            proc2.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        conn.close()
