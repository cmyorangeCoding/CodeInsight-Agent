# 路由策略评测报告（2026-11-08）

> 数据来源：`evals/results/20261108-171507.jsonl`（81 次真实运行）、`evals/results/plan_control/20261108-174801.jsonl`（Plan 对照 3 次）。
> 机器报告：`evals/reports/20261108-routing-final.md` / `.json`（已按复核后的策略标签重算）。
> 所有数字均来自真实模型调用（qwen3.8-flash + PostgreSQL/pgvector），未填模拟数据。

---

## 0. 结论摘要

### 0.1 为什么默认自动路由只在 Direct / ReAct 之间选择？

- 历史 12 次同题对照（2026-10-21/22）：react 7/7 success，plan 仅 2/12 success，慢 2~21 倍，只有 hist-01 一题质量 +0.25。
- 本次用当前代码重新跑了 3 题对照（simple-05 / chain-01 / audit-01）：

| 题目 | Auto（实际 ReAct） | Plan | Plan 相对 Auto 的 token / 耗时 |
|---|---|---|---|
| simple-05 | success，16,023 tok，12.7s，质量 1.0 | success，263,521 tok，202.0s，质量 1.0 | **16.4x / 16.0x** |
| chain-01 | success，131,457 tok，45.2s，质量 1.0 | partial，402,602 tok，301.3s，质量 1.0 | **3.1x / 6.7x** |
| audit-01 | success，26,058 tok，40.9s，质量 1.0 | failed（300s 超时），455,875 tok，质量 0 | **17.5x / 7.3x** |

结论：Plan 在当前实现下是结构性负收益（每步独立 Agent、步骤间重复探索），**不纳入默认路由是数据支持的决策**，本次结果与历史一致。

### 0.2 当前 Direct / ReAct 意图规则是否合理？

- **没有出现「复杂任务被错误降级为 Direct」**：27 题中欠路由（预期 react → 实际 direct）为 **0**；两处强复杂信号（chain-01、chain-05）都被规则以 confidence=1.0 保护为 ReAct，质量均为 1.0。
- **存在系统性 ReAct 偏置**：规则在 22/27 题上以 0.5 低置信度默认 ReAct；虽然会调用 LLM 复核，但「只升不降」使 LLM 无法改成 Direct。实测 **22 次 LLM 复核中最终策略改变 0 次**，其中 LLM 两次明确建议 Direct（simple-02、simple-04）均被拦截。
- **但偏置在本题集上大多是无害的**：复核标签后 21/27 与预期一致，6 道过度路由中只有 review-02 有明确成本损失（同质量 1.0，Auto 74.4k tok vs Direct 51.4k tok，+45%），其余 5 道质量相同、Token 差异在噪声范围内。
- **因此本轮不建议直接改阈值或删除「只升不降」**。理由见第 8、9 节：强制 Direct 在 27 题中有 7 题失败（6 次 GraphRecursionError），放宽降级的证据不足；更值得先修的是路由可解释字段和工具参数 bug。

---

## 1. 代码现状与本次改造

### 1.1 改造前已有能力

| 模块 | 现状 |
|---|---|
| `app/orchestration/complexity.py` | 规则优先 + 低置信度（<0.8）调用摘要模型 + 只升不降 + Plan 归一为 ReAct |
| `evals/dataset/tasks.py` | 27 题、6 类（5 个基础类各 5 题 + `multi_part_audit` 2 题）、事实/证据/禁止结论/评分权重 |
| `evals/run_eval.py` | 支持 direct / react / plan / plan_verifier / plan_replan 对比，Trace/Token/评分/异常样本落盘 |
| `app/orchestration/executor.py` | RunRecord 已记录 `strategy`、`route_source`、`route_reason` |
| 前端 | 已支持用户手动选择策略（本次未改动） |

### 1.2 改造前缺失能力

