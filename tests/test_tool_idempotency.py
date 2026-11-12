"""故障注入测试三：工具执行完成、checkpoint 尚未保存时崩溃，恢复后不得重复副作用。"""
from __future__ import annotations

from typing import ClassVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.db.tool_executions import ToolExecutionStore
from app.tools.idempotency import (
    GuardedTool,
    guard_tool,
    make_idempotency_key,
    tool_execution_context,
)


class _SaveArgs(BaseModel):
    key: str


class _FakeSaveTool(BaseTool):
    name: str = "save_long_term_memory"
    description: str = "fake side-effecting tool"
    args_schema: type[BaseModel] = _SaveArgs
    effects: ClassVar[list[str]] = []

    def _run(self, key: str) -> str:
        type(self).effects.append(key)
        return f"saved:{key}"


class _ReadTool(BaseTool):
    name: str = "read_file"
    description: str = "read only"
    args_schema: type[BaseModel] = _SaveArgs
    reads: ClassVar[int] = 0

    def _run(self, key: str) -> str:
        type(self).reads += 1
        return f"content:{key}"


def test_side_effect_tool_replays_cached_result_after_restart(session_factory):
    _FakeSaveTool.effects.clear()
    store = ToolExecutionStore(session_factory)
    tool = GuardedTool(_FakeSaveTool(), store=store)

    # 第一次执行（崩溃前）
    with tool_execution_context("run-1", store):
        assert tool._run(key="hello") == "saved:hello"
        # 同一 run 内重复调用也命中缓存
        assert tool._run(key="hello") == "saved:hello"
    assert _FakeSaveTool.effects == ["hello"]

    # 模拟重启：新工具实例、新上下文、同一个 run_id（checkpoint 丢失导致重跑工具）
    tool2 = GuardedTool(_FakeSaveTool(), store=ToolExecutionStore(session_factory))
    with tool_execution_context("run-1", ToolExecutionStore(session_factory)):
        assert tool2._run(key="hello") == "saved:hello"

    assert _FakeSaveTool.effects == ["hello"]  # 副作用只发生一次

    # 不同 run 是独立的业务调用，允许再次执行
    with tool_execution_context("run-2", store):
        assert tool2._run(key="hello") == "saved:hello"
    assert _FakeSaveTool.effects == ["hello", "hello"]


def test_read_only_tool_is_not_wrapped_and_can_rerun(session_factory):
    _ReadTool.reads = 0
    tool = guard_tool(_ReadTool(), ToolExecutionStore(session_factory))
    assert isinstance(tool, _ReadTool)  # 只读工具保持原样

    with tool_execution_context("run-1", ToolExecutionStore(session_factory)):
        tool._run(key="a")
        tool._run(key="a")
    assert _ReadTool.reads == 2  # 允许重复读取


def test_idempotency_key_is_stable_and_arg_sensitive():
    k1 = make_idempotency_key("run-1", "save_long_term_memory", {"key": "a", "x": 1})
    k2 = make_idempotency_key("run-1", "save_long_term_memory", {"x": 1, "key": "a"})
    k3 = make_idempotency_key("run-1", "save_long_term_memory", {"key": "b", "x": 1})
    assert k1 == k2
    assert k1 != k3
