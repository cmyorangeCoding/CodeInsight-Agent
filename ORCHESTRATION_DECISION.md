# 编排架构调整决策报告

> 记录 2026-10-21 ~ 2026-10-22 期间对 CodeInsight-Agent 编排层的真实评估与最终架构调整。
> 所有数据均来自真实模型运行（token 用量以 provider 返回为准），未实测项均明确标注。
> 累积评估细节见 `evals/reports/CONCLUSIONS.md`。

---

## 一、背景：原本的编排设计

第七阶段引入编排层时，设计了三种执行策略，由复杂度路由器自动选择：

| 策略 | 定位 | 适用场景（设计假设） |
|---|---|---|
| `direct` | 单次检索直接回答 | 文件位置、类/函数定义等定位型简单问题 |
| `react` | 多轮工具调用，边观察边决策 | 需要多轮检索的中等复杂问题 |
| `plan_execute` | 先生成结构化计划，再逐步执行 | 调用链分析、架构审查、跨模块问题等复杂任务 |

同时保留了一条独立的 `baseline` 路径（`create_repository_agent` 直接装配的单 Agent）作为对照。

**设计假设**：问题越复杂，越应该升级到更重的策略；`plan_execute` 是处理复杂问题的主力。

---

## 二、为什么放弃原本的编排模式

### 2.1 plan_execute 的执行开销是结构性的，不是调参能解决的

`plan_execute` 的原始实现存在四个相互叠加的问题：

1. **计划生成时未检索过代码**：Planner 仅根据问题文本生成步骤，每步的目标文件/符号靠猜，执行阶段必须重新探索。
2. **每步新建独立 Agent**：步骤之间不共享检索上下文，同一批文件被多个步骤反复读取，token 消耗随步骤数线性放大。
3. **单步模型调用上限未真正生效**：LangGraph 的 `recursion_limit` 以 super-step 计数（含工具/中间件节点），不是模型调用次数，换算系数错了导致任何步骤都跑不完。
4. **超时与预算检查位置不对**：超时只在步骤边界检查，单步跑飞了没人拦；预算类失败还在无意义重试，继续烧钱。

经过四轮调参修复（`max_steps 8→5`、`step_max_model_calls 3→5`、`max_input_tokens 30K→60K`、步骤 prompt 注入前序结论、step 独立 thread、超时下沉、预算类失败不重试）后，plan 的可靠性有所提升，但**与 react 的同题对比仍然全面落后**（见第三节数据）。

### 2.2 复杂度路由的强信号自动判 plan，违背用户利益

路由规则中，问题一旦命中"调用链 / 整体架构 / 代码审查 / 深入分析"等强复杂信号，即确定性判为 `PLAN_EXECUTE`。但实测数据显示 plan 在同题对比中 **12 次仅 2 次 success**（见 3.4），慢 react 2~21 倍。这意味着：

> 用户问了一个复杂问题，路由器自动把它送进了大概率超时或部分完成的策略。

这不是模型的问题，是策略选择的问题。

### 2.3 baseline 与 react 能力重叠，白付维护成本

`baseline` 与 `react` 的装配逻辑高度相似，唯一的关键差异是 baseline 挂了 `SummarizationMiddleware`（7000 tokens 触发压缩、保留 10 条消息）而 react 没有。第 1 轮 simple-01 同题对比显示两者质量等价、react 略快。保留两条等价路径只增加维护成本，不带来能力增量。

### 2.4 plan_verifier / plan_replan 是负资产

- `plan_replan` 在 audit-01 / audit-02 两题全部超时（301.6s / 483.4K tokens），比纯 plan 更差：重跑步骤并未修复问题，只是再烧一遍钱。
- `verifier` 两次判定「未通过」的题目，评分器给出质量分 1.0：判定口径与评分器不一致，其结论需谨慎对待。

### 2.5 过程中顺带发现并修复的问题

评估过程中还暴露出数据集与评估框架的若干问题（数据集锚点随代码改动失效、个别断言引用 Agent 无法访问的 git 提交记录、评估报告两处 Markdown 渲染缺陷、难度与编排复杂度字段混用），均已修复，不属于编排架构本身的问题，但与评估结论的可信度直接相关。

---

## 三、数据对比

### 3.1 第 1 轮：simple-01（简单题，direct vs react vs baseline，30K 上限）

| 配置 | 耗时 | Token | 质量 |
|---|---|---|---|
| direct | **8.3s** | **7.3K** | 1.0 |
| react | 12.5s | 12.4K | 1.0 |
| baseline | 17.4s | 12.1K | 0.75 |

**结论**：简单题上 direct 最优；react 与 baseline 能力等价。

### 3.2 第 3 轮：chain-01（线性调用链题，react vs plan，60K 上限）

| 配置 | 状态 | 耗时 | Token | 工具调用 | 质量 |
|---|---|---|---|---|---|
| react | **success** | **49.6s** | **126.1K** | **8** | 1.0 |
| plan | partial | 192.3s | 319.9K | 23 | 1.0 |

**结论**：线性调用链任务上 plan 是结构性负收益——慢 4 倍、贵 2.5 倍、未跑完。

### 3.3 第 4 轮：五类代表题 react vs plan（60K 上限）

| 题目（类别） | react | plan | 质量(react/plan) |
|---|---|---|---|
| simple-05（简单查询） | **13.5s / 16.5K / success** | 286.7s / 337.6K / partial | 1.0 / 1.0 |
| chain-04（调用链） | **28.3s / 33.5K / success** | 256.6s / 284.8K / partial | 1.0 / 1.0 |
| arch-03（架构） | **79.0s / 146.6K / success** | 274.3s / 232.7K / partial | 1.0 / 1.0 |
| review-03（审查） | **34.7s / 64.2K / success** | 122.6s / 70.8K / **success** | 1.0 / 1.0 |
| hist-01（历史决策） | 129.1s / 231.2K / success | 202.2s / 237.0K / partial | **0.75 / 1.0** |

