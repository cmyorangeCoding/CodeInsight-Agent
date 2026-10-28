"""编排层与评估体系测试（全 Mock，不依赖真实模型与数据库）。

1.  简单任务走 Direct，不强制复杂规划
2.  复杂任务生成合法计划并按依赖顺序执行
3.  计划结构不合法时可有限修复或安全失败
4.  步骤状态转换合法
5.  依赖失败时不会错误执行依赖步骤
6.  验证器识别缺失证据 / 不支持结论
7.  验证失败时触发有限重规划
8.  达到 Token / 时间 / 工具调用 / 重规划上限时安全终止
9.  部分任务失败时仍保留已完成步骤结果
10. 并行任务不破坏共享状态
11. 运行记录正确记录成功 / 失败 / 异常退出
12. 评估运行器处理缺失文件 / 错误数据 / 模型调用失败
13. 评分器正确计算必要事实覆盖率与证据覆盖率
14. 现有核心功能不退化

运行：pytest tests/test_orchestration.py -q
"""
from __future__ import annotations

import argparse
import time
import uuid

import pytest
from langchain_core.messages import AIMessage

from app.observability.token_budget import TokenBudget
from app.orchestration.complexity import ComplexityRouter, ComplexityVerdict
from app.orchestration.executor import Orchestrator
from app.orchestration.limits import OrchestrationLimits
from app.orchestration.models import (
    EvidenceRef,
    PlanStatus,
    PlanStep,
    RunRecord,
    StepStatus,
    Strategy,
    TaskPlan,
    VerificationResult,
)
from app.orchestration.planner import (
    PlanGenerator,
    build_fallback_plan,
    validate_plan,
)
from app.orchestration.models import PlanDraft, PlanDraftStep
from app.orchestration.records import (
    RunLog,
    aggregate_records,
    estimate_cost,
    resolve_token_usage,
)
from app.orchestration.verifier import Verifier


# =========================================================================== Mock 基础设施
class _Invoker:
    def __init__(self, fn):
        self.fn = fn

    def invoke(self, prompt):
        return self.fn(prompt)


class FixedLLM:
    """固定结构化输出的假模型。"""

    def __init__(self, result):
        self.result = result

    def with_structured_output(self, schema):
        return _Invoker(lambda p: self.result)


class SeqLLM:
    """按顺序返回结构化输出的假模型（用于计划修复）。"""

    def __init__(self, outputs):
        self._it = iter(outputs)

    def with_structured_output(self, schema):
        return _Invoker(lambda p: next(self._it))


class MockAgent:
    """模拟 LangChain Agent：回放工具事件 + 返回固定/可计算文本。"""

    def __init__(self, content="mock-answer", tool_events=None, raise_on=None, delay=0.0):
        self.content = content                 # str 或 callable(prompt)->str
        self.tool_events = tool_events         # list 或 callable(prompt)->list
        self.raise_on = raise_on               # callable(prompt)->bool
        self.delay = delay
        self.prompts: list[str] = []

    def invoke(self, payload, config=None):
        prompt = payload["messages"][-1]["content"]
        self.prompts.append(prompt)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_on and self.raise_on(prompt):
            raise RuntimeError("mock step failure")

        events = self.tool_events(prompt) if callable(self.tool_events) else (self.tool_events or [])
        cbs = (config or {}).get("callbacks") or []
        for name, args_str, output in events:
            rid = f"rid-{uuid.uuid4().hex}"
            for cb in cbs:
                cb.on_tool_start({"name": name}, args_str, run_id=rid)
                cb.on_tool_end(output, run_id=rid)

        content = self.content(prompt) if callable(self.content) else self.content
        return {"messages": [AIMessage(content=content)]}


class MockFactory:
    def __init__(self, agent):
        self.agent = agent
        self.modes: list[str] = []

    def __call__(self, mode):
        self.modes.append(mode)
        return self.agent


class StaticPlanGenerator:
    def __init__(self, plan):
        self.plan = plan

    def generate(self, question):
        return self.plan.model_copy(deep=True)


class StaticVerifier:
    def __init__(self, results):
        self.results = results
        self.calls = 0

    def verify(self, question, answer, evidence, plan=None):
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


class FakeRepo:
    def __init__(self, files: dict[str, str]):
        self.files = files

    def exists(self, rel_path: str) -> bool:
        return rel_path in self.files

    def read_text(self, rel_path: str) -> str | None:
        return self.files.get(rel_path)


def make_plan(specs) -> TaskPlan:
    steps = [
        PlanStep(step_id=sid, description=desc, depends_on=list(deps), parallelizable=par)
        for (sid, desc, deps, par) in specs
    ]
    plan = TaskPlan(goal="test-goal", steps=steps)
    plan.refresh_status()
    plan.status = PlanStatus.PENDING
    return plan


