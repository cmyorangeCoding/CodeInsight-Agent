"""评估实验配置：模型、策略预设、价格表、结果目录。

注意：价格表默认留空 —— 未知价格时成本一律标记为 unknown，绝不伪造。
如需启用成本计算，请按下述结构填入**真实**计价（单位：元 / 千 token）：
    PRICING = {"qwen-max": {"input_per_1k": 0.02, "output_per_1k": 0.06}}
"""
from __future__ import annotations

from pathlib import Path

# 真实价格配置（留空 = 未知，成本标记 unknown）
PRICING: dict[str, dict[str, float]] = {}

EVALS_DIR = Path(__file__).resolve().parent
DATASET_PATH = EVALS_DIR / "dataset" / "tasks.jsonl"
RESULTS_DIR = EVALS_DIR / "results"
REPORTS_DIR = EVALS_DIR / "reports"

# 对比的编排配置（variant 名称 → 参数）
# - direct / react：强制单一步骤策略（react 与原 baseline 能力一致：同工具/偏好注入/摘要压缩）
# - plan：规划+执行，不做验证、不重规划（手动模式，不进自动路由）
# - plan_verifier：规划+执行+验证
# - plan_replan：规划+执行+验证+有限重规划
# 注：原 baseline 变体已于 2026-10-22 移除——实测其与 react 能力等价（见 reports/CONCLUSIONS.md）
VARIANTS: dict[str, dict] = {
    "direct": {"kind": "orch", "force": "direct"},
    "react": {"kind": "orch", "force": "react"},
    "plan": {"kind": "orch", "force": "plan", "verify": False, "replans": 0},
    "plan_verifier": {"kind": "orch", "force": "plan", "verify": True, "replans": 0},
    "plan_replan": {"kind": "orch", "force": "plan", "verify": True, "replans": 2},
}

# 预算上限（各 variant 统一，保证对比公平）
MAX_TOTAL_TOKENS = 500_000
MAX_INPUT_TOKENS = 60_000
RESERVED_OUTPUT_TOKENS = 2_000
DEFAULT_TIMEOUT_SECONDS = 300.0