1. 评测 Runner **没有 auto 配置**，无法评测真实自动路由。
2. RunRecord 只有 `route_source/route_reason`，缺 `route_confidence`、规则命中信号，难以定位「规则 vs LLM」。
3. 报告只按配置聚合质量，**不分析路由决策**（规则策略、最终策略、误路由、配对比较、LLM 调用次数）。
4. 任务集没有「意图 / 预期策略 / 判断依据」字段，无法做路由维度对照。
5. 无 `expected_strategy` 的合法性校验。

### 1.3 本次修改文件清单（最小改造）

| 文件 | 改动 |
|---|---|
| `evals/config.py` | 新增 `auto` 配置（`force=""` → 复用真实 `ComplexityRouter`，未重写路由） |
| `evals/run_eval.py` | 采集 route 全字段 + `llm_call_count`；每次运行额外计算**规则基线**（直接调用 `ComplexityRouter.rule_route`）；新增 `analyze_routing` / `compare_strategy_outcomes` / `call_stats` 及报告章节 |
| `app/orchestration/models.py` | RunRecord 新增 `route_confidence`、`route_signals`（可选字段，向后兼容） |
| `app/orchestration/executor.py` | 填充上述两个字段 |
| `evals/dataset/tasks.py` | 27 题补 `intent` / `expected_strategy` / `expected_strategy_reason`；按实测复核 4 题标签（见 2.3） |
| `evals/dataset/tasks.jsonl` | 重新物化（保留真实行号锚点） |
| `evals/dataset/__init__.py` | 校验新增字段；`expected_strategy` 仅允许 direct/react |
| `tests/test_orchestration.py` | 新增 auto 配置、路由分析、误路由、数据集元数据 4 个测试 |

未改动：`app/orchestration/complexity.py`（本轮只分析）、前端、Plan 入口、Agent 架构。

---

## 2. 评测集检查

### 2.1 分类与覆盖

27 题 = 5 类基础题各 5 道 + 2 道多问题综合题。编排复杂度分布：low 13 / medium 8 / high 6。

| 目标场景 | 覆盖题目 | 覆盖判断 |
|---|---|---|
| 简单、明确代码查询 | simple-01~05 | 充分 |
| 指定文件/符号的代码分析 | simple-02/04、review-01/02/04/05 | 部分覆盖（题目形态偏向「定位/审查」，没有单独的「指定文件深读」类别） |
| 跨文件、跨模块调用关系 | chain-01~05 | 充分 |
| 复杂架构理解与依赖 | arch-01~05 | 充分 |
| 多轮动态获取证据 | chain-04/05、review-03、hist-01 | 充分 |
| 代码审查、多问题综合 | review-01~05、audit-01/02 | 充分（audit 仅 2 题，样本偏少） |

判断：**现有 27 题足以支撑本轮方向性结论，不需要机械扩充**。真正欠缺的是重复次数（每格仅 1 次运行），不是题目数量；「指定文件深读」可作为后续补充，但本次样本下增加 2 题也不足以改变统计结论，故未扩充。

### 2.2 新增的路由元数据

每题新增：

- `intent`：任务意图（定位查询 / 调用链追踪 / 架构理解 / 缺陷审查 / 设计决策溯源 / 多问题综合等）。
- `expected_strategy`：direct 或 react（**分析参考，不是绝对答案**）。
- `expected_strategy_reason`：判断依据，写明参考的文件数与步骤数。

### 2.3 基于实测的标签复核（重要，涉及后验调整）

首版按「静态文件数」标注的 4 道 direct 题被实测否定，已改为 react 并写明依据：

| 题目 | 首版 | 复核后 | 实测依据 |
|---|---|---|---|
| review-04 | direct | react | Direct 触发 GraphRecursionError；ReAct/Auto 质量 0.4 / 1.0 |
| review-05 | direct | react | Direct 事实覆盖 0.6；ReAct 1.0 |
| hist-02 | direct | react | Direct 0.5；ReAct 1.0 |
| hist-05 | direct | react | Direct 触发 GraphRecursionError；ReAct 0.5、Auto 1.0 |

