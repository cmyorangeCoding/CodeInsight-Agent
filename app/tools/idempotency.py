"""工具幂等保护。

只对「有外部副作用」的工具启用（当前是 save_long_term_memory）。
只读工具（read_file / search_code / search_code_rag / get_repo_tree）允许在恢复时重跑，
不进入幂等表，避免缓存导致读到旧数据。

幂等键 = run_id + tool_name + 参数 JSON 的 sha256。
不含 LLM 生成的 tool_call_id：恢复时模型会重新生成调用，id 不稳定，
而 run_id + 工具名 + 参数在「同一任务的同一逻辑调用」上是稳定的。
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from langchain_core.tools import BaseTool
from pydantic import ConfigDict, PrivateAttr

from app.db.tool_executions import ToolExecutionStore


# 明确声明有副作用的工具名；新增写工具时必须在这里登记
SIDE_EFFECT_TOOL_NAMES = {"save_long_term_memory"}


@dataclass
class ToolExecutionContext:
    run_id: str
    store: ToolExecutionStore


_ctx: ContextVar[ToolExecutionContext | None] = ContextVar("tool_execution_ctx", default=None)


@contextmanager
def tool_execution_context(run_id: str,
                           store: ToolExecutionStore | None = None) -> Iterator[None]:
    """在执行 Orchestrator 的线程里绑定 run_id，供工具包装器取用。"""
    token = _ctx.set(ToolExecutionContext(run_id=run_id,
                                          store=store or ToolExecutionStore()))
    try:
        yield
    finally:
        _ctx.reset(token)


def is_side_effect_tool(tool: BaseTool) -> bool:
    return (
        getattr(tool, "name", "") in SIDE_EFFECT_TOOL_NAMES
        or bool(getattr(tool, "side_effecting", False))
    )


def make_idempotency_key(run_id: str, tool_name: str, args: dict[str, Any]) -> str:
    payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{run_id}:{tool_name}:{digest}"


class GuardedTool(BaseTool):
    """包装一个有副作用的工具，执行前查幂等记录，命中 completed 直接回放结果。"""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    _wrapped: Any = PrivateAttr()
    _store: Any = PrivateAttr(default=None)

    def __init__(self, wrapped: BaseTool,
                 store: ToolExecutionStore | None = None):
        super().__init__(
            name=wrapped.name,
            description=wrapped.description,
            args_schema=getattr(wrapped, "args_schema", None),
        )
        self._wrapped = wrapped
        self._store = store

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        ctx = _ctx.get()
        if ctx is None:
            # 没有 run 上下文（单测 / 独立调用）→ 退化为普通工具
            return self._wrapped._run(*args, **kwargs)

        store = self._store or ctx.store
        key = make_idempotency_key(ctx.run_id, self.name, dict(kwargs))
        row = store.get(key)
        if row is not None and row.status == "completed" and row.result is not None:
            return row.result

        store.begin(key, ctx.run_id, self.name)
        try:
            result = self._wrapped._run(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            store.fail(key, f"{type(exc).__name__}: {exc}")
            raise
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        store.complete(key, text)
        return result


def guard_tool(tool: BaseTool,
               store: ToolExecutionStore | None = None) -> BaseTool:
    if is_side_effect_tool(tool):
        return GuardedTool(tool, store)
    return tool
