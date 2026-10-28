"""执行器（编排层）：接收问题 → 判定策略 → 按需规划 → 调度执行 → 验证 → 有限重规划 → 报告。

职责边界：
- 只负责「编排控制循环」，单步内部的工具调用循环交给 LangChain 的 Agent（ReAct）。
- 复用现有 ``TokenBudget`` / ``Tracer`` / 仓库工具 / ``RepositoryAnalyzer``，不复制第二套工具。
- 所有上限集中来自 ``OrchestrationLimits``；达到上限一律安全终止并返回部分结果。
"""
from __future__ import annotations

import inspect
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.observability.budget_middleware import TokenBudgetExceededError
from app.orchestration.evidence import EvidenceCollector
from app.orchestration.limits import OrchestrationLimits
from app.orchestration.models import (
    EvidenceRef,
    FailureType,
    PlanStatus,
    PlanStep,
    RouteDecision,
    RunRecord,
    StepStatus,
    Strategy,
    TaskPlan,
    ToolResult,
    VerificationResult,
)
from app.orchestration.planner import PlanGenerator
from app.orchestration.verifier import Verifier


SYNTHESIS_PROMPT = (
    "你是代码仓库分析助手。请依据下面的步骤结果与证据清单，给出最终回答。\n"
    "要求：结论摘要 + 关键发现 + 代码证据（文件路径:行号、符号名）+ 未能确认的事项。\n"
    "只能使用给出的证据，不得编造文件路径或行号。\n"
    "如提供了用户长期偏好，请在不违反上述要求的前提下遵循（如输出语言、详略、引用格式）。\n"
    f"用户长期偏好（可能为空）：\n{{preferences}}\n"
    f"原问题：{{question}}\n"
    f"执行步骤结果：\n{{steps}}\n"
    f"证据清单：\n{{evidence}}"
)


class AgentFactory(Protocol):
    """构造单次执行所用的 Agent（mode: direct / react / step）。"""

    def __call__(self, mode: str) -> Any: ...


@dataclass
class OrchestrationResult:
    """一次编排运行的对外结果。"""

    question: str                  # 用户原始问题
    answer: str                    # 最终生成文本回答
    strategy: Strategy             # 执行策略
    route: RouteDecision           # 路由判定结果
    record: RunRecord              # 全链路运行日志与计费统计
    plan: TaskPlan | None = None   # 执行计划
    verification: VerificationResult | None = None
    tool_results: list[ToolResult] = field(default_factory=list)


