
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.repository_agent import create_repository_agent
from app.observability.token_budget import TokenBudget
from app.observability.tracer import Tracer
from app.observability.callback import TraceCallback


def get_answer(result) -> str:
    """获取 Agent 最后一条 AI 回复。"""
    messages = result["messages"]

    for message in reversed(messages):
        if getattr(message, "type", "") == "ai":
            return str(message.content)

    return ""


def main():
    tracer = Tracer()

    budget = TokenBudget(
        max_total_tokens=30000,
        max_input_tokens=12000,
        reserved_output_tokens=2000,
    )

    # 显式创建一个 checkpointer，方便确认它在多个调用间被复用。
    checkpointer = InMemorySaver()

    agent = create_repository_agent(
        token_budget=budget,
        checkpointer=checkpointer,
    )

    callback = TraceCallback(
        tracer=tracer,
        token_budget=budget,
    )

    # 同一会话：第一次告诉 Agent 一个临时信息。
    print("\n===== 测试一：写入会话记忆 =====")

    agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "请记住本次会话的测试代号是 ORANGE-731。"
                        "这是会话测试信息，不是仓库代码事实。"
                        "请简短确认。"
                    ),
                }
            ]
        },
        config={
            "configurable": {"thread_id": "memory-test-001"},
            "callbacks": [callback],
        },
    )

    # 同一个 thread_id：不重复提供代号，检查能否回忆。
    print("\n===== 测试二：同一会话读取记忆 =====")

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "我刚才告诉你的本次会话测试代号是什么？"
                        "只回答代号，不要搜索代码仓库。"
                    ),
                }
            ]
        },
        config={
            "configurable": {"thread_id": "memory-test-001"},
            "callbacks": [callback],
        },
    )

    same_thread_answer = get_answer(result)
    print("同一会话回答：", same_thread_answer)

    assert "ORANGE-731" in same_thread_answer, (
        "同一会话未能回忆之前提供的测试代号。"
    )

    print("通过：同一会话能够读取历史消息。")

    # 不同 thread_id：不应继承上一个会话的历史。
    print("\n===== 测试三：不同会话隔离 =====")

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "我之前在这个会话中告诉过你测试代号吗？"
                        "如果没有，请明确说明当前会话尚未提供代号。"
                        "不要猜测，也不要搜索代码仓库。"
                    ),
                }
            ]
        },
        config={
            "configurable": {"thread_id": "memory-test-002"},
            "callbacks": [callback],
        },
    )

    new_thread_answer = get_answer(result)
    print("新会话回答：", new_thread_answer)

    assert "ORANGE-731" not in new_thread_answer, (
        "新会话似乎获取到了其他会话的测试代号，请检查会话隔离。"
    )

    print("通过：新会话没有复用旧会话的测试代号。")

    print("\n===== Token Budget =====")
    print(budget.summary())

    print("\n===== Trace =====")
    tracer.print_trace()

    print("\n短期记忆集成测试完成！")


if __name__ == "__main__":
    main()