效果：与预期一致从 17/27 提升到 **21/27**，过度路由从 10 降到 **6**。这是**看到结果后的标签修订**，属于本题集的已知局限，不能当作独立验证集；后续应以增加重复次数和新题来验证。

---

## 3. 整体表现（Auto / Direct / ReAct）

| 配置 | 运行 | 成功率 | 完成率 | 事实覆盖 | 事实正确率 | 证据覆盖 | 无依据断言率 | 综合质量分 |
|---|---|---|---|---|---|---|---|---|
| auto | 27 | 0.963 | 0.963 | 0.963 | 0.9904 | 0.9383 | 0.037 | **0.9346** |
| direct | 27 | 0.7407 | 0.7407 | 0.7407 | 0.9875 | 0.7037 | 0.037 | 0.7074 |
| react | 27 | 0.963 | 0.963 | 0.8889 | 0.990 | 0.8642 | 0.037 | 0.8605 |

只看成功运行（排除失败样本）：

| 配置 | 成功数 | 平均质量分 | 平均 Token | 平均耗时 | 平均 LLM 调用 | 平均工具调用 |
|---|---|---|---|---|---|---|
| auto | 26 | 0.9705 | 69,350 | 72.7s | 7.81* | 5.67 |
| direct | 20 | 0.9550 | 28,779 | 22.6s | 3.74* | 3.48 |
| react | 26 | 0.8936 | 70,238 | 67.2s | 7.96* | 6.11 |

\* LLM 调用次数来自 Agent 的 `model_start` 事件，不含复杂度路由与裁判的调用。

**关键解读**：

1. Direct 成功时质量并不差（0.955），但 **7/27 直接失败**（6 次递归上限、1 次网络），说明它无法覆盖复杂任务。
2. Auto 与 ReAct 的成功率、Token、工具调用几乎相同（-6% Token、-7% 工具调用，属单次运行噪声范围），**Auto 当前实质上是「ReAct + 个别 Direct 例外」**。
3. Auto 的质量优势（0.9346 vs 0.8605）主要来自 ReAct 在同一题上的运行波动（chain-03、chain-05、review-04、hist-05），不是路由选择本身。

---

## 4. 分类与复杂度表现

### 4.1 按类别（质量分）

| 类别 | Auto | Direct | ReAct |
|---|---|---|---|
| simple_lookup | **1.0** | 1.0 | 1.0 |
| cross_module_chain | **1.0** | 0.6 | 0.4667 |
| architecture | 0.9467 | **1.0** | **1.0** |
| code_review | **1.0** | 0.72 | 0.88 |
| history_decision | 0.7 | 0.5 | **0.9** |
| multi_part_audit | **1.0** | 0 | **1.0** |

- Direct 在 chain / audit 上崩溃，是策略能力边界，不是评分噪声。
- Auto 在 history_decision 上的 0.7 由 hist-01（网络失败）与 hist-02（同策略波动）拉低，不是误路由。
- architecture 的 0.9467 由 arch-02（Auto 跑了 15 次工具、19 次 LLM，证据覆盖 0.33）拉低；同题强制的 Direct/ReAct 均为 1.0，属于**同策略下的执行波动/发散**。

### 4.2 按编排复杂度（完成率 / 证据覆盖）

| 复杂度 | 题数 | Auto | Direct | ReAct |
|---|---|---|---|---|
| low | 13 | 1.0 / 1.0 | 0.8462 / 0.7692 | 1.0 / 0.9231 |
| medium | 8 | 0.875 / 0.7917 | 0.75 / 0.75 | 1.0 / 0.9167 |
| high | 6 | **1.0 / 1.0** | 0.5 / 0.5 | 0.8333 / 0.6667 |

Auto 在 high 复杂度上 6/6 完成，证明当前「复杂任务不降级」的保护方向正确；Direct 在 high 上只有一半完成。

---

## 5. Auto 的实际路由决策

### 5.1 决策分布

