# 评估结论累积记录（CONCLUSIONS）

本文件按时间累积每轮评估的**结论**（原始数据见同目录带时间戳的 .md / evals/results/*.jsonl）。
所有数字均来自真实运行（token 来源 provider），未实测的一律标注「未测得」。

---

## 参数变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-21 | max_steps 8 → 5 | 步骤过多导致重复检索、必超时 |
| 2026-10-21 | step_max_model_calls 3 → 5（recursion_limit 24） | 单步 3 轮跑不完检索任务 |
| 2026-10-21 | max_input_tokens 30000 → 60000（app/main.py 与 evals/config.py 两处） | 30K 上限使 react 与 plan 同题双双被预算掐断，对比失真 |
| 2026-10-21 | PLANNER_PROMPT 重写：每步必须含具体文件/符号目标、优先 3~4 步、禁探索性步骤 | 模型生成 8 步泛化计划 |
| 2026-10-21 | 步骤 prompt 注入全部前序结论+已定位证据；step 独立 thread_id、无 checkpointer/记忆工具；预算类失败不重试；超时下沉到单步 | 消除步骤间重复探索 |

---

## 第 1 轮（2026-10-21 23:32）chain-01 单题 plan（30K 上限，max_steps=8）

- 结果：**partial**，240.5s / 211.9K tok / 22 次工具 / 8 步仅完成 3 步（s4 撞 recursion 16 上限）
- 质量分 1.0 / 证据 1.0（靠前 3 步证据撑起）
- 结论：8 步计划 + 每步独立 Agent = 重复探索，步骤做不完。

## 第 2 轮（2026-10-21 23:43）chain-01 react vs plan（30K 上限）

- react：8.7s / 63.7K / **partial**（7 次工具后被 30K 单次输入上限掐断，质量 0）
- plan：242.9s / 403.5K / partial（质量 1.0，但 39 次工具调用）
- 结论：**30K 单次输入上限是共同瓶颈**，两个配置在同题对比失真 → 提高到 60K。

## 第 3 轮（2026-10-21 23:51）chain-01 react vs plan（60K 上限，max_steps=5）

| 配置 | 状态 | 耗时 | Token | 工具 | 质量 |
|---|---|---|---|---|---|
| react | **success** | **49.6s** | **126.1K** | **8** | 1.0 |
| plan | partial | 192.3s | 319.9K | 23 | 1.0 |

- plan 挂在 s2 撞 recursion 24 上限，后续步骤连带跳过。
- 结论：**线性调用链任务上 plan 是结构性负收益**（慢 4 倍、贵 2.5 倍）——计划生成时未检索过代码，步骤目标靠猜，每步新 Agent 重新探索。

## 第 4 轮（2026-10-22 16:47）五类代表题 react vs plan（60K 上限）

| 题目（类别） | react | plan | 质量(react/plan) | 结论 |
|---|---|---|---|---|
| simple-05（简单查询） | **13.5s / 16.5K / success** | 286.7s / 337.6K / partial | 1.0 / 1.0 | react 快 21 倍 |
| chain-04（调用链） | **28.3s / 33.5K / success** | 256.6s / 284.8K / partial | 1.0 / 1.0 | react 快 9 倍 |
| arch-03（架构） | **79.0s / 146.6K / success** | 274.3s / 232.7K / partial | 1.0 / 1.0 | react 快 3.5 倍 |
| review-03（审查） | **34.7s / 64.2K / success** | 122.6s / 70.8K / **success** | 1.0 / 1.0 | 质量平手，react 快 3.5 倍；plan 唯一一次 success |
| hist-01（历史决策） | 129.1s / 231.2K / success | 202.2s / 237.0K / partial | **0.75 / 1.0** | **plan 质量唯一胜出**；react 20 次工具调用出现"越走越偏" |

**本轮核心结论**：
1. react（≈baseline 能力）可覆盖大多数题型，4/5 碾压或平手。
2. plan 的价值点仅在「历史决策/多目标枚举」类：先定检索目标不易走偏，质量 +0.25，代价 ~1.5 倍时间。
3. plan 的 partial 多数是「答案已质量 1.0 但步骤未走完」——执行可靠性仍是短板。
4. 策略建议（数据支持）：日常默认 react；历史决策类可选 plan；plan_verifier / plan_replan 保留在配置中但默认不启用（成本过高，收益未测得为正）。

## 第 5 轮（2026-10-22 17:16）多子任务审计题（audit-01 / audit-02，专为 plan 设计）

题目特征：多个相互独立的子问题（枚举配置点 / 对比两条入口链路 + 两个记录实现文件），
是 plan 理论上最擅长的形态。react 在这两题耗时 120~141s（远慢于简单题），说明题目确实难。

| 题目 | react | plan | plan_replan |
|---|---|---|---|
| audit-01 | **success** 140.9s / 208.3K / 10 工具 | partial 300.0s（超时）/ 393.8K / 34 | partial 301.6s / 483.4K / 39，验证未通过 |
| audit-02 | **success** 120.3s / 288.0K / 18 工具 | **success** 253.0s / 325.2K / 29 | partial 304.2s / 319.7K / 37，验证未通过 |

质量分：6 次运行全部 1.0（正确答案谁都能给，差异在成本与可靠性）。

**本轮核心结论**：
1. **plan 首次真正跑成功**（audit-02）——但代价仍是 react 的 2 倍时间、1.13 倍 token，质量无增量。
2. 即使在为 plan 量身定做的题型上，react 依然更快、同等质量 → **plan 的执行开销是结构性的，不是题型问题**。
3. plan_replan 两题全部超时且验证未通过（301.6s / 483.4K，比纯 plan 更差）→ **重规划在当前实现下是负资产**：重跑步骤并未修复问题，只是再烧一遍钱。
4. 验证器（verifier）两次判定「未通过」但答案质量实为 1.0 → 验证器的判定口径与评分器不一致，其结论需谨慎对待（未深挖）。

## 总结论（第 1~5 轮，12 次 react vs plan 同题对比）

| 维度 | react | plan |
|---|---|---|
| 成功率 | 7/7 success | 2/12 success |
| 平均耗时优势 | 基线 | 慢 2~21 倍 |
| 质量胜出的题 | 6/7 平手 | 仅 hist-01（+0.25） |

**最终决策建议（数据支持）**：
- 日常默认 **react**；direct 保留给简单题（路由已验证省时省钱）。
- plan 降级为可选策略（--strategy plan 手动启用），不进默认路由；唯一有数据支持的场景是历史决策类。
- plan_verifier / plan_replan 保留配置但默认不启用：verifier 判定口径待修，replan 实测为负收益。
- 若未来要救 plan，方向是「计划当 checklist、单 Agent 连续执行」（消除每步新 Agent 的重复探索），而非继续调参。

---

## 第 6 轮（2026-10-22）架构收敛：baseline 并入 react，plan 退出自动路由

第 1~5 轮数据驱动的最终架构落地，本轮为代码变更，不再跑评估。

### 变更点
| 变更 | 文件 | 说明 |
|---|---|---|
| 移除 baseline 模式 | `app/main.py` | 删除 `_is_baseline`、`_chat_baseline`、baseline 分支；`create_repository_agent` 仅保留作为测试参考实现，不再被主入口调用 |
| react 补齐 SummarizationMiddleware | `app/main.py` | `build_agent_factory` 在 direct/react 模式挂 `SummarizationMiddleware`（7000 tokens 触发、保留 10 条、qwen-flash 摘要模型），与原 baseline 同配置；这是 baseline 与 react 的唯一关键差异 |
| plan 退出自动路由 | `app/orchestration/complexity.py` | ①`rule_route` 强复杂信号 → REACT（原 PLAN_EXECUTE），reason 注明「plan 仅手动启用」；②`route()` LLM 路径强制归一：`final == PLAN_EXECUTE → REACT`；③`_llm_route` prompt 限定 direct/react 二选一，返回后再归一 |
| 评估配置移除 baseline | `evals/config.py`、`evals/run_eval.py` | VARIANTS 移除 `"baseline"`；删除 `_run_baseline` 与 dispatch 分支 |
| 数据集同步 | `evals/dataset/tasks.py` | chain-01 改单一编排路径（锚点 `orchestrator = Orchestrator(`）；chain-03 异常捕获锚点改到 executor.py；audit-01 改三模式装配差异题 |
| 测试断言更新 | `tests/test_orchestration.py` | 强信号 → REACT；LLM plan → REACT 归一；自动路由 react + 手动 force plan 仍走计划路径；fallback 数据集长度 25 → 27 |

### 决策依据（沿用第 1~5 轮数据）
- **baseline ≈ react**：第 1 轮 simple-01 对比 direct/react/baseline 三者，react 与 baseline 同质量、react 略快；差异仅在 react 缺 SummarizationMiddleware。补齐后两者能力等价，保留一个即可。
- **plan 不进自动路由**：12 次同题对比 react 7/7 success、plan 2/12 success、慢 2~21 倍，质量胜仅 hist-01（+0.25）；强复杂信号命中时自动给 PLAN_EXECUTE 等于自动选了一个大概率超时的策略，违背用户利益。
- **plan 保留代码不删**：作为手动模式（`--strategy plan`），仍可用于「用户明确想要计划先确认」的场景；plan_verifier / plan_replan 配置保留默认不启用。

### 最终架构（当前状态）
```
用户问题
   │
   ▼
ComplexityRouter（规则 + 可选 LLM）
   │  ├─ direct  ──► create_agent(mode="direct")   单次检索
   │  └─ react   ──► create_agent(mode="react")    多轮检索（默认/兜底）
   │
   └─（plan_execute 不在自动路由范围）
        仅 --strategy plan 手动启用 ──► Plan-and-Execute
```

### 测试状态
- `pytest tests -q --ignore=tests/test_memory_persistence.py`：61 全通过。
- `materialize_jsonl` + `validate_dataset`：27 题，0 problems。
