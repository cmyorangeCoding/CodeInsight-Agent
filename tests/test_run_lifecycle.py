"""故障注入测试四 / 六：任务认领、租约、并发恢复与终态保护。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from sqlalchemy import update

from app.api.recovery import RecoveryScheduler
from app.db.runs import Run, RunDAO, utcnow


def _create(factory, run_id: str, **overrides):
    with factory() as session:
        RunDAO(session).create(
            run_id=run_id,
            task_id=f"task-{run_id}",
            repo_source="local",
            repo_identifier=".",
            question="分析代码",
            repository_id="repo-1",
            thread_id=f"task-{run_id}",
            requested_strategy=overrides.pop("requested_strategy", "react"),
        )
        overrides.setdefault("status", "pending")
        session.execute(update(Run).where(Run.run_id == run_id).values(**overrides))
        session.commit()


def _patch(factory, run_id: str, **values):
    with factory() as session:
        session.execute(update(Run).where(Run.run_id == run_id).values(**values))
        session.commit()


def _get(factory, run_id: str) -> dict:
    with factory() as session:
        row = session.query(Run).filter(Run.run_id == run_id).first()
        return {
            "status": row.status,
            "worker_id": row.worker_id,
            "attempt": row.attempt,
            "lease_until": row.lease_until,
            "last_error": row.last_error,
        }


def test_two_workers_only_one_claims(session_factory):
    _create(session_factory, "run-race")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker(name: str):
        with session_factory() as session:
            barrier.wait()
            results.append(RunDAO(session).claim("run-race", name, 60))

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    row = _get(session_factory, "run-race")
    assert row["status"] == "running"
    assert row["attempt"] == 1
    assert row["worker_id"] in ("w0", "w1")


def test_expired_lease_allows_takeover_and_old_worker_cannot_finish(session_factory):
    _create(session_factory, "run-lease")
    with session_factory() as session:
        assert RunDAO(session).claim("run-lease", "old", 60) is True
        assert RunDAO(session).claim("run-lease", "new", 60) is False  # 租约未过期

    _patch(session_factory, "run-lease", lease_until=utcnow() - timedelta(seconds=1))
    with session_factory() as session:
        assert RunDAO(session).claim("run-lease", "new", 60) is True

    with session_factory() as session:
        # 旧 Worker 延迟结束：worker_id 不匹配，不能覆盖新 Worker
        assert RunDAO(session).finish("run-lease", "old", "completed", answer="old") is False
        # 新 Worker 可以正常结束
        assert RunDAO(session).finish("run-lease", "new", "completed", answer="new") is True

    row = _get(session_factory, "run-lease")
    assert row["status"] == "completed"
    assert row["worker_id"] == "new"


def test_terminal_states_never_claimable(session_factory):
    for status in ("completed", "failed", "cancelled"):
        run_id = f"run-{status}"
        _create(session_factory, run_id, status=status)
        with session_factory() as session:
            assert RunDAO(session).claim(run_id, "w", 60) is False


def test_cancel_and_complete_race_preserves_first_terminal(session_factory):
    _create(session_factory, "run-cancel")
    with session_factory() as session:
        assert RunDAO(session).claim("run-cancel", "w1", 60) is True
    with session_factory() as session:
        assert RunDAO(session).request_cancel("run-cancel") is True
    with session_factory() as session:
        # 完成写入晚到：条件更新发现已是终态，不允许覆盖
        assert RunDAO(session).finish("run-cancel", "w1", "completed") is False
    assert _get(session_factory, "run-cancel")["status"] == "cancelled"


# ---------------------------------------------------------------------------
# RecoveryScheduler
# ---------------------------------------------------------------------------
def _scheduler(session_factory, checkpoints: set[str], events: dict[str, int]):
    submitted: list[tuple[str, bool]] = []
    scheduler = RecoveryScheduler(
        session_factory=session_factory,
        checkpoint_probe=lambda tid: tid in checkpoints,
        event_counter=lambda rid: events.get(rid, 0),
        submitter=lambda rid, resume: submitted.append((rid, resume)),
        batch=50,
    )
    return scheduler, submitted


def test_recovery_scheduler_decides_resume_fresh_or_fail(session_factory):
    now = utcnow()
    expired = now - timedelta(seconds=5)
    # 1) 有 checkpoint + react → 续跑
    _create(session_factory, "run-resume", status="running", strategy="react",
            attempt=1, lease_until=expired)
    # 2) 无 checkpoint、无事件、从未启动 → 安全首次执行
    _create(session_factory, "run-fresh", status="running", attempt=1,
            lease_until=expired)
    # 3) 无 checkpoint 但已有事件 → 不可恢复
    _create(session_factory, "run-no-ckpt", status="running", attempt=1,
            lease_until=expired)
    # 4) plan 策略 + 任务线程有 checkpoint（可能来自上一轮）→ 交给执行器判定
    _create(session_factory, "run-plan", status="running", strategy="plan_execute",
            attempt=1, lease_until=expired)
    # 5) 旧版本遗留（task_id / thread_id 都为空）→ 不可恢复
    _create(session_factory, "run-legacy", status="running", attempt=1,
            lease_until=expired, task_id=None, thread_id=None)
    # 6) 租约仍有效 → 原 Worker 可能还活着，不能动
    _create(session_factory, "run-alive", status="running", attempt=1,
            lease_until=now + timedelta(seconds=60))
    # 7) 终态任务 → 不扫描
    _create(session_factory, "run-done", status="completed", attempt=1)

    checkpoints = {"task-run-resume", "task-run-plan"}
    scheduler, submitted = _scheduler(
        session_factory, checkpoints, {"run-no-ckpt": 3}
    )
    summary = scheduler.run_once()

    assert ("run-resume", True) in submitted
    assert ("run-plan", True) in submitted
    assert ("run-fresh", False) in submitted
    assert all(rid not in ("run-legacy", "run-no-ckpt", "run-alive",
                           "run-done") for rid, _ in submitted)
    assert summary["failed"] == 2  # legacy / no-ckpt
    assert summary["recovered"] == 3
    assert "缺少 task_id/thread_id" in _get(session_factory, "run-legacy")["last_error"]
    assert "已有执行事件" in _get(session_factory, "run-no-ckpt")["last_error"]


def test_repeated_scheduler_runs_do_not_submit_twice(session_factory):
    expired = utcnow() - timedelta(seconds=5)
    _create(session_factory, "run-once", status="running", strategy="react",
            attempt=1, lease_until=expired)
    scheduler, submitted = _scheduler(
        session_factory, {"task-run-once"}, {}
    )
    first = scheduler.run_once()
    second = scheduler.run_once()

    assert first["recovered"] == 1
    assert second["recovered"] == 0  # 已认领且租约有效
    assert submitted == [("run-once", True)]


def test_recovering_run_can_be_recovered_again_after_restart(session_factory):
    _create(session_factory, "run-recovering", status="recovering", strategy="react",
            attempt=2, lease_until=utcnow() - timedelta(seconds=1))
    scheduler, submitted = _scheduler(session_factory, {"task-run-recovering"}, {})
    assert scheduler.run_once()["recovered"] == 1
    assert submitted == [("run-recovering", True)]


def test_task_groups_turns_and_limits_one_active_turn(session_factory):
    """同一 task_id 的多轮归在一起；有未结束轮次时不允许再开新一轮。"""
    _create(session_factory, "run-a", task_id="task-shared")
    _create(session_factory, "run-b", task_id="task-shared")
    _create(session_factory, "run-c", task_id="task-other")

    with session_factory() as session:
        dao = RunDAO(session)
        assert [r.run_id for r in dao.list_by_task("task-shared")] == ["run-a", "run-b"]
        assert dao.has_active_run("task-shared") is True
        tasks = {t["task_id"]: t for t in dao.list_tasks()}
        assert tasks["task-shared"]["run_count"] == 2
        assert tasks["task-shared"]["title"] == "分析代码"
        assert tasks["task-other"]["run_count"] == 1

    _patch(session_factory, "run-a", status="completed")
    _patch(session_factory, "run-b", status="failed")
    with session_factory() as session:
        assert RunDAO(session).has_active_run("task-shared") is False