def make_orchestrator(agent, *, modes=None, verifier=None, plan=None, limits=None, budget=None):
    factory = MockFactory(agent)
    kwargs = dict(
        agent_factory=factory,
        budget=budget,
        tracer=None,
        limits=limits or OrchestrationLimits(),
        verifier=verifier,
        llm=None,
        model_name="mock-model",
    )
    if plan is not None:
        kwargs["plan_generator"] = StaticPlanGenerator(plan)
    return Orchestrator(**kwargs), factory


# =========================================================================== 1. Direct 路由
def test_simple_question_routes_direct_without_planning():
    router = ComplexityRouter(use_llm=False)
    decision = router.route("get_llm 定义在哪里？它返回什么对象？")
    assert decision.strategy == Strategy.DIRECT
    assert decision.source == "rule"

    # 强复杂信号 → 自动路由不再进 Plan（plan 仅手动启用），按 ReAct 处理
    strong = router.route("请从 API 入口追踪到数据库，分析完整调用链与模块之间如何协作。")
    assert strong.strategy == Strategy.REACT
    assert strong.confidence == 1.0


def test_llm_router_cannot_downgrade_rule_decision():
    # 弱信号 → 规则给 REACT(0.5)；模型若说 plan_execute，自动路由归一为 REACT（plan 仅手动）
    q = "请解释这个模块的依赖关系设计思路"
    router = ComplexityRouter(llm=FixedLLM(ComplexityVerdict(strategy="plan_execute", reason="x", confidence=0.6)))
    up = router.route(q)
    assert up.strategy == Strategy.REACT

    # 模型若想降级为 direct，必须保留规则档位 REACT
    router2 = ComplexityRouter(llm=FixedLLM(ComplexityVerdict(strategy="direct", reason="x", confidence=0.6)))
    down = router2.route(q)
    assert down.strategy == Strategy.REACT


def test_forced_strategy_shortcircuits():
    router = ComplexityRouter(use_llm=False)
    decision = router.route("随便问", force=Strategy.DIRECT)
    assert decision.source == "forced"
    assert decision.strategy == Strategy.DIRECT


def test_orchestrator_uses_injected_routing_llm():
    """复杂度路由应使用注入的轻量模型；未注入时回退主模型。"""
    main_llm = FixedLLM(ComplexityVerdict(strategy="direct", reason="main", confidence=0.6))
    route_llm = FixedLLM(ComplexityVerdict(strategy="plan_execute", reason="route", confidence=0.6))
    plan = make_plan([("s1", "定位 [s1]", [], False), ("s2", "汇总 [s2]", ["s1"], False)])

    # 未注入 → 回退主模型
    plain = Orchestrator(llm=main_llm, agent_factory=MockFactory(MockAgent()))
    assert plain.routing_llm is main_llm

    # 注入后：规则给 REACT(0.5)，路由模型说 plan_execute → 自动路由归一为 react（plan 仅手动）
    orch = Orchestrator(
        llm=main_llm,
        routing_llm=route_llm,
        agent_factory=MockFactory(MockAgent(content="a")),
        plan_generator=StaticPlanGenerator(plan),
    )
    assert orch.routing_llm is route_llm
    result = orch.run("请解释这个模块的依赖关系设计思路")
    assert result.plan is None                      # 自动路由不再触发计划路径
    assert result.record.strategy == "react"

    # 手动强制 plan 仍可用
    forced = Orchestrator(
        llm=main_llm,
        routing_llm=route_llm,
        agent_factory=MockFactory(MockAgent(content="a")),
        plan_generator=StaticPlanGenerator(plan),
    )
    forced_result = forced.run("请解释这个模块的依赖关系设计思路", force_strategy=Strategy.PLAN_EXECUTE)
    assert forced_result.plan is not None
    assert forced_result.record.strategy == "plan_execute"


