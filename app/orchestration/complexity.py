"""任务复杂度路由：规则优先 + 模型辅助 + 确定性兜底。

规则决定「最低策略档位」，模型只能在规则给出的档位之上做调整：
- 明确要求深入分析/整体架构/调用链的问题，不会因为模型判断简单而跳过必要分析步骤。
- 明确指定文件/类/函数的简单问题，优先 Direct，避免无谓的规划开销。
- 规则置信度不足时才询问模型；模型不可用时回落规则结论（确定性兜底）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.orchestration.models import RouteDecision, Strategy

# 策略档位：数值越大越复杂，用于「只升不降」的保护
_ORDER: dict[Strategy, int] = {
    Strategy.DIRECT: 0,
    Strategy.REACT: 1,
    Strategy.PLAN_EXECUTE: 2,
}

# 强复杂信号：只要命中其一，确定性判为 Plan-and-Execute（deep analysis 不可降级）
_PLAN_STRONG_SIGNALS = (
    "调用链", "调用关系", "整体架构", "架构分析", "整体流程", "端到端", "全流程",
    "模块之间", "如何协作", "怎么协作", "协同", "子系统", "完整", "全面",
    "深入", "详细分析", "代码审查", "审查", "review", "重构建议", "风险",
    "性能分析", "逐层", "从入口", "跨模块",
)

# 中等复杂信号
_PLAN_WEAK_SIGNALS = (
    "多个模块", "多处", "各模块", "top-level", "trace", "依赖关系", "设计",
    "为什么这样", "权衡", "对比",
)

# 简单问题信号（定位型）
_DIRECT_SIGNALS = (
    "在哪里定义", "在哪定义", "在哪个文件", "哪个文件", "在哪里读取", "在哪读取",
    "哪个类", "哪个函数", "哪一行", "定义在哪", "在哪里使用", "配置项", "环境变量",
    "有没有", "是否存在",
)


class ComplexityVerdict(BaseModel):
    """模型侧复杂度判断结果。"""

    strategy: Literal["direct", "react", "plan_execute"]
    reason: str = Field(default="", description="简要说明判断依据")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class ComplexityRouter:
    """复杂度路由器。"""

    def __init__(self, llm=None, tracer=None, use_llm: bool = True):
        self.llm = llm
        self.tracer = tracer
        self.use_llm = use_llm #开关，True 启用 LLM 辅助，False 只用纯规则判断

    def _log(self, **data) -> None:
        if self.tracer is not None:
            try:
                self.tracer.add_event("route_decision", **data)
            except Exception:
                pass

    # ------------------------------------------------------------------ 规则
    @staticmethod
    def rule_route(question: str) -> RouteDecision:
        """纯规则的确定性判断。"""
        text = (question or "").strip()
        lowered = text.lower()
        matched: list[str] = []

        strong = [s for s in _PLAN_STRONG_SIGNALS if s in text or s in lowered]
        weak = [s for s in _PLAN_WEAK_SIGNALS if s in text or s in lowered]
        direct = [s for s in _DIRECT_SIGNALS if s in text or s in lowered]

        if strong:
            matched = strong
            # 同时命中定位词时，若问题很短且只问位置，仍可回落 ReAct
            if direct and len(text) <= 40 and not weak:
                return RouteDecision(
                    strategy=Strategy.REACT,
                    reason=f"同时命中复杂与定位信号，长度较短，先按 ReAct 处理；命中={matched[:3]}",
                    source="rule",
                    confidence=0.6,
                    matched_signals=matched[:5],
                )
            return RouteDecision(
                strategy=Strategy.REACT,
                reason=f"命中强复杂信号，需要多维度/跨模块分析；命中={matched[:3]}（plan 仅手动启用，自动路由按 ReAct）",
                source="rule",
                confidence=1.0,
                matched_signals=matched[:5],
            )

        if weak:
            return RouteDecision(
                strategy=Strategy.REACT,
                reason=f"命中中等复杂信号，先多轮检索再判断；命中={weak[:3]}",
                source="rule",
                confidence=0.5,
                matched_signals=weak[:5],
            )

        if direct and len(text) <= 80:
            return RouteDecision(
                strategy=Strategy.DIRECT,
                reason=f"定位型简单问题，一次检索即可回答；命中={direct[:3]}",
                source="rule",
                confidence=0.8,
                matched_signals=direct[:5],
            )

        # 提到具体文件路径 → 倾向 Direct
        if ".py" in lowered and len(text) <= 120:
            return RouteDecision(
                strategy=Strategy.DIRECT,
                reason="问题中给出具体文件路径，按 Direct 处理",
                source="rule",
                confidence=0.7,
                matched_signals=["file_path"],
            )

        return RouteDecision(
            strategy=Strategy.REACT,
            reason="未命中明确信号，按默认 ReAct 处理",
            source="rule",
            confidence=0.5,
            matched_signals=[],
        )

    # ------------------------------------------------------------------ 主流程
    def route(self, question: str, force: Strategy | None = None) -> RouteDecision:
        """返回最终路由结论（确定性兜底保证不会因模型失败而失序）。"""
        if force is not None:
            decision = RouteDecision(
                strategy=force,
                reason="调用方显式指定策略",
                source="forced",
                confidence=1.0,
                matched_signals=["forced"],
            )
            self._log(strategy=str(decision.strategy), source="forced", reason=decision.reason)
            return decision

        rule_decision = self.rule_route(question)
        # 规则已经很确定 / 不允许用模型 → 直接用规则
        if not self.use_llm or self.llm is None or rule_decision.confidence >= 0.8:
            self._log(
                strategy=str(rule_decision.strategy),
                source="rule",
                reason=rule_decision.reason,
                confidence=rule_decision.confidence,
            )
            return rule_decision

        try:
            verdict = self._llm_route(question)
        except Exception as exc:  # noqa: BLE001 - 模型不可用时回落规则
            self._log(
                strategy=str(rule_decision.strategy),
                source="rule",
                reason=f"{rule_decision.reason}（模型判断失败，回落规则：{type(exc).__name__}）",
                confidence=rule_decision.confidence,
            )
            return rule_decision

        llm_strategy = Strategy(verdict.strategy)
        # 只升不降：模型不得把规则判定的复杂任务降级
        final = llm_strategy if _ORDER[llm_strategy] >= _ORDER[rule_decision.strategy] else rule_decision.strategy
        # 自动路由只在 Direct / ReAct 之间选择：
        # Plan-and-Execute 实测开销大（见 evals/reports/CONCLUSIONS.md），仅允许手动强制启用
        if final == Strategy.PLAN_EXECUTE:
            final = Strategy.REACT
        decision = RouteDecision(
            strategy=final,
            reason=f"规则={rule_decision.strategy}({rule_decision.reason})；模型={llm_strategy}({verdict.reason})"
                   + ("；plan 已降级为 react（plan 仅手动启用）" if llm_strategy == Strategy.PLAN_EXECUTE else ""),
            source="llm",
            confidence=verdict.confidence,
            matched_signals=rule_decision.matched_signals,
        )
        self._log(
            strategy=str(decision.strategy),
            source="llm",
            reason=decision.reason,
            confidence=decision.confidence,
        )
        return decision

    def _llm_route(self, question: str) -> ComplexityVerdict:
        structured = self.llm.with_structured_output(ComplexityVerdict)
        prompt = (
            "你是代码仓库分析任务的路由器。请判断回答下面这个问题所需的最低执行策略：\n"
            "- direct：文件位置、类/函数定义、单个配置项等，一次检索或少量工具调用即可回答；\n"
            "- react：需要多轮检索、读文件、观察结果后继续调用工具的问题（含跨模块调用链、"
            "架构分析、代码审查、历史决策等所有复杂问题）。\n"
            "只能从 direct / react 中选择（plan_execute 为手动模式，不在自动路由范围）。\n"
            f"问题：{question}\n"
            "只返回策略与简要理由。"
        )
        verdict = structured.invoke(prompt)
        # 模型可能仍返回 plan_execute（旧提示词习惯），强制归一到 react
        if Strategy(verdict.strategy) == Strategy.PLAN_EXECUTE:
            verdict = ComplexityVerdict(strategy="react", reason=verdict.reason, confidence=verdict.confidence)
        return verdict