- 最终策略：**Direct 3（simple-01、simple-03、review-01）/ ReAct 24**。
- 路由来源：**rule 5 / llm 22**。
- 规则→最终：`direct->direct` 3、`react->react` 24；**没有发生任何升/降级**。
- 规则置信度 < 0.8 的题 22 道，最终策略与规则不同 **0 道**。
- LLM 的建议分布：`react` 20 次、`direct` 2 次；2 次 direct 建议均因为规则已是 ReAct 而被「只升不降」拦截。

**这说明当前 LLM 辅助层对最终策略的实际影响是 0/22**。它仍会产生一次额外模型调用，并把 LLM 的置信度写进 `route_confidence`（例如 simple-02 记录为 0.95），但实际执行的是规则给出的 ReAct —— **审计字段的语义有歧义**，详见 8.4。

### 5.2 六道过度路由（复核标签后）

| 题目 | 预期 | 实际 | 同题质量（Auto / Direct / ReAct） | Token（Auto / Direct） | 判断 |
|---|---|---|---|---|---|
| review-02 | direct | react | 1.0 / 1.0 / 1.0 | **74,398 / 51,441（+45%）** | **唯一明确的成本损失** |
| simple-02 | direct | react | 1.0 / 1.0 / 1.0 | 10,007 / 17,954 | Auto 反而更省，无损失 |
| simple-04 | direct | react | 1.0 / 1.0 / 1.0 | 9,084 / 9,128 | 持平 |
| simple-05 | direct | react | 1.0 / 1.0 / 1.0 | 16,023 / 16,585 | 持平 |
| hist-03 | direct | react | 1.0 / 1.0 / 1.0 | 22,200 / 23,644 | 持平 |
| hist-04 | direct | react | 1.0 / 1.0 / 1.0 | 20,044 / 21,390 | 持平 |

注意：Direct 在 simple-02 上用了 2 次工具、17.5k 输入 Token，比 Auto 的 1 次工具、9.4k 更高。**「Direct 更省」并不天然成立**，Token 差异更多取决于模型选择了几次工具，而不是策略标签。

### 5.3 Auto 与最佳强制策略的配对比较

- 可比 27 题：Auto 不劣于最佳强制策略（差 ≤0.05）**24 道**，明显更差 **3 道**；平均差 **-0.0123**（Auto 略高）。
- 3 道「更差」都不是路由选择造成的：
  - hist-01：Auto 首次 `OpenAIConnectionError`；重跑后命中工具参数 bug（见 6.2）。
  - hist-02：Auto 与强制 ReAct 同为 ReAct，质量 0.5 vs 1.0，属运行波动。
  - arch-02：Auto 与强制 ReAct 同为 ReAct，质量 0.7333 vs 1.0，Auto 在 15 次工具后发散、证据覆盖仅 0.33。
- 质量相近的 24 对样本：Auto 66,749 tok / 68.8s，最佳强制 54,147 tok / 53.1s。**Auto 没有体现出「相近质量下更省」的收益**；它基本等于 ReAct 的成本。

---

## 6. 典型误路由与异常案例

### 6.1 review-02：唯一值得修的成本误路由

- 问题形态：`CodeVectorStore.upsert_file_chunks` 的事务与异常回滚是否完备。
- 规则：未命中任何信号 → 0.5 默认 ReAct；LLM 也判断 react。
- 实测：Auto/ReAct/Direct 质量都 1.0；Auto 74.4k tok / 43.2s，Direct 51.4k tok / 19.8s。
- 原因：问题里给了明确符号路径（`CodeVectorStore.upsert_file_chunks`），但规则没有把「类.方法」形态识别为高置信 Direct 信号（现有 Direct 信号只有「在哪里定义/哪个函数」等中文定位词，且 `.py` 路径分支要求字面量 `.py`）。
- 建议：新增「`X.y` 形式符号 + 是否/是否完备/存在隐患」类问题的直接信号，并用回归集验证；仅 1 例支持，**先记为候选，不直接上线**。

### 6.2 工具参数 bug（与路由无关，但会污染路由评测）

- `chain-01`（react）与 `hist-01`（Auto 重跑）失败于：
  `TypeError: SearchCodeTool._run() got an unexpected keyword argument 'query'`
  （原始记录：`evals/results/rerun/20261108-174633.jsonl`）