def test_plan_and_synthesis_receive_user_preferences():
    """plan 模式虽然单步不注入偏好，但规划与汇总必须读到用户长期偏好。"""
    prefs_text = "回答请用中文，并给出行号"

    class _Record:
        def __init__(self, content):
            self.content = content

    class _Memory:
        def load_user_preferences(self):
            return [_Record(prefs_text)]

    class _PrefPlanner:
        def __init__(self, plan):
            self.plan = plan
            self.seen: list[str] = []

        def generate(self, question, preferences=""):
            self.seen.append(preferences)
            return self.plan.model_copy(deep=True)

    class _SynthesisLLM:
        def __init__(self):
            self.prompts: list[str] = []

        def invoke(self, prompt):
            self.prompts.append(prompt)
            return AIMessage(content="最终回答")

    plan = make_plan([("s1", "读取 app/main.py 的实现", [], False)])
    planner = _PrefPlanner(plan)
    llm = _SynthesisLLM()
    orch = Orchestrator(
        llm=llm,
        agent_factory=MockFactory(MockAgent(content="步骤结论")),
        plan_generator=planner,
        memory=_Memory(),
        model_name="mock-model",
    )
    result = orch.run("解释 app/main.py", force_strategy=Strategy.PLAN_EXECUTE)

    assert prefs_text in planner.seen[-1]        # 规划阶段注入
    assert llm.prompts and prefs_text in llm.prompts[-1]   # 汇总阶段注入
    assert result.answer == "最终回答"


def test_legacy_plan_generator_without_preferences_still_works():
    """外部注入的旧版计划生成器（只接受 question）不应被新增参数破坏。"""
    plan = make_plan([("s1", "读取实现 [s1]", [], False)])
    orch = Orchestrator(
        llm=None,
        agent_factory=MockFactory(MockAgent(content="结论")),
        plan_generator=StaticPlanGenerator(plan),
        model_name="mock-model",
    )
    result = orch.run("解释实现", force_strategy=Strategy.PLAN_EXECUTE)
    assert result.plan is not None
    assert result.plan.steps[0].status == StepStatus.COMPLETED


# =========================================================================== 2/9. 计划执行与依赖顺序
def test_plan_executes_in_dependency_order_and_records_evidence():
    plan = make_plan([
        ("s1", "定位实现 [s1]", [], False),
        ("s2", "读取细节 [s2]", ["s1"], False),
        ("s3", "汇总结论并核对证据 [s3]", ["s1", "s2"], False),
    ])
    content = lambda p: f"result:{p.split('：')[-1].split(' [')[0]}"  # noqa: E731
    events = lambda p: [("search_code_rag", "{'query':'x'}", "--- [1] app/models/llm.py:10-12 符号: get_llm (rrf=0.1) ---\nbody")]  # noqa: E731
    agent = MockAgent(content=content, tool_events=events)
    orch, factory = make_orchestrator(agent, plan=plan, verifier=None)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    assert result.plan.status == PlanStatus.COMPLETED
    assert result.record.status == "success"
    assert factory.modes == ["step", "step", "step"]
    # 依赖顺序：s1 先于 s2，s2 先于 s3
    joined = "\n".join(agent.prompts)
    assert joined.index("[s1]") < joined.index("[s2]") < joined.index("[s3]")
    # 证据引用回填到步骤
    assert all(len(s.evidence) >= 1 for s in result.plan.steps)
    assert result.plan.steps[-1].evidence[0].file_path == "app/models/llm.py"


def test_direct_and_react_single_step_paths():
    for strategy, mode in ((Strategy.DIRECT, "direct"), (Strategy.REACT, "react")):
        agent = MockAgent(content=f"{mode}-answer")
        orch, factory = make_orchestrator(agent)
        result = orch.run("简单问题", force_strategy=strategy)
        assert result.answer == f"{mode}-answer"
        assert result.plan is None
        assert result.record.status == "success"
        assert factory.modes == [mode]


# =========================================================================== 3. 计划校验/修复/兜底
def test_validate_plan_rejects_invalid_structures():
    dup = make_plan([("s1", "a", [], False), ("s1", "b", [], False), ("s3", "汇总", ["s1"], False)])
    assert any("重复" in p for p in validate_plan(dup))

    cycle = make_plan([("s1", "a", ["s2"], False), ("s2", "b", ["s1"], False), ("s3", "汇总", [], False)])
    assert any("环路" in p for p in validate_plan(cycle))

    missing_dep = make_plan([("s1", "汇总", ["nope"], False)])
    assert any("不存在" in p for p in validate_plan(missing_dep))

    no_summary = make_plan([("s1", "a", [], False), ("s2", "b", ["s1"], False)])
    assert any("汇总" in p for p in validate_plan(no_summary))


def test_plan_generator_repairs_then_succeeds():
    bad = PlanDraft(goal="g", steps=[PlanDraftStep(step_id="s1", description="a"), PlanDraftStep(step_id="s1", description="b")])
    good = PlanDraft(
        goal="g",
        steps=[
            PlanDraftStep(step_id="s1", description="定位实现"),
            PlanDraftStep(step_id="s2", description="汇总并核对证据", depends_on=["s1"]),
        ],
    )
    gen = PlanGenerator(SeqLLM([bad, good]), OrchestrationLimits(max_plan_repair_attempts=1))
    plan = gen.generate("问题")
    assert plan.origin == "repaired"
    assert validate_plan(plan) == []


