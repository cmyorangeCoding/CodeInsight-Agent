"""统一运行记录：JSONL 持久化、指标聚合、Token/成本口径。

设计取舍：
- 选择 JSONL 落盘（最简单、最易复现、不破坏现有架构），一行一条运行记录。
- 追加写入使用 O_APPEND 单次写整行 + 进程内锁，保证并发运行不会互相覆盖。
- Token 区分「供应商真实返回」与「未知」；不把拿不到用量的情况默认为 0。
- 成本只在有可信价格配置时计算，否则标记为 unknown。
"""
from __future__ import annotations

import json
import os
import statistics
import threading
from pathlib import Path

from app.orchestration.models import RunRecord

DEFAULT_RUN_LOG = "runs/orchestration_runs.jsonl"


class RunLog:
    """JSONL 运行记录读写。"""

    def __init__(self, path: str | Path = DEFAULT_RUN_LOG) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: RunRecord) -> None:
        """追加一条运行记录（并发安全：整行单次追加写入）。"""
        line = json.dumps(record.model_dump(), ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

    def load_all(self) -> list[RunRecord]:
        """读取全部运行记录；跳过损坏行而不是整体失败。"""
        if not self.path.exists():
            return []
        records: list[RunRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(RunRecord(**json.loads(line)))
                except Exception:  # noqa: BLE001 - 单行损坏不应影响其余记录
                    continue
        return records

    def find(self, run_id: str) -> RunRecord | None:
        """按 run_id 查出单次运行记录。"""
        for record in self.load_all():
            if record.run_id == run_id:
                return record
        return None


# ----------------------------------------------------------------------- Token
def resolve_token_usage(budget_summary: dict | None) -> tuple[int | None, int | None, int | None, str]:
    """从 TokenBudget 汇总结果解析 Token 用量与口径。

    返回 ``(input, output, total, source)``：
    - 供应商真实返回过用量 → source="provider"
    - 完全没有用量数据   → 三个值均为 None，source="unknown"（不得默认为 0）
    """
    if not budget_summary:
        return None, None, None, "unknown"
    inp = int(budget_summary.get("actual_input_tokens", 0) or 0)
    out = int(budget_summary.get("actual_output_tokens", 0) or 0)
    if inp == 0 and out == 0:
        return None, None, None, "unknown"
    return inp, out, inp + out, "provider"


def estimate_cost(
    model_name: str,
    input_tokens: int | None,
    output_tokens: int | None,
    pricing: dict[str, dict[str, float]] | None,
) -> tuple[float | None, str]:
    """按可信价格配置估算成本（单位：元）。价格缺失时返回 unknown，不伪造。"""
    if not pricing or model_name not in pricing:
        return None, "unknown"
    if input_tokens is None or output_tokens is None:
        return None, "unknown"
    p = pricing[model_name]
    cost = input_tokens / 1000 * p.get("input_per_1k", 0.0) + output_tokens / 1000 * p.get("output_per_1k", 0.0)
    return round(cost, 6), "pricing_config"


# ----------------------------------------------------------------------- 聚合
def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _percentile(values: list[float], pct: float) -> float | None:
    """P95 等分位数；样本不足时返回 None（不臆造）。"""
    if len(values) < 5:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1)
    return round(ordered[max(idx, 0)], 2)


def aggregate_records(records: list[RunRecord]) -> dict[str, dict]:
    """按策略汇总执行可靠性与资源开销指标。"""
    by_strategy: dict[str, list[RunRecord]] = {}
    for r in records:
        by_strategy.setdefault(r.strategy, []).append(r)

    summary: dict[str, dict] = {}
    for strategy, group in by_strategy.items():
        total = len(group)
        durations = [r.duration_ms for r in group if r.duration_ms is not None]
        tokens = [r.total_tokens for r in group if r.total_tokens is not None]
        tool_calls = [r.tool_call_count for r in group]

        def rate(pred) -> float | None:
            if not total:
                return None
            return round(sum(1 for r in group if pred(r)) / total, 4)

        summary[strategy] = {
            "runs": total,
            "success_rate": rate(lambda r: r.status == "success"),
            "verification_pass_rate": rate(lambda r: r.verification_passed is True),
            "tool_failure_rate": rate(lambda r: (r.failure_reason or "").startswith("tool_failure")),
            "retry_rate": rate(lambda r: r.retry_count > 0),
            "replan_rate": rate(lambda r: r.replan_count > 0),
            "timeout_rate": rate(lambda r: (r.failure_reason or "") == "budget_exhausted"),
            "avg_duration_ms": round(sum(durations) / len(durations), 2) if durations else None,
            "median_duration_ms": _median(durations),
            "p95_duration_ms": _percentile(durations, 95),
            "avg_total_tokens": round(sum(tokens) / len(tokens), 2) if tokens else None,
            "token_unknown_runs": sum(1 for r in group if r.token_source == "unknown"),
            "avg_tool_calls": round(sum(tool_calls) / total, 2) if total else None,
            "avg_evidence": round(sum(r.evidence_count for r in group) / total, 2) if total else None,
            "avg_cost": _avg_cost(group),
            "cost_unknown_runs": sum(1 for r in group if r.cost_source == "unknown"),
        }
    return summary


def _avg_cost(group: list[RunRecord]) -> float | None:
    costs = [r.estimated_cost for r in group if r.estimated_cost is not None]
    return round(sum(costs) / len(costs), 6) if costs else None
