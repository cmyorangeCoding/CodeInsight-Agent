"""验证器：确定性校验优先，语义相关性交给 LLM 辅助。

能确定性判断的（文件是否存在、行号是否越界、符号是否真的出现、是否在无证据时
断言功能不存在）一律用程序校验
只有「证据是否真的支持结论」「是否存在矛盾」，这类语义判断才调用 LLM，且 LLM 不可用时不阻断整体流程
"""
from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.orchestration.limits import OrchestrationLimits
from app.orchestration.models import (
    EvidenceRef,
    TaskPlan,
    VerificationResult,
)

# 答案中出现的文件路径
_ANSWER_PATH_RE = re.compile(
    r"[\w./\\-]+\.(?:py|md|txt|toml|yml|yaml|json|java|js|ts)\b"
)
# 断言「不存在 / 未实现」的措辞（在无证据时属于不可支持结论）
_NEGATION_RE = re.compile(
    r"(未找到|没有找到|不存在|未实现|没有实现|没有使用|未使用|没有定义|未定义|no such|not found|does not exist)",
    re.IGNORECASE,
)


class RepoAccess(Protocol):
    """仓库只读访问抽象（便于测试注入假实现）。"""

    def exists(self, rel_path: str) -> bool: ...

    def read_text(self, rel_path: str) -> str | None: ...


class AnalyzerRepoAccess:
    """基于 ``RepositoryAnalyzer`` 的仓库只读访问实现。"""

    def __init__(self, analyzer) -> None:
        self.analyzer = analyzer

    def exists(self, rel_path: str) -> bool:
        p = (self.analyzer.repo_path / rel_path).resolve()
        # 防止读取仓库以外的外部文件（路径穿越防护）
        if self.analyzer.repo_path not in p.parents:
            return False
        # 文件真实存在，并且不在忽略清单内
        return p.is_file() and not self.analyzer._should_ignore(p)

    def read_text(self, rel_path: str) -> str | None:
        if not self.exists(rel_path):
            return None
        try:
            # 读取文件，限制最大字符数，避免超大文件OOM
            return self.analyzer.read_file(rel_path, max_chars=500000)
        except Exception:  # noqa: BLE001
            return None


class ProviderRepoAccess:
    """基于 ``RepositoryProvider`` 协议的只读访问（本地 / GitHub 远程通用）。

    用于 provider 没有暴露底层 ``RepositoryAnalyzer`` 的场景（如 GitHub 远程仓库）。
    文件是否存在的判断依赖 ``read_file`` 是否抛异常，远程模式下一次 exists 检查
    对应一次远程调用，Verifier 的证据数量通常很少，开销可接受。
    """

    def __init__(self, provider) -> None:
        self.provider = provider

    def exists(self, rel_path: str) -> bool:
        try:
            self.provider.read_file(rel_path, max_chars=1)
            return True
        except Exception:  # noqa: BLE001
            return False

    def read_text(self, rel_path: str) -> str | None:
        try:
            return self.provider.read_file(rel_path, max_chars=500000)
        except Exception:  # noqa: BLE001
            return None


class LLMVerdict(BaseModel):
    """LLM 语义验证结论。"""

    covered: bool = Field(description="回答是否覆盖了原问题的全部要求")
    unsupported_claims: list[str] = Field(default_factory=list) #没有证据支撑的断言
    missing_evidence: list[str] = Field(default_factory=list)   #缺少必要证据
    contradictions: list[str] = Field(default_factory=list)     #结论互相矛盾
    suggested_actions: list[str] = Field(default_factory=list)  #给出整改建议


VERIFIER_PROMPT = (
    "你是代码仓库分析答案的验证员。请只依据给出的【证据清单】判断回答是否可靠，"
    "不要引入证据之外的知识。\n"
    "需要检查：\n"
    "1) 回答是否覆盖原问题的要求；\n"
    "2) 关键结论是否有证据支持（证据之外的断言属于 unsupported_claims）；\n"
    "3) 是否存在相互矛盾的结论；\n"
    "4) 是否遗漏了必要的证据项。\n"
    f"原问题：{{question}}\n"
    f"回答：{{answer}}\n"
    f"证据清单：\n{{evidence}}"
)


