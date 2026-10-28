"""评估评分器：确定性指标（默认）+ 可选 LLM 裁判。

口径说明（避免「用长度代替正确性」之类的含糊打分）：

- ``fact_coverage``（必要事实覆盖率）：
  ``required_facts`` 中被答案命中的数量 / ``required_facts`` 总数。
  每条 fact 提供 ``any_of`` 候选串，命中任一即算命中。
- ``fact_correctness``（事实正确率）：
  在答案断言的事实中，未命中「禁止/不支持结论」的比例。
  计算：``1 - forbidden_found / max(1, fact_hits + forbidden_found)``；
  当答案没有命中任何事实且没有禁止结论时记为 ``None``（无法判定，不臆造）。
- ``evidence_coverage``（关键结论证据覆盖率）：
  ``required_evidence`` 中被「答案文本」或「工具证据引用」定位到的数量 / 总数。
  单条证据命中 = 答案文本包含该 ``file_path``，或工具证据中出现同 ``file_path``
  （且若能提供 ``symbol`` 则符号也需匹配）。
- ``unsupported_claim_rate``（无证据断言比例）：
  命中的「禁止/不支持结论」数量 / 禁止结论总数（无禁止结论时记 0.0）。
- ``completed``（任务完成）：运行状态为 ``success`` 且答案非空。
- ``quality_score``（确定性加权分）：
  ``fact_weight * fact_coverage + evidence_weight * evidence_coverage - penalty``，
  权重来自任务 ``scoring_rubric``；penalty 为命中的禁止结论占比。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

_WS_RE = re.compile(r"\s+")

#文本归一化。全部小写、连续空白压缩成单个空格。
def _norm(text: str) -> str:
    """归一化用于匹配：小写 + 折叠空白。"""
    return _WS_RE.sub(" ", (text or "").lower())


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle.lower() in haystack

#单条必需事实的命中记录
class FactHit(BaseModel):
    text: str
    matched: str | None = None       # 命中的候选串（None = 未命中）
    any_of: list[str] = Field(default_factory=list)  #该事实的多个候选匹配词，命中任意一个就算这条事实拿到分


#单条证据的命中记录（对应上一节 dataset 里的 `required_evidence`）
class EvidenceHit(BaseModel):
    file_path: str
    symbol: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    note: str | None = None
    matched_in_answer: bool = False #答案文本里直接写了这个文件路径
    matched_in_evidence: bool = False #Agent 调用工具检索返回的证据列表里包含这个文件路径 + 符号匹配（如果指定了 symbol）

    @property
    def hit(self) -> bool: #只要matched_in_answer 和 matched_in_evidence 有一个为true,即为命中
        return self.matched_in_answer or self.matched_in_evidence


#单条任务单次运行的完整打分结果对象，是这个模块最核心的结构体
class ScoringResult(BaseModel):
    """单个「任务 × 配置」的评分结果。"""

    task_id: str
    category: str
    orchestration_complexity: str | None = None   #题目天然需要的编排复杂度（low/medium/high）
    strategy: str
    variant: str | None = None
    repeat: int = 0

    # 答案质量
    required_fact_count: int = 0
    required_fact_hits: int = 0
    fact_coverage: float = 0.0
    fact_correctness: float | None = None
    fact_hits: list[FactHit] = Field(default_factory=list)
    forbidden_claims_found: list[str] = Field(default_factory=list)
    unsupported_claim_rate: float = 0.0

    # 证据
    required_evidence_count: int = 0
    required_evidence_hits: int = 0
    evidence_coverage: float = 0.0
    evidence_hits: list[EvidenceHit] = Field(default_factory=list)
    evidence_misses: list[EvidenceHit] = Field(default_factory=list)

    # 完成 & 验证
    completed: bool = False
    run_status: str | None = None
    verification_passed: bool | None = None

    # 综合
    quality_score: float = 0.0
    # LLM 裁判（可选）
    llm_judge_score: float | None = None
    llm_judge_reason: str | None = None
    llm_judged: bool = False

    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- 确定性评分
def _fact_hits(answer_norm: str, facts: list[dict]) -> list[FactHit]:
    """逐条检查每条**必需事实**是否命中，返回明细列表"""
    hits: list[FactHit] = []
    for fact in facts:
        text = fact.get("text", "")
        candidates = fact.get("any_of") or []
        matched = next((c for c in candidates if _contains(answer_norm, c)), None)
        hits.append(FactHit(text=text, matched=matched, any_of=list(candidates)))
    return hits


def _evidence_hits(
    answer_norm: str,
    evidence_items: list[dict],
    evidence_refs: list[Any],
) -> list[EvidenceHit]:
    ref_paths: dict[str, set[str]] = {}
    ref_symbols: set[str] = set()
    for ref in evidence_refs or []:
        path = getattr(ref, "file_path", None)
        if not path:
            continue
        ref_paths.setdefault(path.lower(), set())
        sym = getattr(ref, "symbol_name", None)
        if sym:
            ref_symbols.add(sym.lower())

    hits: list[EvidenceHit] = []
    for item in evidence_items:
        path = item.get("file_path", "")
        symbol = item.get("symbol")
        # 与工具证据里的路径匹配：完全相等或其中之一是另一方的子串（应对方括号等差异）
        in_evidence = any(
            path.lower() == p or path.lower() in p or p in path.lower()
            for p in ref_paths
        )
        if in_evidence and symbol:
            in_evidence = symbol.lower() in ref_symbols
        in_answer = _contains(answer_norm, path)
        hits.append(
            EvidenceHit(
                file_path=path,
                symbol=symbol,
                line_start=item.get("line_start"),
                line_end=item.get("line_end"),
                note=item.get("note"),
                matched_in_answer=in_answer,
                matched_in_evidence=in_evidence,
            )
        )
    return hits


def score_answer(
    task: dict, #样本数据集
    answer: str, #Agent 输出的回答文本
    evidence_refs: list[Any] | None = None, #Agent 检索工具返回的证据对象列表
    *,
    run_status: str | None = None, #任务运行状态
    verification_passed: bool | None = None,
    strategy: str = "",
    variant: str | None = None,
    repeat: int = 0,
) -> ScoringResult:
    """对一次运行结果做确定性评分。"""
    answer_norm = _norm(answer)
    facts = task.get("required_facts", []) or []
    required_evidence = task.get("required_evidence", []) or []
    forbidden = task.get("forbidden_or_unsupported_claims", []) or []
    rubric = task.get("scoring_rubric", {}) or {} #打分权重配置
    fact_weight = float(rubric.get("fact_weight", 0.6))
    evidence_weight = float(rubric.get("evidence_weight", 0.4))

    fhits = _fact_hits(answer_norm, facts)  #返回 `FactHit` 明细列表，记录每条事实是否命中、命中了哪个 any_of 候选
    fact_hits = sum(1 for h in fhits if h.matched) #统计命中的事实总数
    fact_coverage = fact_hits / len(facts) if facts else 0.0  #事实覆盖率 = 命中事实 / 全部必需事实；没有事实时固定 0.0

    ev_hits = _evidence_hits(answer_norm, required_evidence, evidence_refs or [])  #`EvidenceHit` 明细列表，每条证据标记：是回答文本命中，还是工具检索证据命中
    ev_hit_count = sum(1 for h in ev_hits if h.hit)
    evidence_coverage = ev_hit_count / len(required_evidence) if required_evidence else 0.0 #证据覆盖率 = 命中证据 / 全部要求证据；无要求证据时 0.0

    found_forbidden = [c for c in forbidden if _contains(answer_norm, c)]
    denom = fact_hits + len(found_forbidden)
    fact_correctness = (1 - len(found_forbidden) / denom) if denom > 0 else None  #事实正确率
    unsupported_rate = (len(found_forbidden) / len(forbidden)) if forbidden else 0.0  #禁止断言命中比例

    completed = bool(answer and answer.strip()) and (run_status == "success")

    penalty = unsupported_rate * 0.5
    quality = max(0.0, fact_weight * fact_coverage + evidence_weight * evidence_coverage - penalty)

    notes: list[str] = []
    if not answer.strip():
        notes.append("答案为空")
    if forbidden and not found_forbidden and fact_hits == 0:
        notes.append("无可判定事实命中，正确率未判定")

    return ScoringResult(
        task_id=task.get("id", ""),
        category=task.get("category", ""),
        orchestration_complexity=task.get("orchestration_complexity"),
        strategy=strategy,
        variant=variant,
        repeat=repeat,
        required_fact_count=len(facts),
        required_fact_hits=fact_hits,
        fact_coverage=round(fact_coverage, 4),
        fact_correctness=None if fact_correctness is None else round(fact_correctness, 4),
        fact_hits=fhits,
        forbidden_claims_found=found_forbidden,
        unsupported_claim_rate=round(unsupported_rate, 4),
        required_evidence_count=len(required_evidence),
        required_evidence_hits=ev_hit_count,
        evidence_coverage=round(evidence_coverage, 4),
        evidence_hits=[h for h in ev_hits if h.hit],
        evidence_misses=[h for h in ev_hits if not h.hit],
        completed=completed,
        run_status=run_status,
        verification_passed=verification_passed,
        quality_score=round(quality, 4),
        notes=notes,
    )


# --------------------------------------------------------------------------- 可选 LLM 裁判
JUDGE_PROMPT = (
    "你是严格的代码库分析评分员。请对照「标准答案必需事实」评估下面的回答。\n"
    "评分规则（不要再自行发明标准）：\n"
    "- 逐条列出「必需事实」是否被回答覆盖；\n"
    "- 指出回答中任何无法由代码证据支持、或与事实矛盾的断言；\n"
    "- semantic_score 取 0~5 的整数，5 5表示全部必需事实被正确覆盖且无无依据断言。\n\n"
    "问题：{question}\n\n"
    "必需事实：\n{facts}\n\n"
    "禁止/不支持结论：\n{forbidden}\n\n"
    "待评回答：\n{answer}"
)


class LLMJudgeOutput(BaseModel):
    semantic_score: int = Field(ge=0, le=5)
    covered_facts: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    reasoning: str = ""


def judge_with_llm(llm, task: dict, answer: str) -> LLMJudgeOutput | None:
    """LLM 裁判（固定评分标准）。模型不可用或输出不合法时返回 None。"""
    if llm is None:
        return None
    facts = "\n".join(f"- {f.get('text', '')}" for f in task.get("required_facts", []))
    forbidden = "\n".join(f"- {c}" for c in task.get("forbidden_or_unsupported_claims", [])) or "（无）"
    prompt = JUDGE_PROMPT.format(
        question=task.get("question", ""), facts=facts, forbidden=forbidden, answer=answer
    )
    try:
        structured = llm.with_structured_output(LLMJudgeOutput)
        return structured.invoke(prompt)
    except Exception:  # noqa: BLE001 - 裁判失败不影响确定性指标
        return None


# --------------------------------------------------------------------------- 聚合
def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def aggregate_scores(results: list[ScoringResult]) -> dict[str, Any]:
    """按 variant 聚合答案质量指标（按类别细分）。"""
    by_variant: dict[str, list[ScoringResult]] = {}
    for r in results:
        by_variant.setdefault(r.variant or r.strategy or "unknown", []).append(r)

    out: dict[str, Any] = {}
    for variant, group in by_variant.items():
        facts = [r.fact_coverage for r in group]
        evs = [r.evidence_coverage for r in group]
        corr = [r.fact_correctness for r in group if r.fact_correctness is not None]
        unsup = [r.unsupported_claim_rate for r in group]
        quality = [r.quality_score for r in group]
        by_category: dict[str, Any] = {}
        by_complexity: dict[str, Any] = {}
        for r in group:
            c = by_category.setdefault(r.category, {"runs": 0, "fact_coverage": 0.0, "evidence_coverage": 0.0, "completed": 0})
            c["runs"] += 1
            c["fact_coverage"] += r.fact_coverage
            c["evidence_coverage"] += r.evidence_coverage
            c["completed"] += 1 if r.completed else 0

            # 按题目天然编排复杂度分组：用于判断 Plan 策略在哪一档才有优势
            k = r.orchestration_complexity or "unlabeled"
            x = by_complexity.setdefault(k, {"runs": 0, "fact_coverage": 0.0, "evidence_coverage": 0.0, "completed": 0})
            x["runs"] += 1
            x["fact_coverage"] += r.fact_coverage
            x["evidence_coverage"] += r.evidence_coverage
            x["completed"] += 1 if r.completed else 0
        for bucket in (by_category, by_complexity):
            for c in bucket.values():
                n = c["runs"] or 1
                c["fact_coverage"] = round(c["fact_coverage"] / n, 4)
                c["evidence_coverage"] = round(c["evidence_coverage"] / n, 4)
                c["completion_rate"] = round(c["completed"] / n, 4)
                c.pop("completed", None)
        out[variant] = {
            "runs": len(group),
            "completion_rate": _avg([1.0 if r.completed else 0.0 for r in group]),
            "avg_fact_coverage": _avg(facts),
            "avg_fact_correctness": _avg(corr),
            "avg_evidence_coverage": _avg(evs),
            "avg_unsupported_claim_rate": _avg(unsup),
            "avg_quality_score": _avg(quality),
            "llm_judged_runs": sum(1 for r in group if r.llm_judged),
            "avg_llm_judge_score": _avg([r.llm_judge_score for r in group if r.llm_judge_score is not None]),
            "by_category": by_category,
            "by_complexity": by_complexity,
        }
    return out