def test_plan_generator_falls_back_when_always_invalid():
    bad = PlanDraft(goal="g", steps=[PlanDraftStep(step_id="s1", description="a"), PlanDraftStep(step_id="s1", description="b")])
    gen = PlanGenerator(SeqLLM([bad, bad]), OrchestrationLimits(max_plan_repair_attempts=1))
    plan = gen.generate("问题")
    assert plan.origin == "fallback"
    assert validate_plan(plan) == []          # 兜底计划必须合法
    assert plan.error is not None


def test_plan_generator_without_llm_uses_deterministic_fallback():
    plan = PlanGenerator(None).generate("复杂问题")
    assert plan.origin == "fallback"
    assert validate_plan(plan) == []
    assert build_fallback_plan("q", OrchestrationLimits()).steps[-1].step_id == "s3"


# =========================================================================== 4. 状态转换合法性
def test_step_state_transitions_are_enforced():
    step = PlanStep(step_id="s1", description="d")
    step.transition(StepStatus.RUNNING)
    step.transition(StepStatus.COMPLETED)
    assert step.status == StepStatus.COMPLETED
    with pytest.raises(ValueError):
        step.transition(StepStatus.RUNNING)      # COMPLETED 不可回退

    step2 = PlanStep(step_id="s2", description="d")
    with pytest.raises(ValueError):
        step2.transition(StepStatus.COMPLETED)   # PENDING 不能直接 COMPLETED


# =========================================================================== 5. 依赖失败跳过
def test_dependency_failure_marks_dependent_step_skipped():
    plan = make_plan([("s1", "定位 [s1]", [], False), ("s2", "读取 [s2]", ["s1"], False)])
    agent = MockAgent(raise_on=lambda p: "[s1]" in p)
    orch, _ = make_orchestrator(agent, plan=plan, verifier=None)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    s1, s2 = result.plan.steps
    assert s1.status == StepStatus.FAILED and s1.error
    assert s2.status == StepStatus.SKIPPED
    assert s2.failure_type.value == "plan_dependency_error"
    assert result.record.status == "failed"
    # 依赖未完成的步骤不会被错误执行
    assert all("[s2]" not in p for p in agent.prompts if p.count("分析步骤"))


# =========================================================================== 6. 验证器
def test_verifier_detects_missing_evidence_and_unsupported_claims():
    limits = OrchestrationLimits(enable_llm_verification=False, min_evidence_refs=1)
    repo = FakeRepo({"app/models/llm.py": "line\n" * 20 + "def get_llm():\n"})
    verifier = Verifier(repo=repo, llm=None, limits=limits)

    # 6a. 正常证据 → 通过
    ok = verifier.verify(
        "问题", "get_llm 在 app/models/llm.py 定义",
        [EvidenceRef(file_path="app/models/llm.py", symbol_name="get_llm", line_start=10, line_end=10)],
    )
    assert ok.passed is True and ok.unsupported_claims == []

    # 6b. 文件不存在 → 不支持结论
    bad_path = verifier.verify(
        "问题", "答案", [EvidenceRef(file_path="app/ghost.py", symbol_name="x")]
    )
    assert bad_path.passed is False
    assert any("不存在" in c for c in bad_path.unsupported_claims)

    # 6c. 行号越界
    oor = verifier.verify(
        "问题", "答案",
        [EvidenceRef(file_path="app/models/llm.py", line_start=999, line_end=1000)],
    )
    assert oor.passed is False and any("越界" in c for c in oor.unsupported_claims)

    # 6d. 符号无法核实
    sym = verifier.verify(
        "问题", "答案",
        [EvidenceRef(file_path="app/models/llm.py", symbol_name="not_here")],
    )
    assert sym.passed is False and any("符号" in c for c in sym.unsupported_claims)

    # 6e. 无证据却断言「不存在」→ 不支持
    neg = verifier.verify("问题", "仓库中没有找到该功能的实现", [])
    assert neg.passed is False
    assert neg.missing_evidence and neg.unsupported_claims

    # 6f. 回答引用不存在文件
    bad_ans = verifier.verify(
        "问题", "参见 app/nope.py 的实现",
        [EvidenceRef(file_path="app/models/llm.py", line_start=1)],
    )
    assert bad_ans.passed is False and any("不存在" in c for c in bad_ans.unsupported_claims)


