"""事件类型映射：Tracer raw event type → RunEvent（前端 EventType）。

为什么要单独抽出来？
  api/main.py 和 db/runs.py 都要用到这个映射：
  - main.py：实时 SSE 推送 + _serialize_tracer_events（存 DB 时映射）
  - runs.py：run_to_dict（历史查看时，如果 DB 里存的是老 raw type，运行时重映射）

  放 main.py 里 runs.py 无法 import（循环导入），所以抽成独立模块。

映射原则：
  - 手动构造每个分支的完整字段 dict，保证前端收到的形状稳定
  - 兜底：未知 type 一律映射成 info（source=tracer），别丢数据
  - span_*（内部观测埋点）跳过，返回 None
"""
from __future__ import annotations

from typing import Optional


def map_tracer_to_run_event(event_type: str, tracer_data: dict) -> Optional[tuple[str, dict]]:
    """Tracer raw event → (RunEvent type, 映射后的数据 dict)。返回 None = 跳过。"""

    # ---- 1. tool_start → tool_started ----
    if event_type == "tool_start":
        return "tool_started", {
            "tool": tracer_data.get("tool", ""),
            "input_preview": str(tracer_data.get("input_preview", ""))[:100],
        }

    # ---- 2. tool_end → tool_completed ----
    if event_type == "tool_end":
        return "tool_completed", {
            "tool": tracer_data.get("tool", ""),
            "elapsed_ms": tracer_data.get("elapsed_ms"),
        }

    # ---- 3. tool_error → tool_completed + status=error ----
    if event_type == "tool_error":
        return "tool_completed", {
            "tool": tracer_data.get("tool", ""),
            "status": "error",
            "error": str(tracer_data.get("error", ""))[:200],
        }

    # ---- 4. rag_hybrid_search → retrieval_completed ----
    if event_type == "rag_hybrid_search":
        selected = tracer_data.get("selected_chunks", [])
        return "retrieval_completed", {
            "candidate_count": tracer_data.get("vector_count", 0) + tracer_data.get("keyword_count", 0),
            "selected_count": tracer_data.get("selected_count", 0),
            "elapsed_ms": tracer_data.get("elapsed_ms"),
            "chunks": [
                {
                    "file_path": c.get("file_path"),
                    "line_start": c.get("line_start"),
                    "line_end": c.get("line_end"),
                    "symbol_name": c.get("symbol_name"),
                    "rrf_score": c.get("rrf_score"),
                }
                for c in selected[:10]
            ],
        }

    # ---- 5. model_end → usage_updated ----
    if event_type == "model_end":
        return "usage_updated", {
            "input_tokens": tracer_data.get("input_tokens", 0),
            "output_tokens": tracer_data.get("output_tokens", 0),
        }

    # ---- 6. model_start → llm_started ----
    if event_type == "model_start":
        return "llm_started", {
            "model": tracer_data.get("model", ""),
            "message_count": tracer_data.get("message_count"),
        }

    # ---- 7. model_error → llm_error ----
    if event_type == "model_error":
        return "llm_error", {
            "error": str(tracer_data.get("error", ""))[:200],
        }

    # ---- 8. route_decision → decision_event ----
    if event_type == "route_decision":
        return "decision_event", {
            "strategy": tracer_data.get("strategy"),
            "source": tracer_data.get("source"),  # rule / llm / forced
            "confidence": tracer_data.get("confidence"),
            "reason": tracer_data.get("reason", ""),
            "matched_signals": tracer_data.get("matched_signals"),
        }

    # ---- 9. plan_event → plan_event ----
    if event_type == "plan_event":
        return "plan_event", {
            "action": tracer_data.get("action"),
            "step_count": tracer_data.get("step_count"),
            "origin": tracer_data.get("origin"),
            "reason": tracer_data.get("reason"),
            "attempt": tracer_data.get("attempt"),
            "error": str(tracer_data.get("error", ""))[:200] if tracer_data.get("error") else None,
            "problems": tracer_data.get("problems"),
        }

    # ---- 10. verification_event → verification_updated ----
    if event_type == "verification_event":
        return "verification_updated", {
            "passed": tracer_data.get("passed", True),
            "issues": [
                tracer_data.get("error") or tracer_data.get("reason") or "verification issue",
            ] if tracer_data.get("error") or tracer_data.get("reason") else [],
            "missing_evidence": tracer_data.get("missing_evidence", []),
        }

    # ---- 11. Orchestrator 生命周期 ----
    if event_type == "orchestration_start":
        return "run_started", {"strategy": tracer_data.get("strategy")}
    if event_type == "orchestration_end":
        return "run_completed", {
            "status": tracer_data.get("status", "success"),
            "duration_ms": tracer_data.get("duration_ms"),
        }
    if event_type == "answer_final":
        return "answer_final", {"answer": tracer_data.get("answer", "")}
    if event_type == "run_completed":
        return "run_completed", {
            "status": tracer_data.get("status"),
            "duration_ms": tracer_data.get("duration_ms"),
        }

    # ---- 12. Memory 内部事件（前端不需要，跳过）----
    if event_type.startswith("memory_"):
        return None

    # ---- 13. span_* 埋点标签跳过 ----
    if event_type.startswith("span_"):
        return None

    # ---- 14. 未知 type 一律跳过（别污染前端）----
    return None


def remap_events_if_needed(events: list[dict]) -> list[dict]:
    """对 DB 里反序列化出来的 events 做运行时重映射。

    为什么需要这个函数？
      events_json 列可能存的是老版本格式（直接透传 Tracer raw type），
      也可能是新版本格式（已经映射过）。这个函数检查每个 event 的 type：
      - 如果是已知 RunEvent type → 原样保留（新版本）
      - 如果是 Tracer raw type → 调 map_tracer_to_run_event 重映射（兼容老数据）
    """
    # 前端 RunEvent 已知 type（和 events.py EventType Literal 对齐）
    KNOWN_RUN_TYPES = {
        "run_started", "repo_connected", "routing_selected", "decision_event",
        "indexing_started", "indexing_completed",
        "retrieval_completed",
        "tool_started", "tool_completed",
        "llm_started", "llm_error", "usage_updated",
        "plan_event", "verification_updated",
        "answer_delta", "answer_final",
        "run_completed", "run_failed", "run_cancelled",
        # "info" ← 故意不包含，历史里的 info 一律跳过
    }
    result = []
    for ev in events:
        ev_type = ev.get("type", "")
        if ev_type in KNOWN_RUN_TYPES:
            # 已经是 RunEvent 格式，原样保留
            result.append(ev)
        else:
            # 老格式 raw type → 重映射
            mapped = map_tracer_to_run_event(ev_type, ev.get("data", ev))
            if mapped is None:
                continue
            new_type, new_data = mapped
            result.append({"type": new_type, "data": new_data})
    return result
