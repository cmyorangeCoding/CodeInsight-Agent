"""端到端测试：FastAPI + GitHub 仓库 + SSE 事件流（阶段五评测采集脚本）。

对同一个真实开源仓库（psf/requests）依次提交三类任务：
  1. 简单查询
  2. 跨文件调用链
  3. 架构分析

每个任务：POST /api/runs 创建 → 订阅 SSE 收集全部事件 → 汇总指标。
原始结果写入 evals/reports/github_web_e2e_raw.json，供评测报告引用。

运行：.\\.venv\\Scripts\\python.exe scripts\\e2e_github_test.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
REPO = "psf/requests"
OUT = Path("evals/reports/github_web_e2e_raw.json")

CASES = [
    {
        "category": "simple_query",
        "question": "requests 库中 Session 类定义在哪个文件？它的 request 方法的签名是什么样的？",
    },
    {
        "category": "cross_file_call_chain",
        "question": "从 requests.get() 被调用到最终真正发出 HTTP 请求，中间依次经过哪些函数和模块？请描述完整调用链，并给出每个关键环节所在的文件。",
    },
    {
        "category": "architecture_analysis",
        "question": "requests 库的整体架构是怎样的？sessions、adapters、models 这几个核心模块分别承担什么职责，它们之间如何协作？",
    },
]

TERMINAL = {"run_completed", "run_failed", "run_cancelled"}


def wait_healthy(timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE}/api/health", timeout=5)
            if r.ok:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("后端未就绪")


def collect_sse(run_id: str, wall_budget: float = 1500.0) -> tuple[list[dict], float]:
    """订阅 SSE 直到终态事件，返回 (事件列表, 客户端墙钟秒)。"""
    events: list[dict] = []
    start = time.time()
    with requests.get(
        f"{BASE}/api/runs/{run_id}/events",
        stream=True,
        # 注意：Tracer 事件是在 orchestrator.run() 返回后才批量桥接到 SSE 的，
        # 运行期间流上可能长时间无字节，read 超时必须大于整个 Agent 运行时长。
        timeout=(10, 1800),
    ) as resp:
        resp.raise_for_status()
        event_name = "message"
        data_lines: list[str] = []
        for raw in resp.iter_lines(decode_unicode=True):
            if time.time() - start > wall_budget:
                events.append({"type": "_client_timeout"})
                break
            if raw is None:
                continue
            line = raw.rstrip("\r")
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        data = {"raw": payload}
                    if isinstance(data, dict) and "type" not in data:
                        data["type"] = event_name
                    events.append(data)
                    print(f"  [evt] {event_name}", flush=True)
                    if event_name in TERMINAL or data.get("type") in TERMINAL:
                        break
                event_name, data_lines = "message", []
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
    return events, time.time() - start


def summarize(case: dict, run_id: str, events: list[dict], wall: float) -> dict:
    counts: dict[str, int] = {}
    tools: list[str] = []
    retrievals: list[dict] = []
    completed: dict = {}
    failed: dict = {}
    answer_len = 0
    verification: dict = {}
    indexing_notes: list[str] = []
    repo_info: dict = {}

    for e in events:
        t = e.get("type", "?")
        counts[t] = counts.get(t, 0) + 1
        d = e.get("data") or {}
        if t == "tool_started":
            tools.append(d.get("tool", "?"))
        elif t == "indexing_completed":
            indexing_notes.append(str(d.get("note", "")))
        elif t == "repo_connected":
            repo_info = d
        elif t == "retrieval_completed":
            retrievals.append({
                "candidate_count": d.get("candidate_count"),
                "selected_count": d.get("selected_count"),
                "elapsed_ms": d.get("elapsed_ms"),
                "files": [c.get("file_path") for c in (d.get("chunks") or [])],
            })
        elif t == "answer_final":
            answer_len = len(d.get("answer", "") or "")
        elif t == "verification_updated":
            verification = {"passed": d.get("passed"), "issues": d.get("issues")}
        elif t == "run_completed":
            completed = d
        elif t == "run_failed":
            failed = d

    return {
        "category": case["category"],
        "question": case["question"],
        "repo": REPO,
        "run_id": run_id,
        "status": completed.get("status") or ("failed" if failed else "unknown"),
        "strategy": None,  # 由 GET /api/runs/{id} 补充
        "wall_seconds": round(wall, 1),
        "duration_ms": completed.get("duration_ms"),
        "tool_call_count": completed.get("tool_call_count"),
        "input_tokens": completed.get("input_tokens"),
        "output_tokens": completed.get("output_tokens"),
        "failure_reason": completed.get("failure_reason") or failed.get("error"),
        "event_counts": counts,
        "tools_used": tools,
        "retrievals": retrievals,
        "verification": verification,
        "indexing_notes": indexing_notes,
        "repo_info": repo_info,
        "answer_chars": answer_len,
    }


def main() -> int:
    health = wait_healthy()
    print(f"[health] {health}", flush=True)

    results: list[dict] = []
    for i, case in enumerate(CASES, 1):
        print(f"\n=== [{i}/{len(CASES)}] {case['category']} ===", flush=True)
        print(f"Q: {case['question']}", flush=True)
        r = requests.post(f"{BASE}/api/runs", json={
            "question": case["question"],
            "repo_source": "github",
            "github_url": REPO,
            "strategy": "auto",
        }, timeout=30)
        r.raise_for_status()
        run_id = r.json()["run_id"]
        print(f"run_id: {run_id}", flush=True)

        events, wall = collect_sse(run_id)
        summary = summarize(case, run_id, events, events and wall or wall)
        try:
            st = requests.get(f"{BASE}/api/runs/{run_id}", timeout=10).json()
            summary["strategy"] = st.get("strategy")
            summary["final_status"] = st.get("status")
        except requests.RequestException:
            pass
        results.append(summary)

        print(f"status={summary['status']} strategy={summary['strategy']} "
              f"wall={summary['wall_seconds']}s duration={summary['duration_ms']}ms "
              f"tools={summary['tool_call_count']} "
              f"tokens(in/out)={summary['input_tokens']}/{summary['output_tokens']} "
              f"answer={summary['answer_chars']}chars", flush=True)
        print(f"events: {summary['event_counts']}", flush=True)
        print(f"tools: {summary['tools_used']}", flush=True)
        if summary["failure_reason"]:
            print(f"FAILURE: {summary['failure_reason']}", flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "backend_health": health,
        "repo": REPO,
        "runs": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始结果已写入 {OUT}", flush=True)

    ok = sum(1 for r in results if r["status"] in ("success", "partial"))
    print(f"成功 {ok}/{len(results)}", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
