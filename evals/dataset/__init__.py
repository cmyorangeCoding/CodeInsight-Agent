"""评估数据集加载、校验与物化。

核心三件事：加载评测任务、校验任务是否合法、把 anchor 转换成真实行号并落盘保存。
整体流程：`load_tasks` 拿到任务 → `validate_dataset` 做全量校验 → `materialize_jsonl` 解析 anchor 生成带行号的 jsonl 文件。

"""
from __future__ import annotations

import json
from pathlib import Path

from evals.config import DATASET_PATH

DEFAULT_REPO = Path(__file__).resolve().parents[2]
#一条评测任务必须有的字段，缺任何一个直接校验失败。
REQUIRED_FIELDS = (
    "id", "category", "question", "difficulty", "orchestration_complexity",
    "expected_answer", "required_facts", "required_evidence", "optional_evidence",
    "forbidden_or_unsupported_claims", "scoring_rubric",
)

#编排复杂度取值：衡量「这题天然需要几次工具调用、跨几个文件」，与 difficulty（读懂难度）无关
ALLOWED_ORCHESTRATION_COMPLEXITY = {"low", "medium", "high"}

#评测集规范要求：一共 5 类，每类固定 5 条任务，合计 25 条
EXPECTED_CATEGORIES = {
    "simple_lookup": 5,
    "cross_module_chain": 5,
    "architecture": 5,
    "code_review": 5,
    "history_decision": 5,
}


def load_tasks(path: Path | None = None) -> list[dict]:
    """加载任务集。优先 JSONL；不存在时使用 Python 模块定义。"""
    target = Path(path) if path else DATASET_PATH
    if target.exists():
        tasks: list[dict] = []
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    tasks.append(json.loads(line))
        if tasks:
            return tasks
    from evals.dataset.tasks import TASKS

    return [dict(t) for t in TASKS]


#内部工具函数，读仓库里指定相对路径的源码文本。
def _read_text(repo: Path, rel: str) -> str | None:
    p = repo / rel
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def locate_anchor(repo: Path, rel_path: str, anchor: str) -> tuple[int, int] | None:
    """在文件中定位 anchor，返回 (line_start, line_end)；找不到返回 None。"""
    text = _read_text(repo, rel_path)
    if text is None:
        return None
    idx = text.find(anchor)
    if idx < 0:
        return None
    start = text[:idx].count("\n") + 1
    end = start + anchor.count("\n")
    return start, end


def validate_dataset(tasks: list[dict], repo: Path | None = None) -> list[str]:
    """校验数据集；返回问题列表（空 = 全部通过）。"""
    repo = repo or DEFAULT_REPO
    problems: list[str] = []

    ids = [t.get("id") for t in tasks]
    if len(set(ids)) != len(ids):
        problems.append("存在重复的任务 id")

    counts: dict[str, int] = {}
    for task in tasks:
        tid = task.get("id", "<no-id>")
        for field in REQUIRED_FIELDS:
            if field not in task:
                problems.append(f"{tid} 缺少必需字段 {field}")
        counts[task.get("category", "?")] = counts.get(task.get("category", "?"), 0) + 1

        cplx = task.get("orchestration_complexity")
        if cplx is not None and cplx not in ALLOWED_ORCHESTRATION_COMPLEXITY:
            problems.append(
                f"{tid} orchestration_complexity 取值非法：{cplx!r}"
                f"（应为 {sorted(ALLOWED_ORCHESTRATION_COMPLEXITY)}）"
            )

        if not task.get("required_facts"):
            problems.append(f"{tid} 没有 required_facts")
        if not task.get("expected_answer"):
            problems.append(f"{tid} 没有 expected_answer")

        for item in task.get("required_evidence", []) + task.get("optional_evidence", []):
            rel = item.get("file_path")
            anchor = item.get("anchor")
            if not rel:
                problems.append(f"{tid} 证据缺少 file_path")
                continue
            if not (repo / rel).is_file():
                problems.append(f"{tid} 证据文件不存在：{rel}")
                continue
            if anchor and locate_anchor(repo, rel, anchor) is None:
                problems.append(f"{tid} anchor 在 {rel} 中已失效：{anchor!r}")

    if len(tasks) < 20 or len(tasks) > 30:
        problems.append(f"任务数量 {len(tasks)} 不在要求的 20~30 区间")
    for category, expected in EXPECTED_CATEGORIES.items():
        actual = counts.get(category, 0)
        if actual != expected:
            problems.append(f"类别 {category} 数量为 {actual}，期望 {expected}")

    return problems


def materialize_jsonl(tasks: list[dict], repo: Path | None = None, path: Path | None = None) -> Path:
    """把 anchor 解析为真实行号，写入 JSONL 数据集。"""
    repo = repo or DEFAULT_REPO
    target = Path(path) if path else DATASET_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    enriched: list[dict] = []
    for task in tasks:
        record = dict(task)
        for key in ("required_evidence", "optional_evidence"):
            new_items = []
            for item in record.get(key, []):
                item = dict(item)
                if item.get("anchor"):
                    located = locate_anchor(repo, item["file_path"], item["anchor"])
                    if located:
                        item["line_start"], item["line_end"] = located
                new_items.append(item)
            record[key] = new_items
        enriched.append(record)

    with target.open("w", encoding="utf-8") as fh:
        for record in enriched:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target