- 工具真实参数名是 `keyword`（`app/tools/repository_tools.py`），模型偶尔生成 `query`。这是工具 schema/描述与模型调用习惯不匹配的健壮性问题，会表现为「某个策略偶发失败」，容易被误判为路由质量问题。
- 建议单独修复（例如在描述中强调参数名，或兼容 `query` 别名），~~本轮未改~~。
  **2026-11-08 已修复**：`SearchCodeTool._run` 现接受 `keyword` / `query` 双别名，
  两者都缺时返回可恢复提示而非抛 `TypeError`（见 `test_search_code_tool_accepts_query_alias`）。

### 6.3 其他异常

| 现象 | 影响 | 处理 |
|---|---|---|
| hist-01 Auto `OpenAIConnectionError`，2727 tok / 23.5s | 该题 Auto 质量 0，是基础设施失败 | 报告中标注，不计入路由判断 |
| Direct 6 次 GraphRecursionError（chain-01/02、review-04、hist-05、audit-01/02，recursion_limit=16） | Direct 的完成率仅 0.7407 | 作为「复杂任务不可 Direct」的结构性证据 |
| arch-02 Auto 发散（15 工具 / 19 LLM / 194.9s） | 同策略质量 0.7333 vs 1.0 | 提示 ReAct 自身的收敛性问题，非路由问题 |

---

## 7. Plan-and-Execute 对照（不纳入默认路由的依据）

本次 3 题 + 历史 12 次同题对比：

| 证据 | 结果 |
|---|---|
| simple-05 | Plan success 但 263.5k tok / 202s，Auto 16.0k / 12.7s（16 倍） |
| chain-01 | Plan partial 402.6k tok / 301s，Auto 131.5k / 45.2s（3.1 倍），且未完成 |
| audit-01 | Plan failed（300s 超时）455.9k tok，Auto/ReAct success 26.1k / 38.5k |
| 历史 12 次 | react 7/7 success；plan 2/12 success；慢 2~21 倍；仅 hist-01 质量 +0.25 |
| plan_replan | 2/2 超时，比纯 plan 更差 |

结论：Plan 的问题不是「题型不匹配」，而是「计划生成时没检索过代码 + 每步新 Agent 重复探索」。在直接回答任务上它是纯开销；即使历史决策类偶有质量收益，也不足以覆盖超时风险。**维持仅手动启用。**

---

## 8. ComplexityRouter 规则设计分析（`app/orchestration/complexity.py`）

### 8.1 强复杂信号保护：方向正确，样本内有效

- `调用链/整体架构/端到端/跨模块/审查/…` 命中即 ReAct、confidence=1.0，且不再询问 LLM。
- 本题集只有 chain-01、chain-05 命中强信号，两题 Auto 均 success、质量 1.0；没有出现「强信号被降级」的样本。
- 风险：信号里混入了纯文风词（`完整`、`全面`、`深入`、`详细分析`）。例如「这个函数的完整实现是什么」会被直接判 ReAct 且不询问 LLM。本题集未出现该误判，**属于待验证风险，不建议现在删**。

### 8.2 低置信度默认 ReAct + 只升不降 = LLM 层空转

- 22/27 题走 LLM 复核路径，但没有一次改变最终策略（0 升级、0 降级；2 次 Direct 建议被拦截）。
- 「只升不降」本身是合理的保护（防止复杂任务被模型误降），问题在于**默认档位已经是 ReAct**，导致保护退化为「永远 ReAct」。
- 因此当前规则的实际决策逻辑可以概括为：**命中 Direct 词典且 ≤80 字 → Direct；命中 `.py` 且 ≤120 字 → Direct；其余一律 ReAct**。LLM 只影响 reason/confidence 文案。

### 8.3 置信度阈值 0.8 缺少数据支撑

