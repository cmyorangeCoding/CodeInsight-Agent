"""结构化计划生成与校验。

流程：LLM 结构化输出 → 确定性校验 → 有限次修复 → 仍失败则使用确定性兜底计划。
不允许把模型输出的自然语言计划直接当作状态管理机制。
"""
from __future__ import annotations

from app.orchestration.limits import OrchestrationLimits
from app.orchestration.models import (
    PlanDraft,
    PlanStep,
    PlanStatus,
    StepStatus,
    TaskPlan,
)

PLANNER_PROMPT = (
    "你是代码仓库分析任务的高级规划器。请针对用户问题生成一个**可执行、可验证**的分析计划。\n"
    "硬性要求：\n"
    "1) 步骤总数不超过 {max_steps} 个，**优先 3~4 个**。步骤不是越多越好："
    "多余的步骤会重复检索、拖慢执行。\n"
    "2) 每个步骤必须写明**具体检索目标**：文件名、符号名（函数/类/常量）、模块路径或明确的关键词。"
    "禁止出现「了解整体」「探索一下」「分析相关代码」这类没有具体目标的步骤。\n"
    "3) 每个步骤应能在 **3 次工具调用内**完成（一次检索 + 一到两次读取）。"
    "不要把同一个文件的多次读取拆成多个步骤。\n"
    "4) 后一步需要前一步的输出时写 depends_on；彼此完全独立可写 parallelizable=true。\n"
    "5) step_id 使用简短稳定标识（如 s1、s2），depends_on 只能引用已出现的 step_id。\n"
    "6) 最后一个步骤是「汇总并核对证据、给出带文件路径与行号的结论」，它不算检索步骤。\n"
    "7) 若给出了用户长期偏好，步骤设计应符合这些偏好（如输出语言、详略、引用格式）。\n"
    "用户长期偏好（可能为空）：\n{preferences}\n"
    "用户问题：{question}"
)

REPAIR_PROMPT = (
    "上一步生成的计划不合法，请修正后重新输出。\n"
    "校验错误：\n{problems}\n"
    "原始计划：\n{draft}\n"
    "用户长期偏好（可能为空）：\n{preferences}\n"
    "用户问题：{question}"
)


class PlanValidationError(ValueError):
    """计划结构非法。"""


def validate_plan(plan: TaskPlan, max_steps: int = 8) -> list[str]:
    """确定性校验计划结构，返回问题列表（空列表 = 合法）。"""
    problems: list[str] = []

    #不能是空计划，必须有步骤
    if not plan.steps:
        problems.append("计划没有任何步骤")
        return problems

    #总步骤不能超过上限
    if len(plan.steps) > max_steps:
        problems.append(f"步骤数 {len(plan.steps)} 超过上限 {max_steps}")

    #step_id 不能重复
    ids = [s.step_id for s in plan.steps]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        problems.append(f"存在重复的 step_id：{sorted(duplicates)}")

    known = set(ids)
    for step in plan.steps:
        #每个步骤描述不能为空
        if not step.description.strip():
            problems.append(f"步骤 {step.step_id} 缺少描述")
        for dep in step.depends_on:
            # 依赖不能是自己；依赖的 step_id 必须真实存在
            if dep == step.step_id:
                problems.append(f"步骤 {step.step_id} 依赖自身")
            elif dep not in known:
                problems.append(f"步骤 {step.step_id} 依赖不存在的步骤 {dep}")

    # 环路检测（拓扑排序）
    if not duplicates and _has_cycle({s.step_id: s.depends_on for s in plan.steps}):
        problems.append("计划依赖关系中存在环路")

    if not any("汇总" in s.description or "结论" in s.description for s in plan.steps):
        problems.append("计划中缺少汇总/结论步骤")

    return problems


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    """
    检测依赖图是否存在环
    逻辑：遍历节点，如果碰到 GRAY 节点 → 发现环，返回 True
    """
    #WHITE = 未访问；GRAY = 正在遍历当前这条路径；BLACK = 遍历完成
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for dep in graph.get(node, []):
            if color.get(dep, BLACK) == GRAY:
                return True
            if color.get(dep, BLACK) == WHITE and visit(dep):
                return True
        color[node] = BLACK
        return False

    return any(color[node] == WHITE and visit(node) for node in list(graph))


