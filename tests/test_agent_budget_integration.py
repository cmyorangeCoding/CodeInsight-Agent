
from app.agents.repository_agent import create_repository_agent
from app.observability.tracer import Tracer
from app.observability.callback import TraceCallback
from app.observability.token_budget import TokenBudget
from app.observability.budget_middleware import (
    TokenBudgetExceededError,
)


def main():
    tracer = Tracer()

    budget = TokenBudget(
        max_total_tokens=30000,
        max_input_tokens=12000,
        reserved_output_tokens=2000,
    )

    agent = create_repository_agent(token_budget=budget)

    callback = TraceCallback(
        tracer=tracer,
        token_budget=budget,
    )

    # 测试一：真实模型调用及实际 Token 统计
    print("\n===== 测试一：正常 Agent 调用 =====")

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "请调用 get_repo_tree 工具查看项目结构，"
                        "简要说明根目录下有哪些主要目录。"
                        "必须先使用工具获取真实信息。"
                    ),
                }
            ]
        },
        config={"callbacks": [callback]},
    )

    print("Agent 调用成功。")
    print("实际 Token 汇总：", budget.summary())

    # 必须确认回调确实记录到了模型用量。
    report = budget.summary()

    if report["actual_total_tokens"] <= 0:
        raise AssertionError(
            "没有记录到实际 Token 用量，请检查模型响应和回调。"
        )

    actual_total = report["actual_total_tokens"]

    # 测试二：超长输入必须在调用模型之前被拦截
    print("\n===== 测试二：超长输入拦截 =====")

    # 临时放宽任务总预算，确保本次主要验证单次输入上限。
    budget.max_total_tokens = actual_total + 100000

    oversized_question = "请分析代码仓库。" * 30000

    try:
        agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": oversized_question,
                    }
                ]
            },
            config={
                "configurable": {
                    "thread_id": "codeinsight-session-001"
                },
                "callbacks": [callback],
            }
        )
    except TokenBudgetExceededError:
        print("通过：超长输入已被拦截。")
    else:
        raise AssertionError(
            "失败：超长输入没有被预算中间件拦截。"
        )

    # 测试三：实际累计 Token 超限后，后续调用必须被拦截
    print("\n===== 测试三：累计预算超限拦截 =====")

    # 将任务上限调至已记录的实际用量以下，
    # 模拟任务累计用量已经超限的状态。
    budget.max_total_tokens = actual_total - 1

    assert budget.summary()["over_budget"] is True

    try:
        agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "请继续分析项目。",
                    }
                ]
            },
            config={"callbacks": [callback]},
        )
    except TokenBudgetExceededError:
        print("通过：累计预算超限后，后续调用已被拦截。")
    else:
        raise AssertionError(
            "失败：累计预算超限后，Agent 仍然执行了调用。"
        )

    print("\n===== 最终预算报告 =====")
    print(budget.summary())

    print("\n===== Trace =====")
    tracer.print_trace()

    print("\n全部集成验证通过！")


if __name__ == "__main__":
    main()