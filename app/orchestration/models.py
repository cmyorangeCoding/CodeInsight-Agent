"""编排层的结构化数据模型。

全部使用 Pydantic v2，保证计划、证据、验证结果都是可校验、可序列化的结构，
而不是把模型输出的自然语言当作状态管理机制。
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

"""
链路：
LLM输出JSON → PlanDraft（Pydantic校验） → 转换成TaskPlan + PlanStep实例 → 送入Executor`
"""

# --------------------------------------------------------------------------- 枚举
class Strategy(str, Enum):
    """执行策略。"""

    DIRECT = "direct"                    # 一次检索/少量工具调用即可回答
    REACT = "react"                      # 多轮工具交互
    PLAN_EXECUTE = "plan_execute"        # 规划 → 执行 → 验证 → 有限重规划

    __str__ = str.__str__


class StepStatus(str, Enum):
    """步骤执行状态。"""

    PENDING = "pending"       #等待执行
    RUNNING = "running"       #正在执行
    COMPLETED = "completed"   #执行成功完成
    FAILED = "failed"         #执行失败
    SKIPPED = "skipped"       #跳过不执行

    __str__ = str.__str__


class PlanStatus(str, Enum):
    """任务（计划）整体状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"      # 部分步骤成功、部分失败/跳过
    FAILED = "failed"

    __str__ = str.__str__


class FailureType(str, Enum):
    """失败类型，用于决定不同处理策略。"""

    TOOL_FAILURE = "tool_failure"                    # 工具暂时失败
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # 检索证据不足
    PLAN_DEPENDENCY_ERROR = "plan_dependency_error"  # 计划依赖关系错误
    VERIFICATION_FAILED = "verification_failed"      # 结果验证失败
    BUDGET_EXHAUSTED = "budget_exhausted"            # Token / 时间 / 调用次数耗尽
    MODEL_ERROR = "model_error"                      # 模型服务错误
    UNKNOWN = "unknown"

    __str__ = str.__str__


# 合法的步骤状态转换表：不在表中的转换一律拒绝
_ALLOWED_TRANSITIONS: dict[StepStatus, set[StepStatus]] = {
    StepStatus.PENDING: {StepStatus.RUNNING, StepStatus.SKIPPED},
    StepStatus.RUNNING: {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED},
    StepStatus.FAILED: {StepStatus.RUNNING, StepStatus.SKIPPED},  # 允许重试
    StepStatus.COMPLETED: set(),
    StepStatus.SKIPPED: set(),
}


# --------------------------------------------------------------------------- 证据
class EvidenceRef(BaseModel):
    """一条证据引用：仓库相对路径 + 符号/片段 + 起止行号 + 与结论的关联。"""

    file_path: str
    symbol_name: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    snippet: str | None = None
    note: str | None = None            # 该证据与当前结论的关联说明
    source_tool: str | None = None     # 产生该证据的工具名
    verified: bool | None = None       # 验证器确定性校验结果（None = 未校验）

    def location(self) -> str:
        """人类可读定位串，如 ``app/models/llm.py:10-18 符号:get_llm``。"""
        loc = self.file_path
        if self.line_start:
            loc += f":{self.line_start}"
            if self.line_end and self.line_end != self.line_start:
                loc += f"-{self.line_end}"
        if self.symbol_name:
            loc += f" 符号:{self.symbol_name}"
        return loc


class ToolResult(BaseModel):
    """统一的工具调用结果格式。"""

    tool_name: str          # 调用的工具名字
    args_summary: str = ""  # 调用工具时，入参的简短摘要（不存完整大参数，节省token）
    status: Literal["success", "error"] = "success"  # 工具调用状态，只能是二选一：成功 / 出错
    elapsed_ms: float | None = None  # 工具执行耗时，单位毫秒，用于观测、预算控制
    result_summary: str = ""   # 工具返回内容的摘要（给LLM看，不是原始完整返回，控制token）
    evidence: list[EvidenceRef] = Field(default_factory=list)   # 本次调用产生的证据引用，传给verifier做校验
    error: str | None = None   # 错误详情字符串，status=error的时候才填充
    step_id: str | None = None   # 关联的PlanStep的id，标记这个工具结果属于任务计划里哪一步


# --------------------------------------------------------------------------- 计划


class PlanStep(BaseModel):
    """单个计划步骤。"""

    step_id: str
    description: str
    status: StepStatus = StepStatus.PENDING
    depends_on: list[str] = Field(default_factory=list) #依赖其他步骤 ID
    result: str | None = None  #步骤最终产出文本
    evidence: list[EvidenceRef] = Field(default_factory=list)
    error: str | None = None
    retry_count: int = 0 #当前重试次数
    failure_type: FailureType | None = None
    # 是否可与其他无依赖步骤并行执行（由计划器标记）
    parallelizable: bool = False

    #处于未跑过和失败可以重试两种状态时该步骤才能被调度器执行
    def can_run(self) -> bool:
        return self.status in (StepStatus.PENDING, StepStatus.FAILED)

    def transition(self, new_status: StepStatus) -> None:
        """执行状态转换，非法转换直接抛错（不可只依赖自然语言描述状态）。"""
        if new_status == self.status:
            return
        allowed = _ALLOWED_TRANSITIONS[self.status]
        if new_status not in allowed:
            raise ValueError(
                f"非法状态转换：{self.status} -> {new_status}（步骤 {self.step_id}）"
            )
        self.status = new_status


class TaskPlan(BaseModel):
    """结构化任务计划：一个任务 = 一个 TaskPlan，包含若干可追踪状态的步骤。"""

    task_id: str = Field(default_factory=lambda: f"task-{uuid.uuid4().hex[:8]}")
    goal: str  #顶层任务目标，用户原始需求
    steps: list[PlanStep] = Field(default_factory=list) #`PlanStep`列表
    status: PlanStatus = PlanStatus.PENDING #任务整体状态
    dependencies: list[str] = Field(default_factory=list)  # 任务级前置依赖（根任务为空）
    result: str | None = None  #最终给用户的回答
    evidence: list[EvidenceRef] = Field(default_factory=list) #汇总所有 PlanStep 里的 EvidenceRef
    error: str | None = None
    retry_count: int = 0
    # 计划来源：llm / repaired / fallback
    # - llm：LLM初次拆解生成的原始计划
    # - repaired：失败后，重规划修复后的计划
    # - fallback：LLM规划失败，兜底备用计划
    origin: str = "llm"

    def step(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    # 判断所有步骤全部 COMPLETED，全部成功返回 True
    def all_completed(self) -> bool:
        return all(s.status == StepStatus.COMPLETED for s in self.steps)

    # 判断是否还有未结束的步骤
    def has_unfinished(self) -> bool:
        return any(
            s.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.FAILED)
            for s in self.steps
        )

    def refresh_status(self) -> PlanStatus:
        """根据步骤状态刷新任务整体状态。"""
        # 如果没有任何步骤 → 任务标记 FAILED
        if not self.steps:
            self.status = PlanStatus.FAILED
            return self.status
        # 收集全部步骤的状态集合`statuses`
        statuses = {s.status for s in self.steps}
        #全部步骤都是 COMPLETED → `PlanStatus.COMPLETED`（任务整体成功）
        if statuses <= {StepStatus.COMPLETED}:
            self.status = PlanStatus.COMPLETED
        #存在 PENDING 或者 RUNNING 步骤 → `PlanStatus.RUNNING`（任务正在执行中）
        elif StepStatus.RUNNING in statuses or StepStatus.PENDING in statuses:
            self.status = PlanStatus.RUNNING
        #既有成功步骤，同时存在 FAILED / SKIPPED 步骤 → `PlanStatus.PARTIAL`（部分完成）
        elif StepStatus.COMPLETED in statuses and (
            StepStatus.FAILED in statuses or StepStatus.SKIPPED in statuses
        ):
            self.status = PlanStatus.PARTIAL
        #有 FAILED 步骤，且没有 pending/running → `PlanStatus.FAILED`（任务失败）
        elif StepStatus.FAILED in statuses:
            self.status = PlanStatus.FAILED
        #其他情况兜底 PARTIAL
        else:
            self.status = PlanStatus.PARTIAL
        return self.status


# --------------------------------------------------------------------------- 路由
class RouteDecision(BaseModel):
    """复杂度路由结论（带日志所需的可解释信息）。"""

    strategy: Strategy
    reason: str #记录为什么选这个策略
    #决策来源，标记这个路由结论是谁生成的
    # - "rule"：硬编码规则判断（关键词、长度、特征匹配）
    # - "llm"：由LLM评估任务复杂度给出路由判断
    # - "forced"：外部强制指定策略（调试 / 手动覆盖场景）
    source: Literal["rule", "llm", "forced"] = "rule"
    confidence: float = 1.0 #可信度
    matched_signals: list[str] = Field(default_factory=list) #命中的特征信号列表，用来溯源


# --------------------------------------------------------------------------- 验证
class VerificationResult(BaseModel):
    """验证器结构化结果。"""

    passed: bool #验证总开关，True = 校验通过；False = 校验失败，触发重规划 / 报错
    issues: list[str] = Field(default_factory=list) #发现的全部问题清单
    missing_evidence: list[str] = Field(default_factory=list)  #缺失证据，LLM 回答里引用了不存在 / 没拿到的材料
    unsupported_claims: list[str] = Field(default_factory=list)  #无证据支撑的断言，输出内容里没有证据可以证明的句子
    suggested_actions: list[str] = Field(default_factory=list) #验证失败时，给出修复建议
    deterministic_checks: dict[str, Any] = Field(default_factory=dict) #确定性硬规则校验结果
    llm_used: bool = False #标记本次验证是否调用了 LLM


class PlanDraftStep(BaseModel):
    """LLM 产出计划的步骤草稿（仅模型需要填写的字段）。"""

    step_id: str #LLM 生成步骤编号
    description: str #该步骤任务描述
    depends_on: list[str] = Field(default_factory=list) #依赖的 step_id，构建 DAG
    parallelizable: bool = False #LLM 标记该步骤是否可以并行

    @field_validator("step_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("step_id 不能为空")
        return v


class PlanDraft(BaseModel):
    """LLM 产出计划的整体草稿。"""

    goal: str #顶层任务目标
    steps: list[PlanDraftStep] #草稿步骤列表

    @field_validator("steps")
    @classmethod
    def _non_empty_steps(cls, v: list[PlanDraftStep]) -> list[PlanDraftStep]:
        if not v:
            raise ValueError("steps 不能为空")
        return v


# --------------------------------------------------------------------------- 运行记录
class RunRecord(BaseModel):
    """一次任务执行的统一运行记录（用于评估与对比）。"""

    #基础标识
    run_id: str #本次运行唯一 id
    question_id: str | None = None #评测数据集里的题目 id，做 benchmark 评估时用
    strategy: str #本次路由选择的策略名称，来自 RouteDecision
    model_name: str #本次使用的 LLM 模型
    #时间与状态
    started_at: str
    finished_at: str | None = None
    duration_ms: float | None = None
    status: str = "running"        # running / success / failed / partial
    #统计指标（核心观测
    tool_call_count: int = 0
    retry_count: int = 0
    replan_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_source: str = "unknown"  # provider / estimate / unknown
    estimated_cost: float | None = None
    cost_source: str = "unknown"   # pricing_config / unknown
    evidence_count: int = 0
    verification_passed: bool | None = None
    failure_reason: str | None = None
    # 附加诊断字段（不包含密钥、不包含完整文件内容）
    plan_steps_total: int = 0
    plan_steps_completed: int = 0
    route_source: str | None = None
    route_reason: str | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