class Verifier:
    """结果验证器。"""

    def __init__(
        self,
        repo: RepoAccess | None = None, #仓库访问对象，用来做文件、行号、符号的确定性校验；不传就跳过文件核验；
        llm=None, #大模型实例，只用来做语义校验；可以为空；
        limits: OrchestrationLimits | None = None,
        tracer=None,
    ) -> None:
        self.repo = repo
        self.llm = llm
        self.limits = limits or OrchestrationLimits()
        self.tracer = tracer

    def _log(self, **data) -> None:
        if self.tracer is not None:
            try:
                self.tracer.add_event("verification_event", **data)
            except Exception:
                pass

    # ------------------------------------------------------------ 确定性校验
    def check_evidence(self, evidence: list[EvidenceRef]) -> dict[str, Any]:
        """逐条核对文件存在性、行号范围、符号是否可核实。"""
        invalid_paths: list[str] = []
        out_of_range: list[str] = []
        missing_symbols: list[str] = []
        verified_count = 0

        for ref in evidence:
            #没有传入仓库访问对象 repo → 没法校验文件
            if self.repo is None:
                ref.verified = None
                continue
            #repo 找不到这个证据写的文件路径
            if not self.repo.exists(ref.file_path):
                ref.verified = False
                invalid_paths.append(ref.location())
                continue

            #文件存在，读取这个文件全部文本
            content = self.repo.read_text(ref.file_path)
            total_lines = len(content.splitlines()) if content else 0
            #标记这条证据是否合法
            ok = True
            if ref.line_start is not None:
                end = ref.line_end or ref.line_start
                if ref.line_start < 1 or end > max(total_lines, 1):
                    ok = False #行号越界
                    out_of_range.append(f"{ref.location()}（文件共 {total_lines} 行）")
            if ref.symbol_name and content is not None:
                if ref.symbol_name not in content:
                    ok = False #字符串不在文件文本
                    missing_symbols.append(f"{ref.location()} 符号未在文件中出现")
            ref.verified = ok
            if ok:
                verified_count += 1

        return {
            "evidence_total": len(evidence),   #总证据数量
            "verified_count": verified_count,  #成功校验数量
            "invalid_paths": invalid_paths,    #不存在路径
            "out_of_range_lines": out_of_range,#行号越界
            "missing_symbols": missing_symbols,#字符串不在文件文本
        }

    def check_answer_paths(self, answer: str) -> list[str]:
        """答案中引用的文件路径必须真实存在。"""
        if self.repo is None:
            return []
        bad: list[str] = []
        for path in dict.fromkeys(_ANSWER_PATH_RE.findall(answer or "")):
            norm = path.replace("\\", "/").lstrip("./")
            if not self.repo.exists(norm):
                bad.append(norm)
        return bad

    # ------------------------------------------------------------ 主流程
    def verify(
        self,
        question: str, #用户问题
        answer: str,   #输出答案
        evidence: list[EvidenceRef], #证据列表
        plan: TaskPlan | None = None,#任务计划（可选）
    ) -> VerificationResult:
        issues: list[str] = []            #逻辑矛盾、覆盖不全这类问题
        missing_evidence: list[str] = []  #证据缺失
        unsupported: list[str] = []       #无依据断言
        suggested: list[str] = []         #修复建议

        #确定性校验
        checks = self.check_evidence(evidence)
        checks["answer_bad_paths"] = self.check_answer_paths(answer)
        checks["evidence_count"] = len(evidence)

        if checks["invalid_paths"]:
            unsupported.extend(f"引用文件不存在：{p}" for p in checks["invalid_paths"])
            suggested.append("改用 search_code_rag 重新定位真实文件路径")
        if checks["out_of_range_lines"]:
            unsupported.extend(f"行号越界：{p}" for p in checks["out_of_range_lines"])
            suggested.append("以实际读取到的文件行号为准，不要沿用记忆中的行号")
        if checks["missing_symbols"]:
            unsupported.extend(f"符号无法核实：{p}" for p in checks["missing_symbols"])
            suggested.append("重新按符号名检索确认定义位置")
        if checks["answer_bad_paths"]:
            unsupported.extend(f"回答引用了仓库中不存在的文件：{p}" for p in checks["answer_bad_paths"])
            suggested.append("删除或修正答案中不真实存在的文件引用")

        # 证据数量不足
        if checks["evidence_count"] < self.limits.min_evidence_refs:
            missing_evidence.append(
                f"可用证据仅 {checks['evidence_count']} 条，低于要求 {self.limits.min_evidence_refs} 条"
            )
            suggested.append("补充检索关键实现文件")

        # 无证据时断言「不存在 / 未实现」
        if _NEGATION_RE.search(answer or "") and checks["evidence_count"] == 0:
            unsupported.append("在没有任何代码证据的情况下断言某项功能不存在/未实现")
            suggested.append("先检索再下结论，或明确说明‘未找到证据’而非‘不存在’")

        # 计划未完成 → 关键子任务遗漏
        if plan is not None:
            unfinished = [s.step_id for s in plan.steps if s.status.value in ("failed", "skipped")]
            if unfinished:
                missing_evidence.append(f"以下步骤未成功完成，结论可能缺少必要分析：{unfinished}")
                suggested.append("针对未完成步骤补充检索或读取文件")
            checks["unfinished_steps"] = unfinished

        llm_used = False
        #if 判断条件全部满足，才会执行 LLM 校验：
        # 1. 配置开启 llm 校验
        # 2. 实例有 llm 对象
        # 3. answer 不为空白字符串
        if (
            self.limits.enable_llm_verification
            and self.llm is not None
            and (answer or "").strip()
        ):
            try:
                verdict = self._llm_verify(question, answer, evidence)
                llm_used = True
                #回答没覆盖用户问题，加到 issues
                if not verdict.covered:
                    issues.append("模型判定回答未完全覆盖原问题要求")
                unsupported.extend(verdict.unsupported_claims)
                missing_evidence.extend(verdict.missing_evidence)
                issues.extend(f"矛盾结论：{c}" for c in verdict.contradictions)
                suggested.extend(verdict.suggested_actions)
                checks["llm_verdict"] = verdict.model_dump()
            except Exception as exc:  # noqa: BLE001 - LLM 不可用不阻断验证
                checks["llm_error"] = f"{type(exc).__name__}: {exc}"
                self._log(action="llm_verify_error", error=str(exc))

        #计算验证是否通过：unsupported 为空，并且 missing_evidence 为空，并且 issues 为空 → passed=True，否则 False
        passed = not unsupported and not missing_evidence and not issues
        result = VerificationResult(
            passed=passed,
            issues=_dedupe(issues),
            missing_evidence=_dedupe(missing_evidence),
            unsupported_claims=_dedupe(unsupported),
            suggested_actions=_dedupe(suggested),
            deterministic_checks=checks,
            llm_used=llm_used,
        )
        self._log(passed=passed, evidence_count=len(evidence), llm_used=llm_used,
                  issue_count=len(result.issues) + len(result.unsupported_claims) + len(result.missing_evidence))
        return result

    def _llm_verify(self, question: str, answer: str, evidence: list[EvidenceRef]) -> LLMVerdict:
        structured = self.llm.with_structured_output(LLMVerdict)
        evidence_text = "\n".join(f"- {ref.location()}" for ref in evidence) or "（无证据）"
        prompt = VERIFIER_PROMPT.format(question=question, answer=answer, evidence=evidence_text)
        return structured.invoke(prompt)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
