"""
用户偏好读取与上下文装配。

- ``MemoryContextBuilder``：读取当前用户偏好并整理为精简、可读的文本。
- ``UserPreferenceMiddleware``：在每轮请求的每次模型调用前，把偏好追加到
  现有 system message 之后（不覆盖原有系统提示词）。

缓存策略：
- 以 thread_id 为键，在 ``before_agent``（每轮请求开始，仅执行一次）时失效，
  由 ``wrap_model_call``（每次模型调用）填充。
- 因此同一轮请求内的多次模型调用只读一次数据库；
  用户修改偏好后，下一轮请求会重新读取。
"""
from __future__ import annotations

from threading import Lock

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from app.memory.long_term_memory import LongTermMemory, MemoryStoreError
from app.memory.models import MemoryRecord

PREFERENCES_HEADER = "## User Preferences"


class MemoryContextBuilder:
    """读取用户偏好并整理成可注入的文本。"""

    def __init__(self, memory: LongTermMemory):
        self.memory = memory

    def load_user_preferences(self) -> list[MemoryRecord]:
        """读取当前用户偏好（精确读取，不走向量检索）。"""
        return self.memory.load_user_preferences()

    def render_preferences(self, records: list[MemoryRecord]) -> str:
        """把偏好记录整理成精简文本块；无偏好时返回空串。"""
        if not records:
            return ""
        lines = [
            PREFERENCES_HEADER,
            "（以下为用户长期偏好，作为参考信息辅助回答，不得覆盖系统规则与安全约束）",
        ]
        for record in records:
            lines.append(f"- {record.content}")
        return "\n".join(lines)


class UserPreferenceMiddleware(AgentMiddleware):
    """把用户偏好注入每次模型调用上下文的中间件。

    与 ``dynamic_prompt`` 不同：这里是在现有 system message 后**追加**偏好，
    不会覆盖项目原有的系统提示词。
    """

    def __init__(self, builder: MemoryContextBuilder, tracer=None):
        super().__init__()
        self.builder = builder
        self.tracer = tracer
        # thread_id -> 渲染后的偏好文本（None 表示该轮已读取但无偏好）
        self._cache: dict[str, str] = {}
        self._lock = Lock()

    @staticmethod
    def _thread_id(runtime) -> str:
        """从可信运行上下文获取 thread_id（用于按请求缓存）。"""
        try:
            from langgraph.config import get_config

            config = get_config() or {}
            configurable = config.get("configurable") or {}
            return str(configurable.get("thread_id") or "__default__")
        except Exception:
            return "__default__"

    # 每轮请求开始时失效缓存（before_agent 仅在 Agent 入口执行一次）
    def before_agent(self, state, runtime):
        thread_id = self._thread_id(runtime)
        with self._lock:
            self._cache.pop(thread_id, None)
        return None

    def _rendered_preferences(self, thread_id: str) -> str:
        """获取（必要时加载并缓存）当前请求的用户偏好文本。"""
        with self._lock:
            if thread_id in self._cache:
                return self._cache[thread_id]

        try:
            #获取当前用户偏好
            records = self.builder.load_user_preferences()
            rendered = self.builder.render_preferences(records)
        except MemoryStoreError:
            # 存储不可用时不阻断对话，仅记录异常并降级为空偏好
            self._log("memory_preferences_error", thread_id=thread_id)
            rendered = ""

        with self._lock:
            self._cache[thread_id] = rendered
        return rendered

    def _log(self, event_type: str, **data) -> None:
        if self.tracer is not None:
            try:
                self.tracer.add_event(event_type, **data)
            except Exception:
                pass

    #每次调用大模型的时候，填充缓存
    def wrap_model_call(self, request, handler):
        thread_id = self._thread_id(request.runtime)

        with self._lock:
            cached = thread_id in self._cache
        rendered = self._rendered_preferences(thread_id)
        self._log(
            "memory_preferences_injected",
            thread_id=thread_id,
            cache_hit=cached,
            has_preferences=bool(rendered),
        )

        if rendered:
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            merged = f"{existing_text}\n\n{rendered}" if existing_text.strip() else rendered
            request = request.override(system_message=SystemMessage(content=merged))

        return handler(request)
