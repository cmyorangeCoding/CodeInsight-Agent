"""评估运行器：``python -m evals.run_eval``

功能：
- 指定任务集 / 类别 / 单题 / 数量上限；
- 指定对比配置（variant）：direct / react / plan / plan_verifier / plan_replan；
- 指定重复次数、并发数、超时、Token 预算；
- 复用 ``get_llm()`` 与现有编排层（``Orchestrator``），
  不把 API Key 写入评估文件；
- 每个「任务 × 配置 × 重复」保存原始结果（答案、证据、运行记录、评分）；
- 自动评分（确定性）并输出汇总报告（答案质量 + 执行可靠性 + 资源开销）；
- 模型不可用时明确报告失败原因，不把模拟结果混入真实实验。

诚实性约定：
- 未真实执行的配置标记为「待测」，不填假数据；
- Token 拿不到用量记为 ``unknown``（不为 0）；成本无价格配置记为 ``unknown``；
- 失败样本保留在结果里，不删除以提高分数。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from evals import config as eval_config
from evals.dataset import load_tasks, validate_dataset
from evals.scoring import aggregate_scores, judge_with_llm, score_answer

from app.models.llm import get_llm, get_summarization_llm
from app.observability.callback import TraceCallback
from app.observability.token_budget import TokenBudget
from app.observability.tracer import Tracer
from app.indexing import build_local_provider, ensure_index
from app.agents.repository_agent import build_repository_tools, create_repository_agent_factory
from app.orchestration.executor import Orchestrator
from app.orchestration.limits import OrchestrationLimits
from app.orchestration.models import RunRecord, Strategy
from app.orchestration.records import aggregate_records
from app.orchestration.verifier import AnalyzerRepoAccess, Verifier
from app.rag.code_vector_store import create_session_factory
from app.rag.retriever import CodeRetriever
from app.config import settings

_REPOSITORY_ID = settings.repo.default_repository_id
_FORCE_MAP = {"direct": Strategy.DIRECT, "react": Strategy.REACT, "plan": Strategy.PLAN_EXECUTE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- 运行器
class EvalRunner:
    """一次评估会话：持有共享资源，按「任务 × 配置」执行并评分。

    说明：``Orchestrator`` 实例持有单次运行状态，故每次运行都新建；
    每个任务运行使用独立的 DB session（``SessionFactory()``），避免并发共享 session。
    """

    def __init__(
        self,
        llm,
        embeddings,
        store,
        checkpointer,
        session_factory,
        *,
        repeat: int = 1,
        timeout: float = eval_config.DEFAULT_TIMEOUT_SECONDS,
        max_total_tokens: int = eval_config.MAX_TOTAL_TOKENS,
        max_input_tokens: int = eval_config.MAX_INPUT_TOKENS,
        llm_judge: bool = False,
        user_id: str = "eval-user",
        routing_llm=None,
    ) -> None:
        self.llm = llm
        # 复杂度路由使用的模型；未注入时回退主模型（保持历史实验结果可比）
        self.routing_llm = routing_llm or llm
        self.embeddings = embeddings
        self.store = store
        self.checkpointer = checkpointer
        self.session_factory = session_factory
        self.repeat = max(1, repeat)
        self.timeout = timeout
        self.max_total_tokens = max_total_tokens
        self.max_input_tokens = max_input_tokens
        self.llm_judge = llm_judge
        self.user_id = user_id

    # -- 资源装配 -----------------------------------------------------------
    def _limits(self, variant_cfg: dict) -> OrchestrationLimits:
        base = OrchestrationLimits()
        replans = variant_cfg.get("replans", base.max_replans)
        return dataclasses.replace(base, max_replans=replans, max_duration_seconds=self.timeout)

    def _run_orch(self, task: dict, variant_cfg: dict, thread_id: str):
        """编排层流程（direct / react / plan[_verifier|_replan]）。"""
        session = self.session_factory()
        try:
            limits = self._limits(variant_cfg)
            budget = TokenBudget(
                max_total_tokens=self.max_total_tokens,
                max_input_tokens=self.max_input_tokens,
                reserved_output_tokens=eval_config.RESERVED_OUTPUT_TOKENS,
            )
            tracer = Tracer()

            # 每次运行构建独立 provider，避免跨任务串用
            provider = build_local_provider()

            retriever = CodeRetriever(
                session=session, embeddings=self.embeddings,
                repository_id=provider.repository_id, tracer=tracer,
            )

            tools, memory = build_repository_tools(
                provider=provider,
                token_budget=budget, store=self.store, repository_id=provider.repository_id,
                retriever=retriever, user_id=self.user_id, tracer=tracer,
            )
            repo_access = AnalyzerRepoAccess(provider._analyzer)
            verifier = (
                Verifier(repo=repo_access, llm=self.llm, limits=limits, tracer=tracer)
                if variant_cfg.get("verify", True)
                else None
            )
            orchestrator = Orchestrator(
                llm=self.llm,
                routing_llm=self.routing_llm,
                agent_factory=create_repository_agent_factory(self.llm, tools, budget, tracer, memory, self.checkpointer),
                budget=budget,
                tracer=tracer,
                limits=limits,
                verifier=verifier,
                model_name=self._model_name(),
                callbacks=[TraceCallback(tracer=tracer, token_budget=budget)],
                pricing=eval_config.PRICING,
                memory=memory,
            )
            forced = _FORCE_MAP.get(variant_cfg.get("force", ""))
            result = orchestrator.run(
                task["question"], question_id=task["id"], force_strategy=forced, thread_id=thread_id
            )
            return {
                "answer": result.answer,
                "record": result.record,
                "evidence_refs": orchestrator._collect_evidence(),
                "plan": result.plan,
                "verification": result.verification,
                "error": result.record.error,
            }
        finally:
            session.close()

    def _model_name(self) -> str:
        for attr in ("model_name", "model"):
            value = getattr(self.llm, attr, None)
            if isinstance(value, str) and value:
                return value
        return "unknown"

    # -- 单次运行 -----------------------------------------------------------
    def run_once(self, task: dict, variant: str, variant_cfg: dict, repeat: int) -> dict:
        thread_id = f"eval-{task['id']}-{variant}-{repeat}-{uuid.uuid4().hex[:6]}"
        try:
            outcome = self._run_orch(task, variant_cfg, thread_id)
        except Exception as exc:  # noqa: BLE001 - 装配阶段异常也要留记录
            outcome = {
                "answer": "", "record": None, "evidence_refs": [], "plan": None,
                "verification": None, "error": f"{type(exc).__name__}: {exc}",
            }

        record: RunRecord | None = outcome["record"]
        #确定性打分
        score = score_answer(
            task,
            outcome["answer"],
            outcome["evidence_refs"],
            run_status=(record.status if record else "failed"),
            verification_passed=(record.verification_passed if record else None),
            strategy=(record.strategy if record else variant),
            variant=variant,
            repeat=repeat,
        )
        #LLM 裁判
        if self.llm_judge and outcome["answer"].strip():
            judged = judge_with_llm(self.llm, task, outcome["answer"])
            if judged is not None:
                score.llm_judged = True
                score.llm_judge_score = float(judged.semantic_score)
                score.llm_judge_reason = judged.reasoning
                if judged.unsupported_claims:
                    score.forbidden_claims_found = list(
                        dict.fromkeys([*score.forbidden_claims_found, *judged.unsupported_claims])
                    )

        plan_summary = None
        if outcome["plan"] is not None:
            plan_summary = {
                "origin": outcome["plan"].origin,
                "status": str(outcome["plan"].status),
                "steps": [
                    {
                        "step_id": s.step_id,
                        "status": str(s.status),
                        "error": s.error,
                        # 保留步骤结论前 500 字符，便于事后诊断（完整内容见 answer）
                        "result_excerpt": (s.result or "")[:500] or None,
                    }
                    for s in outcome["plan"].steps
                ],
            }
        verification = None
        if outcome["verification"] is not None:
            v = outcome["verification"]
            verification = {
                "passed": v.passed,
                "issues": v.issues,
                "missing_evidence": v.missing_evidence,
                "unsupported_claims": v.unsupported_claims,
                "llm_used": v.llm_used,
            }

        return {
            "task_id": task["id"],
            "category": task["category"],
            "variant": variant,
            "repeat": repeat,
            "strategy": record.strategy if record else variant,
            "answer": outcome["answer"],
            "error": outcome["error"],
            "record": record.model_dump() if record else None,
            "plan": plan_summary,
            "verification": verification,
            "score": score.model_dump(),
        }


# --------------------------------------------------------------------------- 报告
def build_report(
    rows: list[dict],
    task_ids: list[str],
    variants: list[str],
    *,
    started_at: str,
    finished_at: str,
    notes: list[str],
) -> dict:
    from evals.scoring import ScoringResult

    #取出每条里的 score，转成 ScoringResult 列表，调用 aggregate_scores 算出质量指标（quality）
    scores = [ScoringResult(**r["score"]) for r in rows]
    quality = aggregate_scores(scores)

    #取出 run 记录，调用 aggregate_records 算出执行可靠性指标（reliability，成功率、耗时、token、工具调用这些）
    records: list[RunRecord] = []
    for r in rows:
        if r.get("record"):
            records.append(RunRecord(**r["record"]).model_copy(update={"strategy": r["variant"]}))
    reliability = aggregate_records(records)

    # 异常样本（模型波动 / 服务错误 / 超时）单独统计，不删除
    anomalies = [
        {"task_id": r["task_id"], "variant": r["variant"], "error": r["error"],
         "status": r["record"]["status"] if r.get("record") else "no-record"}
        for r in rows
        if r.get("error") or (r.get("record") and r["record"]["status"] == "failed")
    ]

    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "task_count": len(task_ids),
        "variants": variants,
        "repeats": sorted({r["repeat"] for r in rows}),
        "run_count": len(rows),
        "quality": quality,
        "reliability": reliability,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
        "notes": notes,
    }


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# 评估报告\n")
    lines.append(f"- 时间：{report['started_at']} → {report['finished_at']}")
    lines.append(f"- 任务数：{report['task_count']}  配置：{', '.join(report['variants'])}  "
                 f"重复：{report['repeats']}  运行数：{report['run_count']}")
    lines.append(f"- 异常样本：{report['anomaly_count']}（未删除，保留在原始结果中）")
    for note in report["notes"]:
        lines.append(f"- 备注：{note}")

    lines.append("\n## 答案质量（按配置）\n")
    lines.append("| 配置 | 运行 | 完成率 | 必要事实覆盖率 | 事实正确率 | 证据覆盖率 | 无证据断言率 | 综合质量分 | LLM裁判 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for variant, m in report["quality"].items():
        lines.append(
            f"| {variant} | {m['runs']} | {_fmt(m['completion_rate'])} | "
            f"{_fmt(m['avg_fact_coverage'])} | {_fmt(m['avg_fact_correctness'])} | "
            f"{_fmt(m['avg_evidence_coverage'])} | {_fmt(m['avg_unsupported_claim_rate'])} | "
            f"{_fmt(m['avg_quality_score'])} | "
            f"{m['llm_judged_runs']}运行/{_fmt(m['avg_llm_judge_score'])} |"
        )

    lines.append("\n## 答案质量（按类别细分）\n")
    lines.append("| 配置 | 类别 | 运行 | 完成率 | 必要事实覆盖率 | 证据覆盖率 |")
    lines.append("|---|---|---|---|---|---|")
    for variant, m in report["quality"].items():
        for category, c in sorted(m["by_category"].items()):
            lines.append(
                f"| {variant} | {category} | {c['runs']} | {_fmt(c['completion_rate'])} | "
                f"{_fmt(c['fact_coverage'])} | {_fmt(c['evidence_coverage'])} |"
            )

    lines.append("\n## 答案质量（按编排复杂度细分）\n")
    lines.append("口径（orchestration_complexity = 题目天然需要的检索步数 / 是否存在「上一步结果决定下一步」的依赖链，"
                 "与 difficulty「读懂难度」无关）：\n")
    lines.append("- low：一次检索或单个文件即可作答，无跨步依赖")
    lines.append("- medium：需 2~3 步，且存在依赖（先定位 A 才知道要看 B），或需跨 2~3 个文件对比")
    lines.append("- high：需 4 步以上，跨多个模块且中间结果需要拼装\n")
    lines.append("| 配置 | 复杂度 | 运行 | 完成率 | 必要事实覆盖率 | 证据覆盖率 |")
    lines.append("|---|---|---|---|---|---|")
    order = {"low": 0, "medium": 1, "high": 2}
    for variant, m in report["quality"].items():
        for cplx, c in sorted(m.get("by_complexity", {}).items(),
                              key=lambda kv: (order.get(kv[0], 9), kv[0])):
            lines.append(
                f"| {variant} | {cplx} | {c['runs']} | {_fmt(c['completion_rate'])} | "
                f"{_fmt(c['fact_coverage'])} | {_fmt(c['evidence_coverage'])} |"
            )

    lines.append("\n## 执行可靠性 & 资源开销（按配置）\n")
    lines.append(
        "| 配置 | 运行 | 成功率 | 验证通过率 | 工具失败率 | 重试率 | 重规划率 | "
        "平均耗时ms | 中位耗时ms | P95耗时ms | 平均Token | Token未知 | 平均工具调用 | 平均证据 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for variant, m in report["reliability"].items():
        lines.append(
            f"| {variant} | {m['runs']} | {_fmt(m['success_rate'])} | {_fmt(m['verification_pass_rate'])} | "
            f"{_fmt(m['tool_failure_rate'])} | {_fmt(m['retry_rate'])} | {_fmt(m['replan_rate'])} | "
            f"{_fmt(m['avg_duration_ms'])} | {_fmt(m['median_duration_ms'])} | {_fmt(m['p95_duration_ms'])} | "
            f"{_fmt(m['avg_total_tokens'])} | {m['token_unknown_runs']} | {_fmt(m['avg_tool_calls'])} | "
            f"{_fmt(m['avg_evidence'])} |"
        )

    if report["anomalies"]:
        lines.append("\n## 异常样本（保留，未删除）\n")
        lines.append("| 任务 | 配置 | 状态 | 错误 |")
        lines.append("|---|---|---|---|")
        for a in report["anomalies"][:40]:
            lines.append(f"| {a['task_id']} | {a['variant']} | {a['status']} | {_fmt_str(a['error'])} |")

    lines.append("\n## 指标口径\n")
    lines.append(
        "- 必要事实覆盖率 = 命中 required_facts / required_facts 总数（每条 fact 命中任一 any_of 即算命中）\n"
        "- 事实正确率 = 1 - 命中禁止结论数 / (命中事实数 + 命中禁止结论数)；无可判定事实时记 null\n"
        "- 证据覆盖率 = 被答案文本或工具证据定位的 required_evidence / 总数\n"
        "- 无证据断言率 = 命中禁止/不支持结论数 / 禁止结论总数（无禁止结论记 0）\n"
        "- 成功率 = status==success 的运行占比；验证通过率 = verification_passed 为 True 的占比\n"
        "- P95 耗时在样本 < 5 时记 null（不臆造）；Token 拿不到用量记 unknown（不为 0）\n"
        "- 成本仅在填入真实价格（evals/config.py PRICING）时才计算，否则 unknown\n"
    )
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def _fmt_str(s) -> str:
    if not s:
        return ""
    return str(s).replace("|", "/")[:120]


# --------------------------------------------------------------------------- CLI
def _select_tasks(tasks: list[dict], args) -> list[dict]:
    selected = tasks
    if args.category:
        cats = {c.strip() for c in args.category.split(",") if c.strip()}
        selected = [t for t in selected if t["category"] in cats]
    if args.ids:
        ids = {i.strip() for i in args.ids.split(",") if i.strip()}
        selected = [t for t in selected if t["id"] in ids]
    if args.limit:
        selected = selected[: args.limit]
    return selected


def _select_variants(args) -> list[str]:
    if not args.variants or args.variants == "all":
        return list(eval_config.VARIANTS.keys())
    names = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = [n for n in names if n not in eval_config.VARIANTS]
    if unknown:
        raise SystemExit(f"未知配置：{unknown}；可选：{list(eval_config.VARIANTS.keys())}")
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodeInsight-Agent 编排评估运行器")
    parser.add_argument("--tasks", default=None, help="任务集 JSONL 路径（默认 evals/dataset/tasks.jsonl）")
    parser.add_argument("--variants", default="all", help="对比配置，逗号分隔或 all")
    parser.add_argument("--category", default=None, help="只跑指定类别（逗号分隔）")
    parser.add_argument("--ids", default=None, help="只跑指定任务 id（逗号分隔）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个任务")
    parser.add_argument("--repeat", type=int, default=1, help="每个任务重复次数")
    parser.add_argument("--concurrency", type=int, default=1, help="任务级并发数（默认 1，顺序执行）")
    parser.add_argument("--timeout", type=float, default=eval_config.DEFAULT_TIMEOUT_SECONDS, help="单任务最大执行秒数")
    parser.add_argument("--max-total-tokens", type=int, default=eval_config.MAX_TOTAL_TOKENS)
    parser.add_argument("--llm-judge", action="store_true", help="额外启用 LLM 裁判（固定评分标准）")
    parser.add_argument("--validate-only", action="store_true", help="仅校验任务集，不调用模型")
    parser.add_argument("--out-dir", default=str(eval_config.RESULTS_DIR))
    parser.add_argument("--no-run-log", action="store_true", help="不写入原始结果文件（仅打印报告）")
    args = parser.parse_args(argv)

    #加载数据集
    tasks = load_tasks(Path(args.tasks) if args.tasks else None)
    ## 校验数据集格式、证据是否合法
    problems = validate_dataset(tasks)
    if problems:
        print("× 任务集校验未通过：")
        for p in problems:
            print(f"  - {p}")
        # 只校验模式，发现问题直接退出
        if args.validate_only:
            return 2
        print("  （继续执行，但结果可信度受影响）")
    elif args.validate_only:
        print(f"✓ 任务集校验通过：{len(tasks)} 个任务，类别分布完整，全部证据文件与锚点有效")
        return 0

    ## 筛选任务、筛选实验组
    selected = _select_tasks(tasks, args)
    variants = _select_variants(args)
    if not selected:
        print("没有匹配的任务，退出。")
        return 1

    print("=" * 66)
    print("  CodeInsight-Agent 评估运行器")
    print("=" * 66)
    print(f"  任务：{len(selected)} 个  配置：{variants}  重复：{args.repeat}  并发：{args.concurrency}")
    print(f"  超时：{args.timeout}s  总Token上限：{args.max_total_tokens}")

    # 模型可用性检查（不可用则明确报告，绝不混入模拟结果）， 初始化大模型，模型拿不到直接终止实验
    try:
        llm = get_llm()
        routing_llm = get_summarization_llm()
    except Exception as exc:  # noqa: BLE001
        print("× 无法初始化模型（get_llm 失败）："
              f"{type(exc).__name__}: {exc}")
        print("  请确认 .env 中的 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL / DASHSCOPE_MODEL 有效。")
        print("  未产生任何真实运行结果，本次实验中止（不填充模拟数据）。")
        return 3

    from app.memory.postgres_resources import open_memory_resources

    stamp = _stamp() # 生成本次实验唯一标识，用来命名报告文件
    started_at = _now_iso() # 记录评测开始时间
    notes: list[str] = [] # 报告里的备注信息

    # 打开Postgres向量库资源（pgvector、checkpointer）
    try:
        ctx = open_memory_resources()
        checkpointer, store, embeddings = ctx.__enter__()
    except Exception as exc:  # noqa: BLE001
        print(f"× 无法连接 PostgreSQL（记忆/持久化资源）：{type(exc).__name__}: {exc}")
        print("  请确认 DATABASE_URL 指向可用的 PostgreSQL（含 pgvector）。")
        return 3

    # 存放每一次 run_once 的返回结果，所有原始行数据
    rows: list[dict] = []
    try:
        session_factory = create_session_factory()
        # 确保向量索引可用（复用既有增量索引逻辑）
        bootstrap = session_factory()
        try:
            bootstrap_provider = build_local_provider()
            ensure_index(bootstrap, embeddings, bootstrap_provider, force=False)
        finally:
            bootstrap.close()

        # 实例化评测执行器
        runner = EvalRunner(
            llm,
            embeddings,
            store,
            checkpointer,
            session_factory,
            repeat=args.repeat,
            timeout=args.timeout,
            max_total_tokens=args.max_total_tokens,
            llm_judge=args.llm_judge,
            routing_llm=routing_llm,
        )

        # 生成全部待跑任务：任务 × 实验组 × 重复次数
        jobs = [(t, v, r) for t in selected for v in variants for r in range(args.repeat)]
        print(f"\n开始执行 {len(jobs)} 次运行 ...\n")

        # 并发执行分支：线程池跑
        if args.concurrency and args.concurrency > 1:
            if args.concurrency > 1:
                notes.append(f"任务级并发={args.concurrency}（每个任务独立 DB session；共享 store/checkpointer）")
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {
                    pool.submit(runner.run_once, t, v, eval_config.VARIANTS[v], r): (t, v, r)
                    for (t, v, r) in jobs
                }
                # 逐个获取线程结果
                for future in as_completed(futures):
                    t, v, r = futures[future]
                    try:
                        row = future.result()
                    except Exception as exc:  # noqa: BLE001
                        # 线程内部异常，包装成失败记录
                        row = {"task_id": t["id"], "category": t["category"], "variant": v,
                               "repeat": r, "strategy": v, "answer": "", "error": f"{type(exc).__name__}: {exc}",
                               "record": None, "plan": None, "verification": None,
                               "score": {"task_id": t["id"], "category": t["category"], "strategy": v,
                                         "variant": v, "repeat": r, "completed": False}}
                    rows.append(row)
                    #每跑完一条，控制台打印一行实时进度
                    _print_progress(row)
        else:
            # 顺序执行分支，逐个跑
            for (t, v, r) in jobs:
                row = runner.run_once(t, v, eval_config.VARIANTS[v], r)
                rows.append(row)
                #每跑完一条，控制台打印一行实时进度
                _print_progress(row)

        # 待测配置：数据集中存在但本次未执行的配置，明确标记（不填假数据）
        not_tested = [v for v in eval_config.VARIANTS if v not in variants]
        if not_tested:
            notes.append(f"待测配置（本次未执行）：{not_tested}")
    finally:
        # 评测结束，关闭数据库资源
        try:
            ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    finished_at = _now_iso() # 记录评测结束时间
    # 汇总所有结果，生成报告结构体
    report = build_report(rows, [t["id"] for t in selected], variants,
                          started_at=started_at, finished_at=finished_at, notes=notes)

    # 落盘原始结果与报告
    if not args.no_run_log:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = out_dir / f"{stamp}.jsonl"
        with raw_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        reports_dir = Path(eval_config.REPORTS_DIR)
        reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = reports_dir / f"{stamp}.json"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path = reports_dir / f"{stamp}.md"
        md_path.write_text(render_markdown(report), encoding="utf-8")
        print(f"\n原始结果：{raw_path}")
        print(f"汇总报告：{md_path}")
        print(f"报告 JSON：{json_path}")

    print("\n" + render_markdown(report))
    return 0


def _print_progress(row: dict) -> None:
    rec = row.get("record") or {}
    score = row.get("score") or {}
    status = rec.get("status", "?")
    tokens = rec.get("total_tokens")
    tokens_s = "unknown" if tokens is None else str(tokens)
    print(
        f"  [{row['variant']:<13}] {row['task_id']:<11} "
        f"status={status:<7} fact={_fmt(score.get('fact_coverage'))} "
        f"evid={_fmt(score.get('evidence_coverage'))} "
        f"tok={tokens_s:<8} ms={rec.get('duration_ms')}"
        + (f"  err={row['error']}" if row.get("error") else "")
    )


if __name__ == "__main__":
    sys.exit(main())