def build_fallback_plan(question: str, limits: OrchestrationLimits) -> TaskPlan:
    """确定性兜底计划：不依赖模型也能得到合法、可执行的计划。"""
    steps = [
        PlanStep(
            step_id="s1",
            description=f"用 search_code_rag 定位与问题相关的实现位置：{question[:80]}",
            parallelizable=False,
        ),
        PlanStep(
            step_id="s2",
            description="读取关键文件，提取实现细节与调用关系",
            depends_on=["s1"],
        ),
        PlanStep(
            step_id="s3",
            description="汇总结论并核对文件路径、符号名与行号证据",
            depends_on=["s1", "s2"],
        ),
    ]
    plan = TaskPlan(goal=question, steps=steps, origin="fallback")
    plan.refresh_status()
    plan.status = PlanStatus.PENDING
    return plan


def draft_to_plan(draft: PlanDraft, question: str, origin: str) -> TaskPlan:
    """把模型草稿转换为带状态机的 TaskPlan。"""
    steps = [
        PlanStep(
            step_id=s.step_id,
            description=s.description,
            depends_on=list(s.depends_on),
            parallelizable=bool(s.parallelizable),
        )
        for s in draft.steps
    ]
    plan = TaskPlan(goal=draft.goal or question, steps=steps, origin=origin)
    plan.refresh_status()
    plan.status = PlanStatus.PENDING
    return plan


class PlanGenerator:
    """计划生成器：结构化输出 + 校验 + 有限修复 + 兜底。"""

    def __init__(self, llm, limits: OrchestrationLimits | None = None, tracer=None):
        self.llm = llm #大模型实例，用来生成 / 修复计划
        self.limits = limits or OrchestrationLimits()
        self.tracer = tracer

    def _log(self, **data) -> None:
        if self.tracer is not None:
            try:
                self.tracer.add_event("plan_event", **data)
            except Exception:
                pass

    def generate(self, question: str, preferences: str = "") -> TaskPlan:
        """生成合法计划；任何环节失败都会安全回落到确定性计划。"""
        if self.llm is None:
            plan = build_fallback_plan(question, self.limits)
            self._log(action="fallback", reason="no_llm", step_count=len(plan.steps))
            return plan

        structured = self.llm.with_structured_output(PlanDraft)
        preferences_text = (preferences or "").strip() or "（无）"
        prompt = PLANNER_PROMPT.format(
            max_steps=self.limits.max_steps, question=question, preferences=preferences_text
        )

        #保存 LLM 输出的计划草稿
        draft: PlanDraft | None = None
        last_error = ""
        #尝试生成 + 修复计划  max_plan_repair_attempts 是最大修复次数
        for attempt in range(self.limits.max_plan_repair_attempts + 1):
            try:
                #`attempt == 0`：第一轮，使用原始 PLANNER_PROMPT，不是第一轮：调用`_repair_prompt`，把用户问题、上一轮草稿、上次错误传入，生成修复 prompt
                raw = structured.invoke(
                    prompt if attempt == 0
                    else self._repair_prompt(question, draft, last_error, preferences_text)
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"计划生成调用失败：{type(exc).__name__}: {exc}"
                self._log(action="generate_error", attempt=attempt, error=last_error)
                break

            try:
                #把 LLM 输出的草稿 raw 转为正式 TaskPlan
                plan = draft_to_plan(raw, question, origin="llm" if attempt == 0 else "repaired")
            except Exception as exc:  # noqa: BLE001
                last_error = f"计划结构解析失败：{type(exc).__name__}: {exc}"
                draft = raw
                continue

            #硬规则校验这个 TaskPlan，得到错误列表 problems。列表为空 = 计划合法。
            problems = validate_plan(plan, self.limits.max_steps)
            #错误列表为空，校验通过，计划合法
            if not problems:
                plan = self._apply_parallel_policy(plan)
                self._log(action="generated", origin=plan.origin, step_count=len(plan.steps))
                return plan

            last_error = "；".join(problems)
            draft = raw
            self._log(action="validation_failed", attempt=attempt, problems=problems)

        #for 循环全部轮次跑完（多次修复依旧校验失败），走到兜底逻辑
        plan = build_fallback_plan(question, self.limits)
        plan.error = last_error or None
        self._log(action="fallback", reason=last_error or "invalid_plan", step_count=len(plan.steps))
        return plan

    @staticmethod
    def _repair_prompt(question: str, draft, problems: str, preferences: str = "") -> str:
        return REPAIR_PROMPT.format(
            problems=problems,
            draft=draft.model_dump_json() if draft is not None else "（无）",
            question=question,
            preferences=preferences or "（无）",
        )

    def _apply_parallel_policy(self, plan: TaskPlan) -> TaskPlan:
        """按配置决定是否保留并行标记：默认关闭并行（最稳）。"""
        #配置不允许并行，或者最大并行 worker 数量≤1
        if not self.limits.allow_parallel or self.limits.max_parallel_workers <= 1:
            for step in plan.steps:
                step.parallelizable = False
        return plan