def _now_iso() -> str:
    """返回UTC标准时间字符串，不带微秒，用于日志时间戳"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def last_ai_content(result: Any) -> str:
    """从 Agent 返回结果中提取最后一条 AI 回复文本。"""
    messages = (result or {}).get("messages", []) if isinstance(result, dict) else getattr(result, "messages", [])
    for message in reversed(list(messages)):
        if getattr(message, "type", "") == "ai":
            content = getattr(message, "content", "")
            text = content if isinstance(content, str) else str(content)
            if text.strip():
                return text
    return ""


class Orchestrator:
    """统一编排入口。"""

    def __init__(
        self,
        llm=None,
        agent_factory: AgentFactory | None = None,
        budget=None,
        tracer=None,
        limits: OrchestrationLimits | None = None,
        verifier: Verifier | None = None,
        plan_generator: PlanGenerator | None = None,
        model_name: str | None = None,
        callbacks: list | None = None,
        pricing: dict[str, dict[str, float]] | None = None,
        routing_llm=None,
        memory=None,
    ) -> None:
        self.llm = llm
        # 复杂度路由是「三分类」任务，不需要推理能力，可单独注入轻量模型；
        # 未注入时回退到主模型，保证既有调用方行为不变。
        self.routing_llm = routing_llm or llm
        self.agent_factory = agent_factory
        self.budget = budget
        self.tracer = tracer
        self.limits = limits or OrchestrationLimits()
        self.verifier = verifier
        self.plan_generator = plan_generator or PlanGenerator(llm, self.limits, tracer)
        self.model_name = model_name or self._infer_model_name(llm)
        self.callbacks = list(callbacks or [])
        self.pricing = pricing
        # 长期记忆（可选）：计划生成与汇总回答会读取用户偏好；
        # 单步执行器保持中立，不注入偏好、不带记忆工具。
        self.memory = memory
        self._preferences_cache: str | None = None
        self._planner_accepts_preferences = self._accepts_kwarg(
            getattr(self.plan_generator, "generate", None), "preferences"
        )
        self.collector = EvidenceCollector()
        self._start = 0.0
        self._run_id = ""
        self._retry_count = 0
        self._replan_count = 0
        self._failure_reason: str | None = None

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _infer_model_name(llm) -> str:
        for attr in ("model_name", "model"):
            value = getattr(llm, attr, None)
            if isinstance(value, str) and value:
                return value
        return "unknown"

    @staticmethod
    def _accepts_kwarg(func, name: str) -> bool:
        """判断可调用对象是否接受某个关键字参数（用于兼容外部注入的计划生成器）。"""
        if func is None:
            return False
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return False
        return name in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

    def _preferences_text(self) -> str:
        """读取并缓存当前用户的长期偏好文本；未注入 memory 时返回空串。

        缓存在单次 ``run()`` 开始时清空，避免同一 Orchestrator 实例跨轮拿到旧偏好。
        """
        if self.memory is None:
            return ""
        if self._preferences_cache is not None:
            return self._preferences_cache
        try:
            from app.memory.memory_context import MemoryContextBuilder

            builder = MemoryContextBuilder(self.memory)
            self._preferences_cache = builder.render_preferences(builder.load_user_preferences())
        except Exception as exc:  # noqa: BLE001 - 记忆不可用不应阻断编排
            self._log("memory_preferences_error", error=f"{type(exc).__name__}: {exc}")
            self._preferences_cache = ""
        return self._preferences_cache

    def _log(self, event_type: str, **data) -> None:
        if self.tracer is not None:
            try:
                self.tracer.add_event(event_type, **data)
            except Exception:
                pass

    #计算耗时
    def _elapsed(self) -> float:
        return time.perf_counter() - self._start

    #返回布尔值，判断是否 token 预算耗尽
    def _budget_blocked(self) -> bool:
        if self.budget is None:
            return False
        try:
            return bool(self.budget.summary().get("over_budget"))
        except Exception:  # noqa: BLE001
            return False

    #判断工具调用次数是否达到最大上限
    def _tools_exhausted(self) -> bool:
        return self.collector.tool_call_count() >= self.limits.max_tool_calls

    #判断是否超时
    def _time_exhausted(self) -> bool:
        return self._elapsed() >= self.limits.max_duration_seconds

    def _stop_reason(self) -> str | None:
        """返回需要立即停止的原因，无则 None。"""
        if self._time_exhausted():
            return "达到最大执行时长"
        if self._tools_exhausted():
            return "达到最大工具调用次数"
        if self._budget_blocked():
            return "Token 预算已耗尽"
        return None

    #统一创建 agent 并且调用，封装 agent 执行逻辑
    """
    - `mode`：direct /react/step，传给 agent 工厂，生成不同模式的 agent
    - `thread_id`：LangGraph 会话 id，保存对话上下文
    - 判断 agent_factory 为空 → 抛异常，必须注入工厂才能跑 
    """
    def _invoke(self, mode: str, messages: list, thread_id: str) -> Any:
        if self.agent_factory is None:
            raise RuntimeError("缺少 agent_factory，无法执行该策略")
        agent = self.agent_factory(mode)
        config = {
            "configurable": {"thread_id": thread_id},
            "callbacks": [self.collector, *self.callbacks],
        }
        # 所有模式都强制设 recursion_limit —— LangGraph 默认可能跑 200+ super-step，
        # 一旦 LLM 陷入"工具返回空 → 再调同一个工具"的循环就会无限跑。
        # LangGraph super-step 计数：模型调用 + 工具调用 + 中间件节点各算 1 个，
        # 实测 1 轮「模型+工具」 ≈ 3~4 super-step，故按 4 倍 + 余量换算。
        if mode == "step":
            config["recursion_limit"] = self.limits.step_max_model_calls * 4 + 4
        elif mode == "react":
            config["recursion_limit"] = self.limits.react_max_tool_calls * 4 + 10
        elif mode == "direct":
            config["recursion_limit"] = self.limits.direct_max_model_calls * 4 + 4
        # 未知模式也兜底：用 react 的限制
        else:
            config["recursion_limit"] = self.limits.react_max_tool_calls * 4 + 10
        return agent.invoke({"messages": messages}, config=config)

    # ------------------------------------------------------------------ 主入口
    def run(
        self,
        question: str,
        *,
        question_id: str | None = None,
        force_strategy: Strategy | None = None, #强制指定策略，绕过路由判断（调试用）
        thread_id: str | None = None,
    ) -> OrchestrationResult:
        """执行一次完整编排。"""
        from app.orchestration.complexity import ComplexityRouter

        self._start = time.perf_counter()
        self._run_id = f"run-{uuid.uuid4().hex[:12]}"  #生成本次编排唯一 id，run-xxxx，日志追踪用
        self._retry_count = 0
        self._replan_count = 0
        self._failure_reason = None
        self.collector = EvidenceCollector() #新建证据收集器实例，清空上一轮证据
        # 偏好按「轮」失效：同一轮内规划/汇总共用一次读取结果
        self._preferences_cache = None
        thread_id = thread_id or f"orch-{self._run_id}"

        started_at = _now_iso()
        router = ComplexityRouter(llm=self.routing_llm, tracer=self.tracer)
        route = router.route(question, force=force_strategy)
        self._log("orchestration_start", run_id=self._run_id, strategy=str(route.strategy))

        #初始化变量
        answer = ""
        plan: TaskPlan | None = None #任务计划，简单策略下是 None
        verification: VerificationResult | None = None
        status = "success"

        try:
            if route.strategy == Strategy.DIRECT:
                answer = self._run_single(question, mode="direct", thread_id=thread_id)
            elif route.strategy == Strategy.REACT:
                answer = self._run_single(question, mode="react", thread_id=thread_id)
            else:
                plan, answer, verification = self._run_plan(question, thread_id)
                #如果校验不通过
                if verification is not None and not verification.passed:
                    #有答案就标记 partial（部分成功），没有答案标记 failed
                    status = "partial" if answer else "failed"
        except TokenBudgetExceededError as exc:
            self._failure_reason = FailureType.BUDGET_EXHAUSTED.value
            answer = answer or f"（执行被 Token 预算中断：{exc}）"
            #只要收集到了证据，状态 partial；完全没证据则 failed
            status = "partial" if self._collect_evidence() else "failed"
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证留下运行记录
            self._failure_reason = FailureType.UNKNOWN.value
            self._log("orchestration_error", error=f"{type(exc).__name__}: {exc}")
            answer = answer or f"（执行失败：{type(exc).__name__}: {exc}）"
            status = "failed"

        # 不允许在尚有必要步骤未完成时宣告成功：计划状态非 COMPLETED 一律降级
        #只要：存在失败原因 或者 计划存在并且计划整体状态不是 COMPLETED，就降级状态：有答案→partial，无答案→failed
        if status == "success" and (
            self._failure_reason
            or (plan is not None and plan.status != PlanStatus.COMPLETED)
        ):
            status = "partial" if answer else "failed"

        duration_ms = round(self._elapsed() * 1000, 2)
        #调用_build_record，组装 RunRecord 全链路日志记录对象
        record = self._build_record(
            question_id=question_id,
            route=route,
            started_at=started_at,
            duration_ms=duration_ms,
            status=status,
            plan=plan,
            verification=verification,
        )
        #埋点：编排任务结束事件返回 OrchestrationResult 对外结果对象，一次性把所有信息返回上层调用方。
        self._log("orchestration_end", run_id=self._run_id, status=status, duration_ms=duration_ms)
        return OrchestrationResult(
            question=question,
            answer=answer,
            strategy=route.strategy,
            route=route,
            record=record,
            plan=plan,
            verification=verification,
            tool_results=self.collector.records(),
        )

    # ------------------------------------------------------------------ 单步策略
    def _run_single(self, question: str, mode: str, thread_id: str) -> str:
        result = self._invoke(mode, [{"role": "user", "content": question}], thread_id)
        return last_ai_content(result)

    # ------------------------------------------------------------------ 计划策略
    #规划→执行→验证→重规划
    def _run_plan(self, question: str, thread_id: str) -> tuple[TaskPlan, str, VerificationResult | None]:
        preferences = self._preferences_text()
        if self._planner_accepts_preferences:
            plan = self.plan_generator.generate(question, preferences=preferences)
        else:
            # 兼容外部注入的旧版计划生成器（签名只接受 question）
            plan = self.plan_generator.generate(question)
        if preferences:
            self._log("preferences_injected", stage="planner")
        self._log("plan_created", run_id=self._run_id, origin=plan.origin, steps=len(plan.steps))

        verification: VerificationResult | None = None
        while True:
            #按依赖顺序执行当前 plan 里面所有可运行步骤
            self._execute_plan(plan, question, thread_id)

            #收集本次执行全部去重证据
            evidence = self._collect_evidence()
            #调用`_compose_answer`，基于步骤结果 + 证据生成最终回答文本
            answer = self._compose_answer(question, plan, evidence)

            #检查熔断条件：超时 / 工具超限 /token 耗尽
            stop = self._stop_reason()
            if stop:
                self._failure_reason = FailureType.BUDGET_EXHAUSTED.value
                self._log("plan_stopped", reason=stop)

            #如果没注入校验器 Verifier，不做校验，直接退出循环
            if self.verifier is None:
                break

            #调用 verifier 做校验：检查证据缺失、断言无依据、逻辑问题，返回 VerificationResult
            verification = self.verifier.verify(question, answer, evidence, plan)
            self._log("verification_result", passed=verification.passed, replan_count=self._replan_count)

            #退出循环条件 1：校验通过，或者触发熔断，停止重规划
            if verification.passed or stop:
                break
            #退出循环条件 2：重规划次数到达上限，不能继续新增补救步骤；标记校验失败，退出
            if self._replan_count >= self.limits.max_replans:
                self._failure_reason = self._failure_reason or FailureType.VERIFICATION_FAILED.value
                self._log("replan_limit_reached", replan_count=self._replan_count)
                break

            #根据校验出来的问题，生成补救步骤列表
            additions = self._build_replan_steps(plan, verification)
            #如果生成不出补救步骤，标记校验失败，终止循环
            if not additions:
                self._failure_reason = self._failure_reason or FailureType.VERIFICATION_FAILED.value
                break
            self._replan_count += 1
            plan = self._merge_replan(plan, additions) #合并原有计划 + 新增补救步骤（深拷贝旧 plan，不修改原始对象）
            self._log("replan", replan_count=self._replan_count, added=len(additions))

        plan.refresh_status()
        return plan, answer, verification

    def _execute_plan(self, plan: TaskPlan, question: str, thread_id: str) -> None:
        """按依赖顺序调度执行；仅对无依赖且标记可并行的步骤开放并行。"""
        #把整个任务计划状态改成 RUNNING
        plan.status = PlanStatus.RUNNING
        while True:
            #前置依赖失败的步骤，直接标记 skipped，不再执行
            self._skip_unrunnable(plan)

            #检查熔断，触发就跳出调度循环
            stop = self._stop_reason()
            if stop:
                self._log("plan_stopped", reason=stop)
                break

            #`_ready_steps`筛选就绪步骤：PENDING、所有依赖已经 COMPLETED
            ready = self._ready_steps(plan)
            if not ready:
                break

            #从就绪步骤里，筛选满足全部条件的可并行步骤
            """
            1. step.parallelizable=True（计划生成时标记可以并行）
            2. 全局配置允许并行`allow_parallel`
            3. 最大并行 worker 数量 > 1
            """
            parallel = [
                s for s in ready
                if s.parallelizable and self.limits.allow_parallel and self.limits.max_parallel_workers > 1
            ]
            if len(parallel) > 1:
                #可并行的步骤数量 > 1：开多线程并行跑这批步骤
                self._run_parallel(parallel, question, thread_id, plan)
            else:
                #否则：串行，拿第一个就绪步骤，带重试执行
                self._run_step_with_retry(ready[0], question, thread_id, plan)
        plan.refresh_status()

    @staticmethod
    def _ready_steps(plan: TaskPlan) -> list[PlanStep]:
        """只调度 PENDING 且依赖已完成的步骤；FAILED 步骤不再重复调度（避免死循环）。"""
        by_id = {s.step_id: s for s in plan.steps}
        ready: list[PlanStep] = []
        for step in plan.steps:
            if step.status != StepStatus.PENDING:
                continue
            deps_ok = all(
                by_id.get(dep) is not None and by_id[dep].status == StepStatus.COMPLETED
                for dep in step.depends_on
            )
            if deps_ok:
                ready.append(step)
        return ready

    @staticmethod
    def _skip_unrunnable(plan: TaskPlan) -> None:
        """依赖失败/跳过的步骤不执行，直接标记 skipped。"""
        by_id = {s.step_id: s for s in plan.steps}
        changed = True
        while changed:
            changed = False
            for step in plan.steps:
                if step.status != StepStatus.PENDING:
                    continue
                for dep in step.depends_on:
                    dep_step = by_id.get(dep)
                    if dep_step is None or dep_step.status in (StepStatus.FAILED, StepStatus.SKIPPED):
                        step.transition(StepStatus.SKIPPED)
                        step.error = f"前置依赖 {dep} 未成功完成（状态 {dep_step.status if dep_step else 'missing'}）"
                        step.failure_type = FailureType.PLAN_DEPENDENCY_ERROR
                        changed = True
                        break


    def _run_step_with_retry(self, step: PlanStep, question: str, thread_id: str, plan: TaskPlan) -> None:
        """单步骤带重试"""
        attempts = self.limits.max_step_retries + 1  #最大重试次数 + 1（包含第一次原始执行）
        for attempt in range(attempts):
            self._run_step(step, question, thread_id, plan)
            if step.status == StepStatus.COMPLETED:
                return
            # 预算类失败（超时 / Token / 递归上限）重试也只会重复烧钱，直接放弃
            if step.failure_type == FailureType.BUDGET_EXHAUSTED:
                self._log("step_no_retry", step_id=step.step_id, reason="budget_exhausted")
                return
            if attempt < attempts - 1:
                self._retry_count += 1
                step.retry_count += 1
                self._log("step_retry", step_id=step.step_id, retry_count=step.retry_count)
                try:
                    step.transition(StepStatus.RUNNING)
                except ValueError:
                    return

    def _run_parallel(self, steps: list[PlanStep], question: str, thread_id: str, plan: TaskPlan) -> None:
        """并行执行互不依赖的步骤；每个 worker 独立归属自己的 step_id。"""
        #配置最大并行 worker 和待执行步骤数量两者更小的值，防止线程过多
        workers = min(self.limits.max_parallel_workers, len(steps))
        self._log("parallel_batch", step_ids=[s.step_id for s in steps], workers=workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._run_step, step, question, f"{thread_id}-{step.step_id}", plan): step
                for step in steps
            }
            for future in as_completed(futures):
                step = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - 单个并行任务异常不影响其他任务
                    if step.status == StepStatus.RUNNING:
                        step.transition(StepStatus.FAILED)
                    step.error = f"{type(exc).__name__}: {exc}"
                    step.failure_type = FailureType.UNKNOWN
                    self._log("step_error", step_id=step.step_id, error=step.error)


    #计划的单步执行
    def _run_step(self, step: PlanStep, question: str, thread_id: str, plan: TaskPlan) -> None:
        if step.status == StepStatus.PENDING:
            step.transition(StepStatus.RUNNING)
        try:
            prompt = self._step_prompt(question, step, plan)
            # 每个步骤使用独立 thread_id：不挂 checkpointer、不积累会话历史，
            # 上下文只通过 prompt（含前序成果）显式传递。
            result = self._invoke_step_with_timeout(prompt, step, thread_id)
            step.result = last_ai_content(result) or "（该步骤未返回文本结果）"
            step.evidence = self.collector.evidence_for(step.step_id)
            #状态流转为 COMPLETED
            step.transition(StepStatus.COMPLETED)
            self._log("step_completed", step_id=step.step_id, evidence=len(step.evidence))
        except Exception as exc:  # noqa: BLE001 - 单步失败不中止整任务
            if step.status == StepStatus.RUNNING:
                step.transition(StepStatus.FAILED)
            step.error = f"{type(exc).__name__}: {exc}"
            step.failure_type = self._classify_step_failure(step, exc)
            self._log("step_error", step_id=step.step_id, error=step.error,
                      failure_type=str(step.failure_type))

    def _invoke_step_with_timeout(self, prompt: str, step: PlanStep, thread_id: str) -> Any:
        """单步调用带硬超时：把全任务剩余时间作为上限，超时按「预算耗尽」记失败。

        两点注意：
        - ``EvidenceCollector`` 用 threading.local 记录当前步骤，因此 step 归属
          必须在真正执行 Agent 的那个线程里设置；
        - Python 无法强杀运行中的线程，超时后 ``shutdown(wait=False)`` 立即返回，
          底层 Agent（已被 recursion_limit 约束）会在后台自然结束，编排层不再等它。
        """
        remaining = self.limits.max_duration_seconds - self._elapsed()
        if remaining <= 0:
            raise TimeoutError("全任务时间预算已耗尽，步骤不再启动")

        def job() -> Any:
            self.collector.set_step(step.step_id)
            try:
                return self._invoke(
                    "step",
                    [{"role": "user", "content": prompt}],
                    f"{thread_id}-{step.step_id}",
                )
            finally:
                self.collector.set_step(None)

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(job).result(timeout=max(10.0, remaining))
        except FuturesTimeoutError as exc:
            raise TimeoutError(
                f"单步执行超过剩余时间预算（{remaining:.0f}s）"
            ) from exc
        finally:
            pool.shutdown(wait=False)

    #给步骤异常分类，返回 FailureType 枚举：
    def _classify_step_failure(self, step: PlanStep, exc: Exception) -> FailureType:
        # GraphRecursionError = 单步模型调用轮数耗尽，属于「调用次数预算」耗尽
        from langgraph.errors import GraphRecursionError
        if isinstance(exc, (TokenBudgetExceededError, TimeoutError, GraphRecursionError)):
            return FailureType.BUDGET_EXHAUSTED
        if any(r.status == "error" for r in self.collector.records(step.step_id)):
            return FailureType.TOOL_FAILURE
        return FailureType.MODEL_ERROR

    def _step_prompt(self, question: str, step: PlanStep, plan: TaskPlan) -> str:
        """把步骤描述 + 前序步骤成果 + 已收集证据拼成单步执行指令。

        关键设计：把「前面步骤已经查到什么」显式告诉当前步骤，让它直接去读
        目标位置核对/补充，而不是从零重新检索一遍仓库（这是 plan 模式 token
        与时间双爆炸的主因）。
        """
        prior = self._prior_results(step, plan)
        known = self._known_evidence_digest()
        parts = [
            f"总体问题：{question}",
            f"你当前只需要完成这个分析步骤：{step.description}",
            "优先利用下面给出的前序成果与已定位证据（直接按 路径:行号 读取核对），"
            "只有缺少信息时才重新检索；给出本步骤的事实性结论（含文件路径与行号）。",
        ]
        if prior:
            parts.append("前序步骤已完成的结论（可直接引用，不要重复执行）：\n" + prior)
        if known:
            parts.append("已定位的代码证据（如需细节请直接 read_file 对应位置）：\n" + known)
        return "\n\n".join(parts)

    #收集当前步骤所有依赖步骤的结果文本：
    def _prior_results(self, step: PlanStep, plan: TaskPlan) -> str:
        """汇总全部已完成前序步骤的结论（不只直接依赖：已完成步骤的成果天然可复用）。"""
        lines: list[str] = []
        for s in plan.steps:
            if s.step_id == step.step_id:
                continue
            if s.status == StepStatus.COMPLETED and s.result:
                lines.append(f"[{s.step_id}] {s.result}")
        return "\n".join(lines)[:4000]

    def _known_evidence_digest(self, limit: int = 30) -> str:
        """当前已收集证据的紧凑清单（路径:行号 符号），供步骤直接按位置读取。"""
        refs = self._collect_evidence()[:limit]
        return "\n".join(f"- {ref.location()}" for ref in refs)

    # ------------------------------------------------------------------ 汇总与重规划

    #全局证据汇总 + 去重
    def _collect_evidence(self) -> list[EvidenceRef]:
        seen: set[tuple] = set()
        out: list[EvidenceRef] = []
        for ref in self.collector.all_evidence():
            #用元组`(文件路径，起始行，结束行，符号名)`作为唯一 key
            key = (ref.file_path, ref.line_start, ref.line_end, ref.symbol_name)
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
        return out

    def _compose_answer(self, question: str, plan: TaskPlan, evidence: list[EvidenceRef]) -> str:
        """优先使用「汇总」步骤的产出；否则用 LLM / 确定性拼接。"""
        summary_steps = [s for s in plan.steps if ("汇总" in s.description or "结论" in s.description)
                         and s.status == StepStatus.COMPLETED and s.result]
        if summary_steps:
            return summary_steps[-1].result or ""

        completed = [s for s in plan.steps if s.status == StepStatus.COMPLETED and s.result]
        if not completed:
            return ""

        steps_text = "\n".join(f"[{s.step_id}] {s.result}" for s in completed)
        if self.llm is not None and not self._stop_reason():
            try:
                evidence_text = "\n".join(f"- {r.location()}" for r in evidence) or "（无证据）"
                preferences = self._preferences_text()
                if preferences:
                    self._log("preferences_injected", stage="synthesis")
                response = self.llm.invoke(
                    SYNTHESIS_PROMPT.format(
                        question=question,
                        steps=steps_text,
                        evidence=evidence_text,
                        preferences=preferences or "（无）",
                    )
                )
                content = getattr(response, "content", "")
                if isinstance(content, str) and content.strip():
                    return content
            except Exception as exc:  # noqa: BLE001 - 汇总失败回落确定性拼接
                self._log("synthesis_error", error=f"{type(exc).__name__}: {exc}")

        header = "以下为各部分分析结果（部分步骤未能完成，结论为部分结果）：\n"
        return header + steps_text

    def _build_replan_steps(self, plan: TaskPlan, verification: VerificationResult) -> list[PlanStep]:
        """根据验证问题生成补救步骤（保留已完成步骤，不无条件重跑）。"""
        # #计算还能新增多少步骤（全局最大步骤数减去现有步骤），没配额直接返回空列表，不再新增补救步骤。
        remaining = self.limits.max_steps - len(plan.steps)
        if remaining <= 0:
            return []

        #把验证器输出的三类问题合并：缺失证据、无依据断言、逻辑 issues
        needs = [*verification.missing_evidence, *verification.unsupported_claims, *verification.issues]
        needs = [n for n in needs if n][:remaining]
        #所有已经 COMPLETED 的步骤 id，新补救步骤依赖已完成的步骤
        base_deps = [s.step_id for s in plan.steps if s.status == StepStatus.COMPLETED]
        steps: list[PlanStep] = []
        for i, need in enumerate(needs, 1):
            steps.append(
                PlanStep(
                    step_id=f"r{self._replan_count + 1}-{i}",
                    description=f"针对验证指出的问题补充检索与核对：{need}",
                    depends_on=list(base_deps[:1]),
                )
            )
        if steps:
            steps.append(
                PlanStep(
                    step_id=f"r{self._replan_count + 1}-sum",
                    description="基于补充证据重新汇总结论并核对文件路径、符号与行号",
                    depends_on=[s.step_id for s in steps],
                )
            )
        return steps[:remaining]

    @staticmethod
    def _merge_replan(plan: TaskPlan, additions: list[PlanStep]) -> TaskPlan:
        merged = plan.model_copy(deep=True)
        merged.steps.extend(additions) #追加新增补救步骤到 steps 列表
        merged.origin = "replanned"  #代表这是重规划后的计划
        merged.refresh_status()
        merged.status = PlanStatus.RUNNING
        return merged

    # ------------------------------------------------------------------ 记录
    """构建完整运行记录 RunRecord，用于入库保存任务全链路日志。局部导入 token 计算工具。"""
    def _build_record(
        self,
        *,
        question_id: str | None,
        route: RouteDecision,
        started_at: str,
        duration_ms: float,
        status: str,
        plan: TaskPlan | None,
        verification: VerificationResult | None,
    ) -> RunRecord:
        from app.orchestration.records import estimate_cost, resolve_token_usage

        #读取预算统计，解析输入 token、输出 token、总 token；估算本次调用费用，获取费用计算来源。
        budget_summary = None
        if self.budget is not None:
            try:
                budget_summary = self.budget.summary()
            except Exception:  # noqa: BLE001
                budget_summary = None
        inp, out, total, token_source = resolve_token_usage(budget_summary)
        cost, cost_source = estimate_cost(self.model_name, inp, out, getattr(self, "pricing", None))

        failures = [s.failure_type.value for s in (plan.steps if plan else []) if s.failure_type]
        failure_reason = self._failure_reason or (failures[0] if failures else None)

        return RunRecord(
            run_id=self._run_id,
            question_id=question_id,
            strategy=str(route.strategy),
            model_name=self.model_name,
            started_at=started_at,
            finished_at=_now_iso(),
            duration_ms=duration_ms,
            status=status,
            tool_call_count=self.collector.tool_call_count(),
            retry_count=self._retry_count,
            replan_count=self._replan_count,
            input_tokens=inp,
            output_tokens=out,
            total_tokens=total,
            token_source=token_source,
            estimated_cost=cost,
            cost_source=cost_source,
            evidence_count=len(self._collect_evidence()),
            verification_passed=verification.passed if verification else None,
            failure_reason=failure_reason,
            plan_steps_total=len(plan.steps) if plan else 0,
            plan_steps_completed=sum(1 for s in plan.steps if s.status == StepStatus.COMPLETED) if plan else 0,
            route_source=route.source,
            route_reason=route.reason,
            limits=self.limits.as_dict(),
        )