- 只有 0.8（Direct 词典）和 1.0（强信号）会跳过 LLM；0.5/0.6/0.7 都会询问。
- 本题集只有 3 道题以 0.8 直接走 Direct（simple-01、simple-03、review-01），均质量 1.0，但没有足够样本证明「0.8 是正确切分点」。0.7 的 `.py` 路径规则在本题集未触发。
- 结论：**阈值可保留，但需在扩大重复次数后再调**。

### 8.4 审计字段语义问题（已具备改造条件）

- `RouteDecision.confidence` 在 LLM 路径下记录的是 **LLM 的置信度**（如 0.95），但最终策略可能仍是规则给出的 ReAct。
- 这会让 Trace/报告读起来像「路由器 95% 确信选 ReAct」，而实际是「LLM 95% 确信选 Direct，被拦截」。
- 本次已在 RunRecord 增加 `route_confidence/route_signals` 便于分析，但**未改语义**（避免本轮评测口径变化）。建议下一轮把「规则建议 / 模型建议 / 采纳结果 / 采纳原因」拆成独立字段。

---

## 9. 是否需要调整规则：建议与前后对比

### 9.1 不建议现在做的事

| 候选改动 | 数据推演 | 结论 |
|---|---|---|
| 允许低置信度下 LLM 把 ReAct 降为 Direct | 只有 simple-02 / simple-04 会被降级；simple-02 Direct 反而多用 7,947 tok，simple-04 多用 44 tok；质量均为 1.0 | **无收益，拒绝** |
| 彻底删除「只升不降」 | Direct 在 7/27 题失败（6 次递归上限），复杂任务风险不可控 | **拒绝** |
| 降低全部 0.5 默认档为 Direct | Direct 完成率仅 0.74，且 chain/audit 全崩 | **拒绝** |
| 把 Plan 纳入复杂任务自动路由 | 3/3 对照全部 partial/failed 或 3~17 倍开销 | **拒绝** |

### 9.2 建议的最小改动（按优先级）

1. **P0｜语义修正（低风险）**：为路由决策增加 `rule_strategy / llm_strategy / adopted_strategy / adoption_note`，把「建议」与「采纳」分开；报告里不再把 LLM 置信度当作最终置信度。这能让后续误路由归因不再靠解析 reason 字符串。
   **2026-11-08 已落地**：`RouteDecision` 新增 `rule_*` / `llm_*` 审计字段与 `adoption`（forced / rule_only / rule_fallback / llm_agree / llm_upgrade / llm_downgrade_blocked），
   `RunRecord` 同步记录 `route_rule_strategy / route_llm_strategy / route_adoption`，评测报告路由表增加「LLM建议 / 采纳」列。
2. **P1｜消除空转（可选，需度量）**：既然规则=ReAct 时 LLM 无法降级，可在 `rule_decision.strategy == REACT` 时跳过 LLM 调用（保留规则 reason），或把 LLM 仅用于「是否升级/补充说明」。预期影响：22/27 请求少一次路由模型调用；策略结果不变。**本轮未改**，因为会改变审计输出，需单独验证延迟与可观测性。
3. **P1｜补 Direct 词典（候选，需回归）**：针对 review-02 这类「`类.方法` + 是否/隐患」问题增加 Direct 信号。前后对比（仅 1 例）：Direct 51.4k tok / 19.8s，ReAct 74.4k / 43.2s，质量同为 1.0。样本=1，先作为候选规则进回归集。
4. ~~**P1｜修工具参数兼容**：`search_code` 接受 `query` 别名或强化参数名描述~~ **已完成**（见 6.2）。
5. **P2｜标签维护**：已把 review-04/05、hist-02、hist-05 改标 react；后续每次评测后复核，但必须记录「后验修改」，不得用它宣称泛化能力。

### 9.3 标签修改前后的评测口径对比

| 指标 | 修改前（首版标签） | 修改后（复核标签） |
|---|---|---|
| 与预期策略一致 | 17/27（63%） | **21/27（78%）** |
| 过度路由 | 10 | **6** |
| 欠路由 | 0 | 0 |
| 最终策略分布 | direct 3 / react 24 | 不变 |
| 规则→最终变化 | 0 | 不变 |

