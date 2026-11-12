"""故障注入测试一：LangGraph checkpoint 恢复语义。

不依赖数据库的部分用 InMemorySaver 验证「崩溃 → 新执行环境 → 同一 thread 续跑」；
配置了可用 PostgreSQL 时，额外用 PostgresSaver 验证跨 Saver 实例的持久化恢复。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.orchestration.executor import Orchestrator
from app.observability.tracer import Tracer


class _State(dict):
    pass


def _build_graph(checkpointer, log: list[str], fail_once: dict):
    def node_a(state):
        log.append("A")
        return {"steps": state.get("steps", []) + ["A"]}

    def node_b(state):
        if fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("simulated process crash")
        log.append("B")
        return {"steps": state.get("steps", []) + ["B"]}

    def node_c(state):
        log.append("C")
        return {"steps": state.get("steps", []) + ["C"]}

    graph = StateGraph(dict)
    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_node("c", node_c)
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_edge("c", END)
    return graph.compile(checkpointer=checkpointer)


def test_resume_from_checkpoint_after_simulated_crash():
    log: list[str] = []
    fail_once = {"value": True}
    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "run-crash-1"}}

    graph = _build_graph(saver, log, fail_once)
    with pytest.raises(RuntimeError):
        graph.invoke({"steps": []}, config=cfg)

    # 崩溃后：A 已完成并落 checkpoint，B 是待执行任务
    state = graph.get_state(cfg)
    assert state.next  # 仍有未完成的 super-step
    assert log == ["A"]

    # 模拟「新进程」：全新 graph 对象，但相同 checkpointer + thread_id
    log.clear()
    graph2 = _build_graph(saver, log, fail_once)
    result = graph2.invoke(None, config=cfg)

    # 只续跑 B、C，不会重新执行 A
    assert log == ["B", "C"]
    assert result["steps"] == ["A", "B", "C"]

    # 已完成的图再 invoke(None)：不发新输入就不会开启新一轮
    log.clear()
    same = graph2.invoke(None, config=cfg)
    assert log == []
    assert same["steps"] == ["A", "B", "C"]


def test_thread_isolation_between_runs():
    log: list[str] = []
    saver = InMemorySaver()
    fail_once = {"value": False}
    graph = _build_graph(saver, log, fail_once)

    graph.invoke({"steps": []}, config={"configurable": {"thread_id": "run-a"}})
    graph2 = _build_graph(saver, log, fail_once)
    state_b = graph2.get_state({"configurable": {"thread_id": "run-b"}})
    assert not state_b.next
    assert state_b.values == {}


# ---------------------------------------------------------------------------
# Orchestrator 的恢复分支：必须区分「续跑 / 已跑完 / 找不到 checkpoint」
# ---------------------------------------------------------------------------
class _FakeAgent:
    def __init__(self, state):
        self.state = state
        self.invoke_inputs: list = []

    def get_state(self, config):
        return self.state

    def invoke(self, payload, config=None):
        self.invoke_inputs.append(payload)
        return {"messages": [AIMessage(content="resumed answer")]}


def _make_orchestrator(agent):
    return Orchestrator(agent_factory=lambda mode: agent, tracer=Tracer())


def test_orchestrator_resume_with_pending_step_calls_invoke_none():
    agent = _FakeAgent(SimpleNamespace(next=("model",), values={"messages": []}))
    answer = _make_orchestrator(agent)._run_single(
        "q", "react", "run-1", resume=True
    )
    assert answer == "resumed answer"
    assert agent.invoke_inputs == [None]  # 关键：不是重新传入问题


def test_orchestrator_resume_finished_graph_does_not_rerun():
    agent = _FakeAgent(SimpleNamespace(
        next=(),
        values={"messages": [HumanMessage(content="q"), AIMessage(content="already done")]},
    ))
    answer = _make_orchestrator(agent)._run_single(
        "q", "direct", "run-1", resume=True
    )
    assert answer == "already done"
    assert agent.invoke_inputs == []  # 没有开启任何新执行


def test_orchestrator_resume_without_checkpoint_raises():
    agent = _FakeAgent(None)
    with pytest.raises(RuntimeError):
        _make_orchestrator(agent)._run_single("q", "react", "run-1", resume=True)


# ---------------------------------------------------------------------------
# 可选：真实 PostgreSQL PostgresSaver 跨实例恢复
# ---------------------------------------------------------------------------
def _postgres_saver_or_skip():
    from app.db import engine

    if engine is None:
        pytest.skip("DATABASE_URL 未配置，跳过 PostgreSQL checkpoint 集成测试")
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    from app.config import settings

    try:
        psycopg.connect(settings.db.url, connect_timeout=2).close()
    except Exception:  # noqa: BLE001
        pytest.skip("PostgreSQL 不可用，跳过集成测试")
    return PostgresSaver.from_conn_string(settings.db.url)


def test_postgres_saver_recovers_across_saver_instances():
    conn_cm = _postgres_saver_or_skip()
    log: list[str] = []
    fail_once = {"value": True}
    cfg = {"configurable": {"thread_id": "run-pg-recovery-test"}}

    with conn_cm as saver:
        saver.setup()
        graph = _build_graph(saver, log, fail_once)
        with pytest.raises(RuntimeError):
            graph.invoke({"steps": []}, config=cfg)

    # 新连接 / 新 Saver 实例 ≈ 重启后的服务进程
    with _postgres_saver_or_skip() as saver2:
        log.clear()
        graph2 = _build_graph(saver2, log, fail_once)
        result = graph2.invoke(None, config=cfg)
        assert log == ["B", "C"]
        assert result["steps"] == ["A", "B", "C"]
