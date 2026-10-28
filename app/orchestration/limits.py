"""编排层的集中配置：所有上限、并发、重试策略都在这里定义。

要求「集中配置，不要散落在代码中」：执行器、计划器、验证器、运行记录
统一从 ``OrchestrationLimits`` 读取限制值，避免多处硬编码。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrchestrationLimits:
    """一次任务编排的硬性上限与策略参数。"""

    # ---- 重规划与重试上限 ----
    max_replans: int = 2                 # 最大重规划次数
    max_step_retries: int = 1            # 单个步骤最大重试次数
    max_plan_repair_attempts: int = 1    # 计划格式修复尝试次数

    # ---- 执行规模上限 ----
    max_tool_calls: int = 40             # 全任务最大工具调用次数
    max_steps: int = 5                   # 单个计划最大步骤数（步骤越少，每步能做的工作越聚焦）
    max_duration_seconds: float = 300.0  # 全任务最大执行时长（秒）

    # ---- 并发 ----
    max_parallel_workers: int = 1        # 1 = 顺序执行（默认，最稳）；>1 才启用并行

    # ---- 单步 Agent 限制 ----
    step_max_model_calls: int = 5        # 单个步骤内 Agent 的最大模型调用轮数（经 recursion_limit 强制约束）

    # ---- ReAct / Direct 模式限制 ----
    # LangGraph recursion_limit 按 super-step 计数（模型、工具、中间件各算 1 个），
    # 实测 1 轮「模型+工具」 ≈ 3~4 super-step，故统一乘 4 + 余量
    react_max_tool_calls: int = 25       # ReAct 模式最大工具调用次数（对应 recursion_limit ≈ 110）
    direct_max_model_calls: int = 3      # Direct 模式最大模型调用轮数（不应该调工具，兜底防止死循环）

    # ---- 验证器 ----
    enable_llm_verification: bool = True      # 是否启用 LLM 语义验证
    min_evidence_refs: int = 1                # 结论至少需要的证据条数

    # ---- 并行安全 ----
    allow_parallel: bool = False         # 默认关闭并行；开启需提供独立的检索器工厂

    def as_dict(self) -> dict:
        """序列化配置（写入运行记录，便于复现实验）。"""
        return {
            "max_replans": self.max_replans,
            "max_step_retries": self.max_step_retries,
            "max_plan_repair_attempts": self.max_plan_repair_attempts,
            "max_tool_calls": self.max_tool_calls,
            "max_steps": self.max_steps,
            "max_duration_seconds": self.max_duration_seconds,
            "max_parallel_workers": self.max_parallel_workers,
            "step_max_model_calls": self.step_max_model_calls,
            "react_max_tool_calls": self.react_max_tool_calls,
            "direct_max_model_calls": self.direct_max_model_calls,
            "enable_llm_verification": self.enable_llm_verification,
            "min_evidence_refs": self.min_evidence_refs,
            "allow_parallel": self.allow_parallel,
        }
