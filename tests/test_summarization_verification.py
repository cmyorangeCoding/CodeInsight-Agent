"""
验证 SummarizationMiddleware 摘要功能：
1. 确认 SummarizationMiddleware 能正常导入、Agent 能正常创建
2. 使用同一个 thread_id 连续发送多轮消息，检查摘要触发前后能否继续对话
3. 查看 TokenBudget 和 TraceCallback 的统计，确认摘要后输入 Token 是否减少
"""
from app.agents.repository_agent import create_repository_agent
from app.observability.tracer import Tracer
from app.observability.callback import TraceCallback
from app.observability.token_budget import TokenBudget


def main():
    print("=" * 70)
    print("验证一：SummarizationMiddleware 导入 & Agent 创建")
    print("=" * 70)

    tracer = Tracer()
    # 由于 enable_thinking 导致输出 token 较大，这里放宽总预算以便跑满多轮
    # max_input_tokens 放宽到 30000，避免工具返回大结果时被单次输入上限拦截
    budget = TokenBudget(
        max_total_tokens=500000,
        max_input_tokens=30000,
        reserved_output_tokens=2000,
    )

    agent = create_repository_agent(token_budget=budget)
    callback = TraceCallback(tracer=tracer, token_budget=budget)

    print("SummarizationMiddleware 导入成功")
    print("Agent 创建成功:", type(agent).__name__)

    thread_id = "summarization-verification-001"
    print(f"使用 thread_id: {thread_id}")
    print(f"TokenBudget 配置: max_total={budget.max_total_tokens}, "
          f"max_input={budget.max_input_tokens}")
    print("SummarizationMiddleware 配置: trigger=(tokens, 7000), keep=(messages, 10)")

    print("\n" + "=" * 70)
    print("验证二：连续多轮对话（触发摘要前后）")
    print("=" * 70)

    # 构造一组较长的问题，每轮都带较长上下文，目的是让消息 token 累积
    # 触发 SummarizationMiddleware 的 trigger=("tokens", 7000)
    rounds = [
        "请介绍一下 Python 中装饰器的概念，并给出一个实际使用的代码示例，包括 functools.wraps 的作用。",
        "请详细解释一下 Python 的 GIL（全局解释器锁）机制，它对多线程程序有什么影响？在什么情况下多线程仍然有用？",
        "请比较 Python 中 list、tuple、set、dict 四种数据结构的特点、底层实现差异以及各自适用的场景。",
        "请解释 Python 中的生成器（generator）和迭代器（iterator）的区别，yield 关键字的工作原理是什么？",
        "请详细说明 Python 的异常处理机制，包括 try/except/else/finally 的执行顺序，以及自定义异常类的最佳实践。",
        "请介绍 Python 中的上下文管理器（context manager），with 语句的工作原理，以及如何使用 contextlib 模块。",
        "请解释 Python 中的 metaclass（元类）概念，它的作用是什么？在什么场景下需要使用元类？",
        "请比较 Python 中的深拷贝（deepcopy）和浅拷贝（copy），分别说明它们的实现原理和使用场景。",
        "请介绍 Python 中的 asyncio 异步编程模型，事件循环的工作原理，以及 async/await 关键字的使用方法。",
        "请解释 Python 中的描述符（descriptor）协议，__get__、__set__、__delete__ 方法的作用，以及属性访问的底层机制。",
        "请比较 Python 中的多线程、多进程和协程三种并发模型，分别说明它们的优缺点和适用场景。",
        "请介绍 Python 中的 import 机制，模块缓存、命名空间包以及 sys.path 的工作原理。",
    ]

    # 每轮统计：本轮累计 input_tokens 增量、消息数、最后一次模型调用的 input_tokens
    round_input_tokens = []   # 本轮所有模型调用的 input_tokens 之和
    last_model_inputs = []    # 本轮最后一次模型调用的 input_tokens（反映上下文大小）
    message_counts = []
    model_call_counts = []    # 本轮模型调用次数

    for i, question in enumerate(rounds, 1):
        print(f"\n--- 第 {i} 轮对话 ---")
        print(f"用户问题（前80字）: {question[:80]}...")

        # 记录本轮开始前的预算基线
        input_before = budget.actual_input_tokens
        model_end_before = len(
            [e for e in tracer.events if e.get("type") == "model_end"]
        )

        result = agent.invoke(
            {"messages": [{"role": "user", "content": question}]},
            config={
                "configurable": {"thread_id": thread_id},
                "callbacks": [callback],
            },
        )

        last_msg = result["messages"][-1]
        reply = last_msg.content
        print(f"Agent 回复（前80字）: {str(reply)[:80]}...")

        # 本轮模型调用次数
        model_ends = [e for e in tracer.events if e.get("type") == "model_end"]
        round_model_ends = model_ends[model_end_before:]
        call_count = len(round_model_ends)
        model_call_counts.append(call_count)

        # 本轮最后一次模型调用的 input_tokens（最能反映当前上下文大小）
        if round_model_ends:
            last_input = round_model_ends[-1].get("input_tokens", 0)
        else:
            last_input = 0
        last_model_inputs.append(last_input)

        # 本轮累计 input_tokens（所有模型调用之和）
        round_total_input = budget.actual_input_tokens - input_before
        round_input_tokens.append(round_total_input)

        print(f"本轮模型调用次数: {call_count}")
        print(f"本轮最后一次模型输入 Token: {last_input}")
        print(f"本轮累计输入 Token: {round_total_input}")

        # 记录当前消息数量
        state = agent.get_state({"configurable": {"thread_id": thread_id}})
        msg_count = len(state.values.get("messages", []))
        message_counts.append(msg_count)
        print(f"当前会话消息总数: {msg_count}")

        budget_report = budget.summary()
        print(
            f"TokenBudget 累计: input={budget_report['actual_input_tokens']}, "
            f"output={budget_report['actual_output_tokens']}, "
            f"total={budget_report['actual_total_tokens']}"
        )

    print("\n" + "=" * 70)
    print("验证三：TokenBudget & TraceCallback 统计分析")
    print("=" * 70)

    print("\n各轮统计（最后一次模型调用 input_tokens 反映上下文大小）:")
    print(f"{'轮次':>4} {'模型调用次数':>10} {'最后输入Token':>14} {'本轮累计输入':>14} {'消息总数':>8}")
    for idx in range(len(last_model_inputs)):
        print(
            f"{idx+1:>4} {model_call_counts[idx]:>10} "
            f"{last_model_inputs[idx]:>14} {round_input_tokens[idx]:>14} "
            f"{message_counts[idx]:>8}"
        )

    # 检查摘要触发效果：摘要触发后，后续轮次"最后一次模型输入 Token"
    # 应相比峰值下降（历史消息被压缩成摘要）
    if len(last_model_inputs) >= 3:
        peak = max(last_model_inputs)
        peak_idx = last_model_inputs.index(peak)
        print(f"\n上下文输入 Token 峰值: {peak} (出现在第 {peak_idx+1} 轮)")

        after_peak = last_model_inputs[peak_idx:]
        if after_peak:
            min_after = min(after_peak)
            reduction = peak - min_after
            reduction_pct = (reduction / peak * 100) if peak > 0 else 0
            print(f"峰值后最低上下文输入 Token: {min_after}")
            print(f"Token 减少量: {reduction} ({reduction_pct:.1f}%)")

            # 检测摘要触发信号：消息总数突降（keep=10，压缩后历史合并为摘要）
            summary_triggered = False
            for j in range(1, len(message_counts)):
                if message_counts[j] < message_counts[j-1]:
                    summary_triggered = True
                    print(
                        f"检测到消息数下降：第 {j} 轮 {message_counts[j-1]} -> "
                        f"第 {j+1} 轮 {message_counts[j]}，摘要已触发"
                    )

            if reduction > 0 and summary_triggered:
                print(
                    "\n结论：摘要后上下文输入 Token 相比峰值明显减少，"
                    "且消息总数下降，SummarizationMiddleware 摘要压缩生效。"
                )
            elif reduction > 0:
                print(
                    "\n结论：上下文输入 Token 相比峰值减少，"
                    "SummarizationMiddleware 摘要压缩可能已生效。"
                )
            else:
                print(
                    "\n警告：上下文输入 Token 未明显减少，"
                    "可能摘要尚未触发或压缩效果不明显。"
                )
    else:
        print("\n轮次不足，无法对比摘要前后 Token 变化。")

    print("\n各轮会话消息总数:")
    for idx, cnt in enumerate(message_counts, 1):
        print(f"  第 {idx} 轮后: messages = {cnt}")

    print("\n最终 TokenBudget 汇总:")
    print(budget.summary())

    print("\nTrace 事件统计:")
    trace_summary = tracer.summary()
    print(f"  总事件数: {trace_summary['event_count']}")
    print(f"  错误数: {trace_summary['error_count']}")
    print(f"  总耗时: {trace_summary['total_elapsed_ms']} ms")

    # 统计 model_end 事件
    model_ends = [e for e in trace_summary["events"] if e["type"] == "model_end"]
    print(f"  model_end 事件数: {len(model_ends)}")
    total_input = sum(e.get("input_tokens", 0) for e in model_ends)
    total_output = sum(e.get("output_tokens", 0) for e in model_ends)
    print(f"  Trace 中累计 input_tokens: {total_input}")
    print(f"  Trace 中累计 output_tokens: {total_output}")

    # 校验 TraceCallback 与 TokenBudget 的统计一致性
    budget_report = budget.summary()
    print("\n统计一致性校验:")
    print(f"  TokenBudget.actual_input_tokens = {budget_report['actual_input_tokens']}")
    print(f"  Trace 累计 input_tokens         = {total_input}")
    if budget_report["actual_input_tokens"] == total_input:
        print("  -> 一致：TraceCallback 正确将 token 用量同步到 TokenBudget")
    else:
        print("  -> 不一致：请检查回调逻辑")

    print("\n" + "=" * 70)
    print("验证完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
