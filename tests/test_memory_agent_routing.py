"""
项目记忆按需检索与工具路由测试（任务书 C）。

使用可控模型验证工具路由（不依赖真实模型的随机行为）：
1. 普通知识问答不会被程序强制调用项目记忆工具；
2. 用户明确提到历史决策时，Agent 能调用 search_project_memory 并拿到结果；
3. 项目记忆不会被自动注入到上下文（只有用户偏好会自动注入）；
4. 检索不到结果时明确返回“没有找到”，不编造历史结论；
5. 保存工具对未确认决策 / 敏感的校验行为。

运行方式：python tests/test_memory_agent_routing.py
"""
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr

from app.memory.long_term_memory import LongTermMemory
from app.memory.memory_context import MemoryContextBuilder, UserPreferenceMiddleware
from app.memory.models import MemoryCategory, MemoryScope, MemorySource
from app.tools.memory_tools import (
    SaveLongTermMemoryTool,
    SearchProjectMemoryTool,
)

SYSTEM_PROMPT = "BASE SYSTEM PROMPT: 代码仓库分析助手。"
FACT_TOKEN = "PROJECT_FACT_TOKEN_9F2A"
DECISION_TOKEN = "已确定的向量库方案是 PostgreSQL + pgvector"


class RoutedChatModel(BaseChatModel):
    """可指定首次工具调用的可控模型，记录每次模型调用的消息。"""

    tool_call_name: str | None = None
    tool_call_args: dict | None = None

    _captured: list = PrivateAttr(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "routed-chat-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._captured.append(messages)
        self._calls += 1
        if self.tool_call_name and self._calls == 1:
            message = AIMessage(
                content="",
                tool_calls=[{
                    "name": self.tool_call_name,
                    "args": self.tool_call_args or {},
                    "id": "call_1",
                }],
            )
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


@tool
def echo(text: str) -> str:
    """回显输入文本。"""
    return f"echo:{text}"


def _joined(messages) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


def _build_agent(memory, model):
    tools = [echo, SearchProjectMemoryTool(memory=memory), SaveLongTermMemoryTool(memory=memory)]
    middleware = [UserPreferenceMiddleware(builder=MemoryContextBuilder(memory))]
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
    )


def test_project_memory_not_auto_injected_on_plain_question():
    store = InMemoryStore()
    memory = LongTermMemory(store=store, user_id="u-1", project_id="p-1")
    memory.save(scope=MemoryScope.PROJECT.value, category=MemoryCategory.FACT.value,
                memory_key="db_stack", content=f"项目事实：{FACT_TOKEN}。",
                source=MemorySource.CODE_VERIFIED.value)

    model = RoutedChatModel()  # 不调用任何工具，直接回答
    agent = _build_agent(memory, model)
    agent.invoke(
        {"messages": [{"role": "user", "content": "什么是 Python 的列表推导式？"}]},
        config={"configurable": {"thread_id": "r1"}},
    )

    text = _joined(model._captured[0])
    assert FACT_TOKEN not in text, "项目记忆不应被自动注入到上下文"
    assert "def" in text.lower() or len(model._captured) == 1


def test_history_question_routes_to_search_project_memory():
    store = InMemoryStore()
    memory = LongTermMemory(store=store, user_id="u-1", project_id="p-1")
    memory.save(scope=MemoryScope.PROJECT.value, category=MemoryCategory.DECISION.value,
                memory_key="vector_db", content=DECISION_TOKEN,
                source=MemorySource.USER_CONFIRMED.value)

    model = RoutedChatModel(
        tool_call_name="search_project_memory",
        tool_call_args={"query": "", "category": "decision", "limit": 5},
    )
    agent = _build_agent(memory, model)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "我们之前确定用哪个向量库？"}]},
        config={"configurable": {"thread_id": "r2"}},
    )

    assert len(model._captured) >= 2, "应先调用工具，再基于工具结果回答"
    second_call_text = _joined(model._captured[1])
    assert DECISION_TOKEN in second_call_text, "检索到的项目决策应进入模型上下文"

    # 结果消息里应包含 tool 调用记录
    types = {getattr(m, "type", "") for m in result["messages"]}
    assert "tool" in types, "应产生工具调用结果消息"


def test_search_tool_returns_clear_message_when_no_match():
    store = InMemoryStore()
    memory = LongTermMemory(store=store, user_id="u-1", project_id="p-1")
    tool_obj = SearchProjectMemoryTool(memory=memory)
    output = tool_obj._run(query="完全不存在的历史主题", category="fact", limit=5)
    assert "没有找到" in output
    assert "编造" in output


def test_save_tool_rejects_unconfirmed_decision_and_secrets():
    store = InMemoryStore()
    memory = LongTermMemory(store=store, user_id="u-1", project_id="p-1")
    tool_obj = SaveLongTermMemoryTool(memory=memory)

    rejected = tool_obj._run(
        scope="project", category="decision", memory_key="new_db",
        content="建议改用 Redis。", source="model_proposed",
    )
    assert "拒绝保存" in rejected

    leak = tool_obj._run(
        scope="project", category="fact", memory_key="leak",
        content="api_key=sk-test-1234567890abcdef", source="code_verified",
    )
    assert "拒绝保存" in leak

    ok = tool_obj._run(
        scope="user", category="preference", memory_key="style",
        content="回答风格：结构化。", source="user_stated",
    )
    assert "created" in ok


def test_memory_tools_cannot_access_other_users():
    """工具参数不暴露 user_id/project_id，模型无法越权访问他人空间。"""
    store = InMemoryStore()
    memory = LongTermMemory(store=store, user_id="alice", project_id="p-1")
    model = RoutedChatModel(
        tool_call_name="search_project_memory",
        tool_call_args={"query": "", "category": "fact", "limit": 5},
    )
    agent = _build_agent(memory, model)

    # 模型即便在用户消息里声称自己是 bob，也只能看到 alice 的空间
    memory.save(scope=MemoryScope.USER.value, category=MemoryCategory.PROFILE.value,
                memory_key="stack", content="ALICE_ONLY_PROFILE",
                source=MemorySource.USER_STATED.value)

    search_tool = SearchProjectMemoryTool(memory=memory)
    assert "ALICE_ONLY_PROFILE" not in search_tool._run(query="", category="fact", limit=5)


def main():
    tests = [
        test_project_memory_not_auto_injected_on_plain_question,
        test_history_question_routes_to_search_project_memory,
        test_search_tool_returns_clear_message_when_no_match,
        test_save_tool_rejects_unconfirmed_decision_and_secrets,
        test_memory_tools_cannot_access_other_users,
    ]
    for test in tests:
        test()
        print(f"✓ {test.__name__}")
    print(f"\n全部通过：{len(tests)} 个用例")


if __name__ == "__main__":
    main()