def test_verifier_flags_unfinished_plan_steps():
    limits = OrchestrationLimits(enable_llm_verification=False, min_evidence_refs=0)
    verifier = Verifier(repo=FakeRepo({}), llm=None, limits=limits)
    plan = make_plan([("s1", "a", [], False)])
    plan.steps[0].transition(StepStatus.RUNNING)
    plan.steps[0].transition(StepStatus.FAILED)
    result = verifier.verify("q", "答案", [], plan)
    assert result.passed is False
    assert any("未成功完成" in m for m in result.missing_evidence)


# =========================================================================== 7. 验证失败触发有限重规划
def test_verification_failure_triggers_bounded_replan():
    plan = make_plan([("s1", "定位 [s1]", [], False), ("s2", "汇总 [s2]", ["s1"], False)])
    failing = VerificationResult(passed=False, missing_evidence=["需要补充调用链证据"])
    verifier = StaticVerifier([failing])   # 始终失败
    limits = OrchestrationLimits(max_replans=2, max_steps=8, enable_llm_verification=False)
    agent = MockAgent(content="answer")
    orch, _ = make_orchestrator(agent, plan=plan, verifier=verifier, limits=limits)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    assert result.record.replan_count == 2                 # 达到上限即停
    assert result.record.verification_passed is False
    assert result.record.status == "partial"
    assert len(result.plan.steps) > 2                      # 确实新增了补救步骤
    assert result.plan.origin == "replanned"
    assert verifier.calls == 3                             # 初次 + 2 次重规划后各验证一次


# =========================================================================== 8. 上限安全终止
def test_time_limit_terminates_safely():
    plan = make_plan([("s1", "定位 [s1]", [], False), ("s2", "汇总 [s2]", ["s1"], False)])
    limits = OrchestrationLimits(max_duration_seconds=0.0)
    agent = MockAgent(content="answer")
    orch, factory = make_orchestrator(agent, plan=plan, verifier=None, limits=limits)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    assert result.record.failure_reason == "budget_exhausted"
    assert result.record.plan_steps_completed == 0
    assert result.record.status != "success"               # 未完成不得宣告成功
    assert factory.modes == []                             # 未真正执行任何步骤


def test_token_budget_limit_terminates_safely():
    plan = make_plan([("s1", "定位 [s1]", [], False), ("s2", "汇总 [s2]", ["s1"], False)])
    budget = TokenBudget(max_total_tokens=100, max_input_tokens=1000, reserved_output_tokens=10)
    budget.record_usage(200, 200)                          # 人为超预算
    agent = MockAgent(content="answer")
    orch, factory = make_orchestrator(agent, plan=plan, verifier=None, budget=budget)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    assert result.record.failure_reason == "budget_exhausted"
    assert result.record.status != "success"


def test_tool_call_limit_terminates_safely():
    plan = make_plan([("s1", "定位 [s1]", [], False), ("s2", "汇总 [s2]", ["s1"], False)])
    limits = OrchestrationLimits(max_tool_calls=1)
    events = [("search_code_rag", "{'query':'x'}", "--- [1] app/models/llm.py:1-2 符号: x (rrf=0.1) ---\nb")]
    agent = MockAgent(content="answer", tool_events=events)
    orch, factory = make_orchestrator(agent, plan=plan, verifier=None, limits=limits)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    # 首个步骤消耗 1 次工具调用 → 达到上限，后续步骤不再执行
    assert result.record.tool_call_count == 1
    assert len(factory.modes) == 1
    assert result.record.status != "success"


# =========================================================================== 9. 部分失败保留结果
def test_partial_failure_keeps_completed_step_results():
    plan = make_plan([
        ("s1", "定位 [s1]", [], False),
        ("s2", "读取 [s2]", ["s1"], False),
        ("s3", "汇总 [s3]", ["s1", "s2"], False),
    ])
    agent = MockAgent(content="S1-RESULT", raise_on=lambda p: "[s2]" in p)
    orch, _ = make_orchestrator(agent, plan=plan, verifier=None)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    s1, s2, s3 = result.plan.steps
    assert s1.status == StepStatus.COMPLETED and s1.result == "S1-RESULT"
    assert s2.status == StepStatus.FAILED and s2.error
    assert s3.status == StepStatus.SKIPPED
    assert result.plan.status == PlanStatus.PARTIAL
    assert result.record.status == "partial"
    assert "S1-RESULT" in result.answer                    # 已完成结果被保留
    assert result.record.plan_steps_completed == 1


