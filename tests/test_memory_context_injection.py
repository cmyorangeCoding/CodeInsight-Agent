"""
用户偏好自动注入测试（任务书 B）。

使用可控的 ScriptedChatModel（记录每次模型调用的完整消息），验证：
1. 用户偏好自动注入到模型上下文；
2. 没有偏好记录时仍能正常回答；
3. 不同用户读取各自偏好（隔离）；
4. 同一轮 Agent 的多次模型调用都能拿到偏好；
5. 偏好更新后，后续请求读取到新内容；
6. 注入不会覆盖原有系统提示词。

运行方式：python tests/test_memory_context_injection.py
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

BASE_PROMPT = "BASE SYSTEM PROMPT: 你是代码仓库分析助手。"


class ScriptedChatModel(BaseChatModel):
    """第一次调用返回工具调用，之后返回最终答案；记录每次调用的消息。"""

    _captured: list = PrivateAttr(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._captured.append(messages)
        self._calls += 1
        if self._calls == 1:
            message = AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"text": "ping"}, "id": "call_1"}],
            )
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


@tool
def echo(text: str) -> str:
    """回显输入文本。"""
    return f"echo:{text}"


def _build_agent(memory, model):
    middleware = [UserPreferenceMiddleware(builder=MemoryContextBuilder(memory))]
    return create_agent(
        model=model,
        tools=[echo],
        system_prompt=BASE_PROMPT,
        middleware=middleware,
    )


def _joined(messages) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


def make_memory(user_id="u-1", project_id="p-1"):
    store = InMemoryStore()
    return store, LongTermMemory(store=store, user_id=user_id, project_id=project_id)


def test_preference_injected_into_every_model_call():
    _, memory = make_memory()
    memory.save(
        scope=MemoryScope.USER.value, category=MemoryCategory.PREFERENCE.value,
        memory_key="answer_style",
        content="回答风格：先给整体流程，再讲每个模块。",
        source=MemorySource.USER_STATED.value,
    )

    model = ScriptedChatModel()
    agent = _build_agent(memory, model)
    agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]},
        config={"configurable": {"thread_id": "t1"}},
    )

    assert len(model._captured) >= 2, "应至少发生两次模型调用（含工具调用后的二次调用）"
    for batch in model._captured:
        text = _joined(batch)
        assert "先给整体流程，再讲每个模块" in text, "每次模型调用都应带上用户偏好"
        assert BASE_PROMPT in text, "注入不得覆盖原有系统提示词"


def test_no_preference_still_answers():
    _, memory = make_memory()
    model = ScriptedChatModel()
    agent = _build_agent(memory, model)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]},
        config={"configurable": {"thread_id": "t2"}},
    )
    assert result["messages"], "无偏好时应能正常回答"
    assert "## User Preferences" not in _joined(model._captured[0])


def test_users_read_their_own_preferences():
    store = InMemoryStore()
    mem_a = LongTermMemory(store=store, user_id="alice", project_id="p-1")
    mem_b = LongTermMemory(store=store, user_id="bob", project_id="p-1")

    mem_a.save(scope=MemoryScope.USER.value, category=MemoryCategory.PREFERENCE.value,
               memory_key="style", content="ALICE_PREF：回答要非常详细。",
               source=MemorySource.USER_STATED.value)
    mem_b.save(scope=MemoryScope.USER.value, category=MemoryCategory.PREFERENCE.value,
               memory_key="style", content="BOB_PREF：回答要尽量简短。",
               source=MemorySource.USER_STATED.value)

    a_prefs = mem_a.load_user_preferences()
    b_prefs = mem_b.load_user_preferences()
    assert a_prefs[0].content == "ALICE_PREF：回答要非常详细。"
    assert b_prefs[0].content == "BOB_PREF：回答要尽量简短。"


def test_preference_update_visible_on_next_request():
    _, memory = make_memory()
    memory.save(scope=MemoryScope.USER.value, category=MemoryCategory.PREFERENCE.value,
                memory_key="style", content="旧偏好：用中文回答。",
                source=MemorySource.USER_STATED.value)

    model = ScriptedChatModel()
    agent = _build_agent(memory, model)
    agent.invoke(
        {"messages": [{"role": "user", "content": "第一问"}]},
        config={"configurable": {"thread_id": "t3"}},
    )
    assert "旧偏好：用中文回答。" in _joined(model._captured[0])

    # 更新偏好后，同一 thread 的下一轮请求应读取到新内容
    model._calls = 0
    model._captured = []
    memory.save(scope=MemoryScope.USER.value, category=MemoryCategory.PREFERENCE.value,
                memory_key="style", content="新偏好：先给结论再展开。",
                source=MemorySource.USER_STATED.value)

    agent.invoke(
        {"messages": [{"role": "user", "content": "第二问"}]},
        config={"configurable": {"thread_id": "t3"}},
    )
    text = _joined(model._captured[0])
    assert "新偏好：先给结论再展开。" in text
    assert "旧偏好：用中文回答。" not in text


def main():
    tests = [
        test_preference_injected_into_every_model_call,
        test_no_preference_still_answers,
        test_users_read_their_own_preferences,
        test_preference_update_visible_on_next_request,
    ]
    for test in tests:
        test()
        print(f"✓ {test.__name__}")
    print(f"\n全部通过：{len(tests)} 个用例")


if __name__ == "__main__":
    main()
