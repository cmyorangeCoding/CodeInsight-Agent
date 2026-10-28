from app.agents.repository_agent import (
    create_repository_agent,
)
from app.observability.tracer import Tracer
from app.observability.callback import TraceCallback
from app.observability.token_budget import TokenBudget
import app.tools.repository_tools as repo_tools


def main():

    question = """
请分析这个项目中的 ChatOpenAI 是在哪里创建的。

要求：

1. 找到真正实例化 ChatOpenAI 的位置。
2. 找到 get_llm() 被哪些地方调用。
3. 给出调用关系。
4. 不要根据猜测回答，必须使用代码证据。
"""

    tracer = Tracer()

    #参数选填
    budget = TokenBudget(
        max_total_tokens=30000,
        max_input_tokens=12000,
        reserved_output_tokens=2000
    )
    agent = create_repository_agent(token_budget=budget)

    callback = TraceCallback(
        tracer=tracer,
        token_budget=budget,
    )


    # 【已交给中间件执行】
    # # 先对用户问题做预检查。
    # # 完整的代码上下文仍需要在 ContextBuilder 组装后再次检查。
    # budget.check_context(question)

    try:
        with tracer.span("repository_agent"):
            result = agent.invoke(
                {
                    "messages": [
                        {"role": "user", "content": question}
                    ]
                },
                config={
                    "callbacks": [callback],
                },
            )

        print("\n===== Agent Result =====")
        print(result)

    except Exception as exc:
        print(f"Agent 执行失败：{exc}")
        raise

    finally:
        print("\n===== Trace =====")
        tracer.print_trace()

        print("\n===== Token Budget =====")
        print(budget.summary())


if __name__ == "__main__":
    main()