
from app.observability.token_budget import TokenBudget


def test_normal_context():
    budget = TokenBudget(
        max_total_tokens=1000,
        max_input_tokens=500,
        reserved_output_tokens=200
    )

    tokens = budget.check_context("分析一下这个 Java 方法。")
    assert tokens > 0
    print("正常上下文检查通过")


def test_oversized_context():
    budget = TokenBudget(
        max_total_tokens=1000,
        max_input_tokens=10,
    )

    try:
        budget.check_context("这是一段用于测试的长上下文。" * 100)
    except ValueError:
        print("超预算拦截通过")
    else:
        raise AssertionError("超预算上下文没有被拦截")


def test_usage_accounting():
    budget = TokenBudget(max_total_tokens=1000)

    budget.record_usage(300, 200)
    budget.record_usage(100, 50)

    summary = budget.summary()

    assert summary["actual_input_tokens"] == 400
    assert summary["actual_output_tokens"] == 250
    assert summary["actual_total_tokens"] == 650
    assert summary["over_budget"] is False

    print("Token 累计统计通过")


if __name__ == "__main__":
    test_normal_context()
    test_oversized_context()
    test_usage_accounting()
    print("\n所有预算测试通过")