# =========================================================================== 10. 并行不破坏共享状态
def test_parallel_steps_keep_isolated_state():
    plan = make_plan([
        ("s1", "分析配置模块 [s1]", [], True),
        ("s2", "分析数据库模块 [s2]", [], True),
        ("s3", "汇总 [s3]", ["s1", "s2"], False),
    ])

    def events(prompt):
        sid = prompt.split("[")[1].split("]")[0]
        return [("search_code_rag", "{'query':'x'}",
                 f"--- [1] app/{sid}.py:1-2 符号: fn_{sid} (rrf=0.1) ---\nb")]

    agent = MockAgent(content=lambda p: f"done:{p.split('[')[1].split(']')[0]}",
                      tool_events=events, delay=0.03)
    limits = OrchestrationLimits(allow_parallel=True, max_parallel_workers=2)
    orch, factory = make_orchestrator(agent, plan=plan, verifier=None, limits=limits)

    result = orch.run("问题", force_strategy=Strategy.PLAN_EXECUTE)
    assert result.plan.status == PlanStatus.COMPLETED

    by_step: dict[str, list] = {}
    for tr in result.tool_results:
        by_step.setdefault(tr.step_id, []).append(tr)
    # 两个并行步骤各自保有自己的结果与证据，互不覆盖
    assert by_step["s1"][0].evidence[0].file_path == "app/s1.py"
    assert by_step["s2"][0].evidence[0].file_path == "app/s2.py"
    assert result.plan.step("s1").result == "done:s1"
    assert result.plan.step("s2").result == "done:s2"
    assert result.plan.step("s3").result == "done:s3"


# =========================================================================== 11. 运行记录
def _record(run_id="r1", strategy="direct", status="success", **kw):
    base = dict(run_id=run_id, strategy=strategy, model_name="m",
                started_at="2026-01-01T00:00:00+00:00", status=status)
    base.update(kw)
    return RunRecord(**base)


def test_run_log_appends_reads_and_finds(tmp_path):
    log = RunLog(tmp_path / "runs.jsonl")
    log.append(_record("a", total_tokens=10, duration_ms=100))
    log.append(_record("b", status="failed", failure_reason="tool_failure"))
    records = log.load_all()
    assert len(records) == 2
    assert log.find("b").status == "failed"
    assert log.find("missing") is None