说明：这只改变了「评测参照」，没有改变线上路由行为。

---

## 10. 后续演进：评测驱动的路由优化（设计建议，本次不实现）

```
真实问题持续积累
  → 动态评测集（新问题入集，保留来源与版本）
  → 多策略离线回放（Auto / Direct / ReAct，同题同版本同配置，repeat ≥2）
  → 指标分析（质量优先：事实/证据/无依据；再比较 token/时延/调用次数）
  → LLM 分析误路由案例，归纳问题类型，生成候选路由规则
  → 固定回归集验证（老题 + 新题；质量/证据不得下降）
  → 通过才更新规则；不通过保留旧规则
```

硬性约束：

1. 候选规则只能由 LLM 提出，**不能由 LLM 直接覆盖线上规则**；变更需人工确认与版本记录。
2. 每次变更必须同时报告「质量/证据」与「效率」，质量或证据下降则否决。
3. Plan 保持独立实验策略，不进入 Direct/ReAct 的自动优化闭环。
4. 回归集必须包含容易误降级的复杂题（chain/audit）与容易误升的简单题（simple），并按类别分别统计，避免总体均值掩盖某类退化。
5. 至少 repeat=2，并单独记录基础设施失败（网络、工具参数），避免把环境故障算进路由质量。

---

## 11. 复现命令与产物

```powershell
# 1) 数据集校验（27 题，证据锚点全部有效）
.\.venv\Scripts\python.exe -m evals.run_eval --validate-only

# 2) 主评测：Auto / Direct / ReAct 全量 27 题（本次实际执行）
.\.venv\Scripts\python.exe -m evals.run_eval --variants auto,direct,react --concurrency 3 --repeat 1

# 3) Plan 对照（3 道代表题，独立实验入口，不参与自动路由）
.\.venv\Scripts\python.exe -m evals.run_eval --ids simple-05,chain-01,audit-01 --variants plan --concurrency 3 --out-dir evals/results/plan_control

# 4) 单测
.\.venv\Scripts\python.exe -m pytest tests/test_orchestration.py -q
```

产物：

- 原始 81 次运行：`evals/results/20261108-171507.jsonl`
- 机器报告（标签复核后重算）：`evals/reports/20261108-routing-final.md` / `.json`
- Plan 对照原始数据：`evals/results/plan_control/20261108-174801.jsonl`
- hist-01 重跑原始数据（工具 bug 证据）：`evals/results/rerun/20261108-174633.jsonl`
- 本报告：`evals/reports/ROUTING_EVAL.md`

> 2026-11-08 清理：首版机器报告（标签复核前）、单题调试与 Plan 对照的机器报告已删除，
> 原始数据均保留在 `evals/results/`，需要时用 `evals/run_eval.py` 的渲染逻辑重新生成。

---

## 12. 局限与不能下的结论

1. **单次运行、样本 27 题**：同策略下的质量差异（如 hist-02 0.5 vs 1.0）不能归因于路由；本文只把「策略不同且质量差异稳定 + 机制可解释」的案例当作证据。
2. **评分是词面事实/证据覆盖**，不是语义判分；LLM 裁判本次未启用（0 次）。质量分相同不代表答案完全等价。
3. **基础设施失败混入**：hist-01 网络失败、2 次工具参数 TypeError、6 次 Direct 递归上限。已逐条注明，未从原始数据删除。
4. **4 个策略标签是看到结果后修订的**，因此 21/27 的「一致率」不能作为独立泛化验证；它只用于本轮校准口径。
5. **Token/耗时对比含噪声**：单次运行下，工具选择次数对 Token 的影响大于策略本身（simple-02 Direct 反而更高）。
6. **不能得出「Auto 比 ReAct 更优」**：Auto 在 24/24 同策略题上与 ReAct 的质量差异来自波动；Auto 的实际收益只有少数 Direct 例外，且未观察到显著效率优势。
7. **不能得出「规则阈值 0.8 最优」**：只有 3 题以 0.8 走 Direct，样本不足。