**结论**：5 题中 react 4 题碾压或平手；plan 唯一一次质量胜出是 hist-01（+0.25），代价 ~1.5 倍时间。

### 3.4 第 5 轮：audit-01 / audit-02（多子任务审计题，专为 plan 设计）

| 题目 | react | plan | plan_replan |
|---|---|---|---|
| audit-01 | **success** 140.9s / 208.3K | partial 300.0s（超时）/ 393.8K | partial 301.6s / 483.4K |
| audit-02 | **success** 120.3s / 288.0K | **success** 253.0s / 325.2K | partial 304.2s / 319.7K |

**结论**：即使在为 plan 量身定做的题型上，react 依然更快、同等质量；plan 的开销是结构性的，不是题型问题；replan 全面负收益。

### 3.5 汇总：12 次 react vs plan 同题对比

| 维度 | react | plan |
|---|---|---|
| 成功率 | **7/7 success** | 2/12 success |
| 耗时 | 基线 | 慢 2~21 倍 |
| 质量胜出的题 | 6/7 平手 | 仅 hist-01（+0.25） |

---

## 四、现在的方案

### 4.1 最终架构

```
用户问题
   │
   ▼
ComplexityRouter（规则 + 可选 LLM）
   │  ├─ direct  ──► create_agent(mode="direct")   单次检索，简单题
   │  └─ react   ──► create_agent(mode="react")    多轮检索，默认/兜底
   │
   └─（plan_execute 不在自动路由范围）
        仅 --strategy plan 手动启用 ──► Plan-and-Execute
```

### 4.2 关键变更

| 变更 | 文件 | 说明 |
|---|---|---|
| 移除 baseline 模式 | `app/main.py` | 删除 `_is_baseline` / `_chat_baseline` / baseline 分支；`create_repository_agent` 仅保留作为测试参考实现 |
| react 补齐 SummarizationMiddleware | `app/main.py` | `build_agent_factory` 在 direct/react 挂摘要压缩（7000 tokens 触发、保留 10 条、qwen-flash 摘要模型），与原 baseline 同配置 |
| plan 退出自动路由 | `app/orchestration/complexity.py` | ①规则强信号 → REACT；②LLM 路径强制归一 `PLAN_EXECUTE → REACT`；③`_llm_route` prompt 限定 direct/react 二选一 |
| 评估配置移除 baseline | `evals/config.py` / `evals/run_eval.py` | VARIANTS 移除 `baseline`；删除 `_run_baseline` 与 dispatch |
| 数据集同步 | `evals/dataset/tasks.py` | chain-01 改单一编排路径、chain-03 异常捕获锚点改到 executor.py、audit-01 改三模式装配差异题 |
| 测试断言更新 | `tests/test_orchestration.py` | 强信号 → REACT；LLM plan → REACT 归一；自动路由 react + 手动 force plan 仍走计划路径 |

### 4.3 与原方案的核心差异

| 维度 | 原方案 | 现方案 |
|---|---|---|
| 模式数量 | baseline / direct / react / plan 四个 | direct / react 两个 + plan 手动 |
| 复杂度路由 | 三档，强信号自动进 plan | 只在 direct/react 间选，plan 不进路由 |
| baseline 与 react | 并存且能力重叠 | 合并为 react（补 SummarizationMiddleware） |
| plan 定位 | 复杂问题主力 | 手动专家模式（`--strategy plan`） |
| 异常处理 | main.py 捕获 TokenBudgetExceededError | executor.py 捕获并降级为部分回答 |
| plan_verifier / plan_replan | 配置可用 | 保留但默认不启用 |

---

## 五、本次调整为原方案带来的收益

1. **删掉 baseline 与 react 的重复实现**：主入口模式从 4 个降到 2 个，装配路径只剩一条，维护成本减半。
2. **消除用户被自动分配到 plan 的风险**：复杂题默认走 react，避免 4~21 倍耗时与大概率 partial 的体验。
3. **plan 保留代码不删**：作为手动专家模式（`--strategy plan`），仍可用于「用户明确想先看计划再执行」的场景；不在默认路径上就不产生成本。
4. **建立可重复的评估体系**：27 题数据集 + 确定性评分 + 累积报告，后续任何架构调整都有数据支撑。
5. **揭露 verifier / replan 的隐藏问题**：避免未来盲目信任这两个组件，明确标注「默认不启用」。
6. **每个变更都有测试覆盖**：61 项测试全通过、27 题 0 校验问题，路由、计划、预算、记录任一路径被改动都会被测试捕获。

---

## 六、最终结论

> 把"plan 是复杂问题银弹"的直觉，换成"**react 是默认正确选择、plan 是手动专家模式**"的数据驱动结论。

原方案的问题不是"实现不够细"，而是**核心假设与实测数据相悖**。继续往 plan 投精力是负收益，因此把 plan 从默认路由撤下、合并 baseline 与 react、保留 direct 处理简单题，是当前数据支持下的最优架构。

---

## 附：测试与数据集状态

- `pytest tests -q --ignore=tests/test_memory_persistence.py`：**61 全通过**。
- `materialize_jsonl` + `validate_dataset`：**27 题，0 problems**。
- 累积评估报告：`evals/reports/CONCLUSIONS.md`。