def test_run_log_skips_corrupt_lines(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text('{"bad json"\n' + _record("ok").model_dump_json() + "\n", encoding="utf-8")
    log = RunLog(path)
    records = log.load_all()
    assert len(records) == 1 and records[0].run_id == "ok"


def test_token_usage_unknown_never_defaults_to_zero():
    assert resolve_token_usage(None) == (None, None, None, "unknown")
    assert resolve_token_usage({"actual_input_tokens": 0, "actual_output_tokens": 0}) == (None, None, None, "unknown")
    assert resolve_token_usage({"actual_input_tokens": 5, "actual_output_tokens": 7}) == (5, 7, 12, "provider")


def test_cost_unknown_without_pricing():
    assert estimate_cost("m", 100, 100, None) == (None, "unknown")
    assert estimate_cost("m", 100, 100, {}) == (None, "unknown")
    cost, source = estimate_cost("m", 1000, 1000, {"m": {"input_per_1k": 0.01, "output_per_1k": 0.02}})
    assert source == "pricing_config" and cost == pytest.approx(0.03)


def test_orchestrator_records_success_and_failure():
    ok = make_orchestrator(MockAgent(content="a"))[0].run("q", force_strategy=Strategy.DIRECT)
    assert ok.record.status == "success" and ok.record.strategy == "direct"
    assert ok.record.token_source == "unknown"             # 无 budget → 不臆造 token

    plan = make_plan([("s1", "定位 [s1]", [], False), ("s2", "汇总 [s2]", ["s1"], False)])
    orch, _ = make_orchestrator(MockAgent(raise_on=lambda p: True), plan=plan, verifier=None)
    failed = orch.run("q", force_strategy=Strategy.PLAN_EXECUTE)
    assert failed.record.status == "failed"
    assert failed.record.failure_reason in {"model_error", "tool_failure", "unknown"}


def test_aggregate_records_by_strategy():
    records = [
        _record("1", strategy="direct", status="success", duration_ms=100, total_tokens=10),
        _record("2", strategy="direct", status="failed", duration_ms=200, total_tokens=20),
        _record("3", strategy="baseline", status="success", duration_ms=300, total_tokens=30, token_source="provider"),
    ]
    agg = aggregate_records(records)
    assert agg["direct"]["runs"] == 2
    assert agg["direct"]["success_rate"] == 0.5
    assert agg["direct"]["avg_duration_ms"] == 150.0
    assert agg["direct"]["token_unknown_runs"] == 2          # 未设 token_source → unknown
    assert agg["baseline"]["token_unknown_runs"] == 0


# =========================================================================== 12. 评估运行器容错
def test_eval_dataset_loader_and_validator_tolerate_bad_input():
    from pathlib import Path

    from evals.dataset import load_tasks, validate_dataset

    # 缺失文件 → 回落到模块内置任务集
    fallback = load_tasks(Path("no/such/tasks.jsonl"))
    assert len(fallback) == 27

    good = load_tasks()
    assert validate_dataset(good) == []

    # 错误数据：证据文件不存在 + 重复 id + 缺字段
    broken = [
        {
            "id": "x1", "category": "simple_lookup", "question": "q", "difficulty": "easy",
            "expected_answer": "a", "required_facts": [{"text": "t", "any_of": ["t"]}],
            "required_evidence": [{"file_path": "no/such.py", "anchor": "x"}],
            "optional_evidence": [], "forbidden_or_unsupported_claims": [],
            "scoring_rubric": {"fact_weight": 1.0, "evidence_weight": 0.0},
        },
        {"id": "x1", "category": "simple_lookup", "question": "q"},
    ]
    problems = validate_dataset(broken)
    assert any("不存在" in p for p in problems)
    assert any("重复" in p for p in problems)
    assert any("缺少必需字段" in p for p in problems)


def test_eval_runner_handles_run_exception(monkeypatch):
    from evals import run_eval

    runner = run_eval.EvalRunner.__new__(run_eval.EvalRunner)
    runner.llm = None
    runner.llm_judge = False
    runner.repeat = 1

    def boom(self, *args, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(run_eval.EvalRunner, "_run_orch", boom)
    task = load_first_task()
    row = runner.run_once(task, "direct", {"kind": "orch", "force": "direct"}, 0)
    assert "RuntimeError" in row["error"]
    assert row["score"]["completed"] is False
    assert row["record"] is None


def test_eval_cli_validate_only_and_bad_variant(capsys):
    from evals import run_eval

    assert run_eval.main(["--validate-only"]) == 0
    out = capsys.readouterr().out
    assert "校验通过" in out

    with pytest.raises(SystemExit):
        run_eval._select_variants(argparse.Namespace(variants="does-not-exist"))

    tasks = load_first_task()
    ns = argparse.Namespace(category="architecture", ids=None, limit=None)
    selected = run_eval._select_tasks([tasks], ns)
    assert isinstance(selected, list)


def test_llm_judge_returns_none_without_model():
    from evals.scoring import judge_with_llm

    assert judge_with_llm(None, load_first_task(), "answer") is None

    class Boom:
        def with_structured_output(self, schema):
            raise RuntimeError("judge unavailable")

    assert judge_with_llm(Boom(), load_first_task(), "answer") is None


# =========================================================================== 13. 评分器
def test_scorer_computes_coverage_metrics():
    from evals.scoring import score_answer

    task = {
        "id": "t1", "category": "simple_lookup",
        "required_facts": [
            {"text": "f1", "any_of": ["app/models/llm.py"]},
            {"text": "f2", "any_of": ["ChatOpenAI"]},
            {"text": "f3", "any_of": ["enable_thinking"]},
        ],
        "required_evidence": [
            {"file_path": "app/models/llm.py", "symbol": "get_llm", "anchor": "def get_llm()"},
            {"file_path": "app/main.py", "symbol": "main", "anchor": "def main"},
        ],
        "forbidden_or_unsupported_claims": ["使用 Redis 缓存"],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4},
    }
    ref = type("R", (), {"file_path": "app/models/llm.py", "symbol_name": "get_llm"})()
    answer = "get_llm 定义在 app/models/llm.py，返回 ChatOpenAI 实例；建议使用 Redis 缓存。"
    score = score_answer(task, answer, [ref], run_status="success", strategy="direct", variant="direct")

    assert score.required_fact_count == 3
    assert score.required_fact_hits == 2                   # 命中 llm.py 与 ChatOpenAI
    assert score.fact_coverage == pytest.approx(2 / 3, abs=1e-4)
    assert score.required_evidence_count == 2
    assert score.required_evidence_hits == 1               # 仅 app/models/llm.py 命中
    assert score.evidence_coverage == pytest.approx(0.5)
    assert score.forbidden_claims_found == ["使用 Redis 缓存"]
    assert score.unsupported_claim_rate == pytest.approx(1.0)
    assert score.fact_correctness == pytest.approx(2 / 3, abs=1e-4)
    assert score.completed is True
    assert score.quality_score == pytest.approx(0.6 * (2 / 3) + 0.4 * 0.5 - 0.5, abs=1e-4)

    # 空答案 → 全部 0，且不宣告完成
    empty = score_answer(task, "", [], run_status="failed", strategy="direct")
    assert empty.fact_coverage == 0.0 and empty.completed is False
    assert empty.quality_score == 0.0


def test_scorer_aggregation_by_variant_and_category():
    from evals.scoring import aggregate_scores, score_answer

    task = {
        "id": "t", "category": "architecture",
        "required_facts": [{"text": "f", "any_of": ["A"]}],
        "required_evidence": [{"file_path": "app/main.py", "anchor": "def main"}],
        "forbidden_or_unsupported_claims": [],
        "scoring_rubric": {},
    }
    s1 = score_answer(task, "A app/main.py", [], run_status="success", variant="plan")
    s2 = score_answer(task, "B", [], run_status="failed", variant="react")
    agg = aggregate_scores([s1, s2])
    assert agg["plan"]["avg_fact_coverage"] == 1.0
    assert agg["plan"]["completion_rate"] == 1.0
    assert agg["react"]["avg_fact_coverage"] == 0.0
    assert agg["plan"]["by_category"]["architecture"]["runs"] == 1


def test_report_flags_anomalies_without_dropping_them():
    from evals import run_eval

    good = {
        "task_id": "a", "category": "simple_lookup", "variant": "direct", "repeat": 0,
        "strategy": "direct", "answer": "x", "error": None,
        "record": _record("ra", strategy="direct", status="success").model_dump(),
        "plan": None, "verification": None,
        "score": {"task_id": "a", "category": "simple_lookup", "strategy": "direct",
                  "variant": "direct", "repeat": 0, "completed": True},
    }
    bad = {
        "task_id": "b", "category": "simple_lookup", "variant": "direct", "repeat": 0,
        "strategy": "direct", "answer": "", "error": "RuntimeError: model down",
        "record": None, "plan": None, "verification": None,
        "score": {"task_id": "b", "category": "simple_lookup", "strategy": "direct",
                  "variant": "direct", "repeat": 0, "completed": False},
    }
    report = run_eval.build_report([good, bad], ["a", "b"], ["direct"],
                                   started_at="s", finished_at="f", notes=[])
    assert report["anomaly_count"] == 1
    assert report["anomalies"][0]["task_id"] == "b"
    md = run_eval.render_markdown(report)
    assert "异常样本（保留，未删除）" in md
    assert "| b |" in md


# =========================================================================== 14. 现有功能不退化
def test_existing_entrypoints_still_importable():
    """守卫测试：核心装配函数与 Agent 工厂保持可导入（CLI 入口已删除，共享逻辑在 app.indexing）。"""
    import app.indexing as indexing_mod
    from app.agents.repository_agent import (
        REPOSITORY_SYSTEM_PROMPT,
        build_repository_tools,
        create_repository_agent,
    )

    assert callable(indexing_mod.ensure_index)
    assert callable(indexing_mod.build_local_provider)
    assert callable(create_repository_agent)
    assert "search_code_rag" in REPOSITORY_SYSTEM_PROMPT

    # 未注入 retriever / store 时，工具集不含 RAG 与记忆工具
    tools, memory = build_repository_tools(token_budget=None, store=None, retriever=None)
    names = {t.name for t in tools}
    assert {"get_repo_tree", "read_file", "search_code"} <= names
    assert "search_code_rag" not in names and memory is None

    # 注入 retriever 后应包含 RAG 工具
    fake_retriever = type("Retriever", (), {"hybrid_search": lambda self, q: []})()
    tools2, _ = build_repository_tools(token_budget=None, store=None, retriever=fake_retriever)
    assert "search_code_rag" in {t.name for t in tools2}


def test_repository_context_test_module_imports():
    # 该测试此前 import 了已被移除的 search_code，现已修正为 SearchCodeTool
    from app.tools.repository_tools import SearchCodeTool, _IndexRepositoryFilesTool
    from app.repository import LocalRepositoryProvider

    provider = LocalRepositoryProvider(".")
    assert SearchCodeTool(provider=provider).name == "search_code"
    assert _IndexRepositoryFilesTool(provider=provider).name == "index_repository_files"


def test_token_budget_and_middleware_unchanged():
    from app.observability.budget_middleware import TokenBudgetExceededError, TokenBudgetMiddleware

    budget = TokenBudget(max_total_tokens=10, max_input_tokens=5, reserved_output_tokens=1)
    with pytest.raises(ValueError):
        budget.check_context("x" * 100, reserved_output_tokens=100)
    assert issubclass(TokenBudgetExceededError, RuntimeError)
    assert TokenBudgetMiddleware(budget).__class__ is TokenBudgetMiddleware


# --------------------------------------------------------------------------- helpers
def load_first_task() -> dict:
    from evals.dataset import load_tasks

    return load_tasks()[0]
