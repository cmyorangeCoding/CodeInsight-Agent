# CodeInsight-Agent

> 面向代码仓库的智能分析 Agent：接入本地或 GitHub 仓库，用自然语言提问，自动检索代码、追踪调用链、给出带文件路径与行号的结论。

CodeInsight-Agent 是一个「代码仓库理解」方向的 LLM Agent 项目。它把**仓库访问、代码切片、混合检索、Agent 编排、记忆、可观测性、任务持久化**串成一条完整链路，并通过 Vue 3 前端以流式方式实时展示分析过程与代码证据。

---

## 界面预览

### 主界面与仓库接入

![主界面](docs/images/01-home.png)

![仓库配置](docs/images/02-repo-config.png)

### 执行过程与代码证据

![执行进度](docs/images/03-progress.png)

![代码证据](docs/images/04-evidence.png)

### 最终回答

![最终回答](docs/images/05-answer.png)

---

## 项目介绍

传统代码搜索只能给出「文本命中」， 大部分AI问答助手又通常绑定在单个编辑器里。CodeInsight-Agent 想解决的是：

- **跨文件问题**：一个函数从入口到真正执行经过了哪些模块？只靠 grep 很难回答。
- **自然语言提问**：不需要记住符号名，直接问「这个项目的鉴权流程是怎样的」。
- **过程可解释**：不仅给答案，还给出检索到的代码片段、文件路径、行号与工具调用轨迹。
- **可复用**：分析结果落库，历史任务可以随时回看，执行过程完整回放。

项目同时支持：

- **本地仓库**：直接读取本地目录，建立增量向量索引，适合深度分析。
- **GitHub 仓库**：优先 `git` 浅克隆后走完整 RAG；克隆失败自动降级为 GitHub MCP 按需读取。

---

## 核心亮点

### 1. 双源仓库接入 + 自动降级

统一 `RepositoryProvider` 协议屏蔽本地 / 远程差异。GitHub 模式先尝试浅克隆以支持完整索引；失败时自动切换为 MCP 按需读取（不中断任务，只是不走 RAG）。

### 2. 语义 + 关键词混合检索（RRF）

`app/rag/retriever.py` 同时执行：

- pgvector 余弦相似度召回（自然语言语义）
- PostgreSQL 全文检索 `content_tsv @@ plainto_tsquery`（精确标识符）

再用 **Reciprocal Rank Fusion（RRF）** 融合两路排名，避免向量分数与关键词分数量纲不一致的问题。

### 3. 代码感知切片 + 增量索引

- 按语言与 AST 结构切块，保留文件路径、符号名、起止行号。
- 每次任务都做 **文件 hash 增量检查**：只重索引变更文件，自动清理已删除文件。
- 自动过滤 `.cache`、`.claims`、`.data`、JSON 数据文件等非代码内容，避免超长单行文本触发 embedding 接口超限。

### 4. 多策略编排 + 复杂度路由

编排层提供四种执行策略，`auto` 为默认自动路由：

| 策略 | 适用场景 |
|---|---|
| `auto` | 默认；由「规则 + 轻量模型」在 `direct` / `react` 之间选择 |
| `direct` | 定位型问题（某个类/函数在哪个文件） |
| `react` | 需要多轮工具调用的中等复杂问题 |
| `plan_execute` | 用户显式选择的深度分析；不参与自动路由 |

路由流程：规则先给出建议（命中强复杂信号时置信度 1.0），低置信度再调用摘要模型复核；复核**只升不降**，避免复杂任务被模型误降为 `direct`。`plan_execute` 会被归一为 `react`，只能手动启用。

每次路由都会分别记录**规则建议 / LLM 建议 / 最终采纳策略 / 采纳原因**（`rule_strategy`、`llm_strategy`、`adoption` 等字段），便于事后归因，不会把不同来源的置信度混在一起。

`plan_execute` 默认**不自动启用**的取舍来自真实评测数据，详见 [ORCHESTRATION_DECISION.md](ORCHESTRATION_DECISION.md)；27 题 × Auto/Direct/ReAct 的路由专项评测见 [ROUTING_EVAL.md](evals/reports/ROUTING_EVAL.md)。

### 5. 全链路可观测 + SSE 实时推送

- `Tracer` 记录工具调用、RAG 检索、LLM 用量、路由决策、计划步骤、验证结论。
- 通过 SSE 实时推送到前端，任务执行过程不是「等最终结果」，而是边跑边看。
- `TokenBudget` + 中间件对总 token、单次输入、输出预留做硬性约束，超限安全终止。

### 6. 短期 + 长期记忆

- **短期记忆**：LangGraph `PostgresSaver` checkpointer。执行线程 `execution_thread_id = run-{run_id}` 每个任务独立、可精确恢复；会话上下文由 `conversation_thread_id`（默认 `repo-{repository_id}`）承载，只用于加载历史消息，不写入执行线程。同一仓库多轮提问仍然连续，不同任务之间互不干扰。
- **长期记忆**：`PostgresStore` 向量记忆库，保存用户偏好与项目事实，通过中间件在每轮请求中按需注入系统提示词。
- 工具集提供 `search_project_memory` / `search_user_memory` / `save_long_term_memory`，由模型判断何时读写。

### 7. 历史任务持久化与完整回放

每次任务的问题、回答、状态、策略、耗时与完整事件链路都会写入 PostgreSQL。前端按仓库分组展示历史，并可直接恢复当时的执行进度与代码证据。

### 8. 业务任务断点恢复

每个 Run 使用独立的 `run-{run_id}` 执行线程，执行状态由 LangGraph checkpoint 持续落库。服务崩溃重启后，`RecoveryScheduler` 会自动扫描租约过期的未完成任务，通过数据库原子认领取得执行权，并用原 `thread_id` 从最近 checkpoint 续跑。无法安全恢复的任务会被明确标记为 `failed` 并记录原因，不会静默重跑整个任务。详见[业务任务断点恢复](#业务任务断点恢复)。

### 9. 自带评测体系

`evals/` 提供任务数据集、评分器、对比运行器与报告产出，可复现不同编排策略在真实仓库问题上的质量、耗时与 token 消耗。

评测 Runner 支持 `auto` 配置，直接调用真实路由器，因此除了回答质量，还能评测**路由决策本身**：规则建议、LLM 建议、最终采纳策略、误路由（过度/欠路由）、规则与最终策略的差异，以及同题下 Auto 与强制策略的配对比较。

---

## 功能一览

| 功能 | 说明 |
|---|---|
| 自然语言代码问答 | 支持定位、解释、调用链、架构分析等问法 |
| 本地仓库接入 | 指定本地路径，自动增量索引 |
| GitHub 仓库接入 | 支持 `owner/repo` 或完整 URL，优先浅克隆，失败降级 MCP |
| 混合代码检索 | 向量召回 + 全文检索 + RRF 融合 |
| 精确文本检索 | 类似 grep 的标识符兜底检索 |
| 多策略编排 | `auto` / `direct` / `react` / `plan_execute` |
| 实时执行流 | SSE 推送索引进度、工具调用、检索证据、最终回答 |
| Token 预算控制 | 总预算、单次输入、输出预留三重约束 |
| 长期记忆 | 用户偏好、项目事实的保存与检索 |
| 历史任务 | 按仓库分组、详情回看、事件回放、删除 |
| 断点恢复 | 崩溃重启后自动认领未完成任务，从 LangGraph checkpoint 续跑 |
| 事件续传 | SSE 按 sequence / `Last-Event-ID` 补发历史事件，重启不丢进度 |
| GitHub Token 配置 | 前端可直接保存，无需重启后端 |
| 评测与报告 | 数据集校验、策略对比、结果报告 |

---

## 系统架构

```mermaid
flowchart LR
    U[Vue 3 前端] -->|POST /api/runs| API[FastAPI API 层]
    API --> TM[RunManager 线程池 + 事件队列]
    TM --> O[Orchestrator 编排层]
    O --> R{复杂度路由}
    R -->|direct| A1[单轮 Agent]
    R -->|react| A2[ReAct Agent]
    R -->|plan_execute| A3[Planner + 分步执行]
    A1 --> T[工具集]
    A2 --> T
    A3 --> T
    T --> P[RepositoryProvider<br/>本地 / GitHub MCP]
    T --> V[(PostgreSQL + pgvector<br/>代码索引 / 记忆)]
    A3 --> VF[Verifier 证据校验]
    VF --> O
    O -->|Tracer 事件| TM
    TM -->|SSE| U
    O --> DB[(runs / run_events<br/>tool_executions)]
    RS[RecoveryScheduler] -->|扫描过期租约| DB
    RS -->|原子认领 + resume| TM
    CP[(LangGraph checkpoints)] --> O
    O --> CP
```

### 分层说明

| 层 | 目录 | 职责 |
|---|---|---|
| 前端展示层 | `web/` | Vue 3 + Vite + TypeScript，SSE 订阅、Markdown/代码高亮、历史回看 |
| API 层 | `app/api/` | FastAPI 路由、任务管理、统一执行入口、恢复调度、SSE 事件流、历史 CRUD |
| 编排层 | `app/orchestration/` | 复杂度路由、计划生成、执行器、验证器、证据收集、运行记录 |
| Agent / 工具层 | `app/agents/`、`app/tools/` | LangChain Agent、仓库工具、记忆工具 |
| 检索层 | `app/rag/` | 切片、增量索引、向量/关键词混合检索 |
| 仓库访问层 | `app/repository/`、`app/mcp/` | 本地 Provider、GitHub Provider、GitHub MCP 客户端 |
| 记忆层 | `app/memory/` | 短期 checkpointer、长期向量记忆、偏好上下文注入 |
| 观测层 | `app/observability/` | Tracer、TokenBudget、预算中间件、回调 |
| 持久化层 | `app/db/` | runs / run_events / tool_executions 的 ORM 与 DAO |

### 一次请求的执行流程

1. 前端 `POST /api/runs` 创建任务，立即拿到 `run_id`。
2. 后端把任务提交到线程池，前端通过 `GET /api/runs/{run_id}/events` 建立 SSE 连接。
3. 后端构建仓库 Provider，执行增量索引（本地仓库或克隆成功的 GitHub 仓库）。
4. 复杂度路由器决定 `direct` / `react` / `plan_execute`。
5. Agent 通过 RAG 检索、grep、`read_file`、记忆工具收集证据。
6. 编排层汇总证据并生成最终回答；复杂策略额外经过 Verifier 校验。
7. 执行事件先写入 `run_events` 再实时推送；任务结束后回答与事件快照写入 `runs` 表。

---

## 业务任务断点恢复

### 两类线程 ID

| 概念 | 取值 | 作用 |
|---|---|---|
| `execution_thread_id` | `run-{run_id}` | 承载当前任务的 LangGraph checkpoint，任务之间完全隔离，是恢复执行的唯一依据 |
| `conversation_thread_id` | 前端传入的 `thread_id`，缺省 `repo-{repository_id}` | 只用于加载历史会话上下文（最近若干轮 Human/AI 消息），不写入执行线程 |

首次执行时把会话上下文作为 `initial_messages` 注入，再拼接用户问题；恢复执行不再注入问题、不开启新一轮对话，而是直接用 `invoke(None)` 从执行线程的 checkpoint 续跑。

### 恢复流程

```mermaid
flowchart TD
    S[服务启动] --> I[init_tables 增量迁移<br/>runs 新列 + run_events + tool_executions]
    I --> R[RecoveryScheduler 立即扫描一轮]
    R --> Q{status ∈ pending/running/recovering<br/>且租约为空或已过期?}
    Q -->|否| W[按 RECOVERY_INTERVAL 周期重扫]
    Q -->|是| C[数据库原子认领<br/>条件 UPDATE + rowcount]
    C -->|认领失败| W
    C -->|认领成功| T{存在 thread_id?}
    T -->|否| F[标记 failed：旧版本任务无法恢复]
    T -->|是| P{策略是否 direct/react?}
    P -->|否| F2[标记 failed：plan_execute 无 checkpoint 可恢复]
    P -->|是| K{checkpoints 表存在该线程记录?}
    K -->|是| RS[以 resume=True 提交线程池<br/>invoke(None) 从断点续跑]
    K -->|否| E{该 run 有执行事件?}
    E -->|无| FR[以 resume=False 首次执行]
    E -->|有| F3[标记 failed：无 checkpoint 且已有执行事件]
    RS --> X[execute_run 续租 + 事件先落库后推送]
    FR --> X
    X --> Z{completed / failed / cancelled}
```

关键约束：

- **不依赖内存变量伪装恢复**：恢复判定完全来自数据库（`status`、`lease_until`、`thread_id`、`checkpoints` 表、`run_events` 计数）。
- **不会直接重提所有 running 任务**：只有租约缺失或过期的任务才可能被认领；原 Worker 仍在续租时，其它进程无法抢占。
- **恢复幂等**：认领是条件 UPDATE，重复扫描不会重复提交；`run_manager.is_inflight` 额外挡住本进程内的重复提交。
- **终态不可恢复**：`completed` / `failed` / `cancelled` 不进入扫描，也不会被覆盖。
- **Plan 明确不支持 checkpoint 恢复**：`plan_execute` 的分步 Agent 未接 checkpointer，恢复会被标记 `failed` 并写入原因，而不是悄悄重跑整个任务。

### 状态流转

```text
pending ──claim──> running ──finish──> completed
   │                  │
   │                  ├──finish──> failed
   │                  └──finish──> cancelled
   └──claim──> recovering ──claim/resume──> running ──> ...
                    └──fail_unrecoverable──> failed
```

`recovering` 只表示「已被恢复调度器认领、正在决定续跑还是失败」，最终仍回到 `running` 或终态。所有终态写入都带 `worker_id` 与「非终态」条件：旧 Worker 在丢失租约后才完成任务时，`finish` 返回 False，不会覆盖新 Worker 的结果；取消与完成同时发生时也只有一方生效。

### 任务认领与租约

- **认领**：`UPDATE runs SET status=..., worker_id=..., lease_until=..., attempt=attempt+1 WHERE run_id=... AND (租约为空或已过期)`，以 `rowcount == 1` 判定成功。
- **续租**：`LeaseRenewer` 后台线程按 `RUN_LEASE_SECONDS / 3` 周期续租；续租失败即认为失去执行权，后续不再写终态。
- **接管**：Worker 崩溃后租约到期，其它进程（或重启后的自己）才能认领。
- 保护全部落在数据库条件更新上，SQLite / PostgreSQL 均可工作，不依赖 Python 进程内的锁。

### 工具执行的幂等保障

- **只读工具**（检索、grep、读文件等）不做包装，恢复时允许重新执行。
- **有副作用工具**（当前为 `save_long_term_memory`）由 `GuardedTool` 包装，以 `run_id + tool_name + 参数摘要(SHA-256)` 作为幂等键写入 `tool_executions`：
  - 命中 `completed` → 直接返回上次结果，不再执行；
  - 未命中 → 先写 `running`，成功后写 `completed`；
  - 执行异常 → 写 `failed` 并抛出，恢复时可重试。
- 这能消除「恢复后重复执行已成功工具」的大部分情况，但**不是 exactly-once**：副作用已经发生、`completed` 尚未写库时进程崩溃，恢复后仍可能重放一次。该窗口无法在应用层彻底消除，需要工具自身幂等或外部去重配合。

### SSE 事件持久化与续传

- 所有事件经 `RunEventPublisher.emit` **先写入 `run_events`（sequence 单调递增），再进入内存队列推送**；数据库不可用时降级为仅内存推送。
- `GET /api/runs/{run_id}/events` 支持三种游标：`?after=N`、`?last_sequence=N`、标准 `Last-Event-ID` 头。服务端先补发 `sequence > N` 的历史事件，再衔接实时队列，并按 sequence 去重。
- 每条 SSE 事件带 `id: <sequence>`；任务进入终态后会补发剩余事件、发送 `run_status` 终止事件再关闭连接。
- `events_json` 仍保留为任务完成后的完整快照，历史回放接口无需改动。

---

## 技术栈

| 分类 | 技术 |
|---|---|
| 后端 | Python 3.10、FastAPI、Uvicorn、SSE-Starlette |
| Agent | LangChain 1.x、LangGraph 1.x |
| 模型 | 阿里云 DashScope（ChatOpenAI 兼容接口）、`text-embedding-v3` |
| 数据库 | PostgreSQL + pgvector |
| ORM | SQLAlchemy 2.0 + psycopg3 |
| 远程仓库 | GitHub MCP Server（`npx @modelcontextprotocol/server-github`） |
| 前端 | Vue 3、Vite、TypeScript、marked、highlight.js |
| 测试 / 评测 | pytest、自研评测运行器与评分器 |

---

## 数据库结构

完整建表脚本见 [db/schema.sql](db/schema.sql)，共 12 张表，分六组：

| 分组 | 表 | 用途 |
|---|---|---|
| RAG 索引 | `code_chunks` | 代码切片 + 1024 维向量 + 全文检索列 `content_tsv` |
| 历史任务 | `runs` | 仓库路径、问题、回答、耗时、事件快照 `events_json`，以及 `thread_id` / `attempt` / `worker_id` / `lease_until` / `last_error` 等恢复字段 |
| 任务事件 | `run_events` | 逐条持久化的执行事件，`(run_id, sequence)` 唯一，支持崩溃补发与 SSE 续传 |
| 工具幂等 | `tool_executions` | 有副作用工具的幂等键、执行状态与结果 |
| 短期记忆 | `checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations` | LangGraph checkpointer，按 `run-{run_id}` 保存每个任务的执行状态 |
| 长期记忆 | `store`、`store_vectors`、`store_migrations`、`vector_migrations` | PostgresStore 保存用户偏好 / 项目事实，并对文本做向量检索 |

- **短期记忆**分为两层：`execution_thread_id` 保存「单个任务执行到哪一步」，用于崩溃恢复；`conversation_thread_id` 只作为会话上下文来源，不写入执行线程。
- **长期记忆**保存「跨会话仍然有用的信息」，由 Agent 通过记忆工具按需读写。
- **任务事件**逐条落库，是服务重启后事件补发的依据；**历史任务**的 `events_json` 与实时 SSE 事件格式一致，历史回看时直接复用前端渲染逻辑。
- 增量迁移见 [db/migrations/20261111_run_recovery.sql](db/migrations/20261111_run_recovery.sql)；同时 `init_tables()` 会在启动时用 `ADD COLUMN IF NOT EXISTS` 补齐字段，兼容已有历史数据。

---

## 快速开始

### 环境要求

| 依赖 | 版本 / 说明 |
|---|---|
| Python | 3.10+ |
| Node.js | 18+（前端构建与 GitHub MCP Server） |
| PostgreSQL | 14+，并安装 [pgvector](https://github.com/pgvector/pgvector) 扩展 |
| DashScope | 需要可用的 API Key（对话 + Embedding） |
| Git | GitHub 深度分析依赖 `git clone`（可选） |

### 1. 克隆项目

```bash
git clone https://github.com/<your-name>/CodeInsight-Agent.git
cd CodeInsight-Agent
```

### 2. 安装后端依赖

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

编辑 `.env`，至少填写：

```ini
DASHSCOPE_API_KEY=你的 DashScope API Key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen-plus
DATABASE_URL=postgresql://user:password@127.0.0.1:5432/codeinsight?sslmode=disable
```

> `GITHUB_TOKEN` 为可选项。不填只能访问公开仓库且速率限制较低；也可以启动后在页面里直接保存。

### 4. 初始化数据库

先创建数据库：

```bash
createdb codeinsight
# 或：psql -c "CREATE DATABASE codeinsight;"
```

然后执行仓库内的完整结构脚本 [db/schema.sql](db/schema.sql)：

```bash
psql "postgresql://user:password@127.0.0.1:5432/codeinsight" -f db/schema.sql
```

没有 `psql` 命令时，也可以把 `db/schema.sql` 的内容直接粘贴到 Navicat / DBeaver / pgAdmin 中执行。

> `code_chunks`（RAG 索引）必须初始化；`runs`（历史任务）与 LangGraph 的 checkpoint / store 表也会在运行时自动创建。脚本里保留了全部表结构，方便空库一次性建好或对照排查。

### 5. 启动后端

```bash
.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload
```

健康检查：<http://127.0.0.1:8000/api/health>

### 6. 启动前端

```bash
cd web
npm install
npm run dev
```

打开 <http://localhost:5173>。Vite 已配置 `/api` 代理到 `http://127.0.0.1:8000`，无需额外配置。

### 7. 开始使用

1. 选择仓库来源：本地路径或 GitHub 地址。
2. 输入问题，选择策略（默认 `auto`）。
3. 查看左侧执行进度与代码证据，右侧查看最终回答。
4. 任务结束后可在左侧历史列表中回看。

---

## 配置项

| 变量 | 必填 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 是 | DashScope API Key |
| `DASHSCOPE_BASE_URL` | 是 | OpenAI 兼容接口地址 |
| `DASHSCOPE_MODEL` | 是 | 主对话模型 |
| `SUMMARIZATION_MODEL` | 否 | 摘要压缩 / 复杂度路由模型，默认 `qwen-flash` |
| `EMBEDDING_MODEL` | 否 | 向量模型，默认 `text-embedding-v3` |
| `EMBEDDING_DIMENSIONS` | 否 | 向量维度，默认 `1024` |
| `DATABASE_URL` | 是 | PostgreSQL 连接串 |
| `GITHUB_TOKEN` | 否 | GitHub Token，提高速率限制并访问私有仓库 |
| `GITHUB_MCP_COMMAND` | 否 | MCP Server 启动命令，默认使用 `npx @modelcontextprotocol/server-github` |
| `WEB_HOST` / `WEB_PORT` | 否 | 后端监听地址，默认 `0.0.0.0:8000` |
| `TASK_TIMEOUT` | 否 | 单任务超时秒数，默认 `300` |
| `MAX_CONCURRENT_TASKS` | 否 | 同时执行的任务数，默认 `5` |
| `RUN_LEASE_SECONDS` | 否 | 任务执行租约秒数，默认 `90`；Worker 崩溃后需等租约过期才能被接管 |
| `RECOVERY_ENABLED` | 否 | 是否启用启动 / 周期恢复调度，默认 `true` |
| `RECOVERY_INTERVAL_SECONDS` | 否 | 恢复调度扫描间隔，默认 `30` |
| `RECOVERY_BATCH` | 否 | 单轮最多扫描任务数，默认 `10` |
| `CODEINSIGHT_CLONE_WORKSPACE` | 否 | GitHub 浅克隆工作区，默认 `~/.codeinsight/workspace` |
| `CLONE_CLEANUP_THRESHOLD_MB` | 否 | 工作区清理阈值，默认 `500` |

---

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/config` | 读取 GitHub Token 配置状态 |
| `POST` | `/api/config` | 保存 / 清除 GitHub Token |
| `POST` | `/api/runs` | 创建分析任务，返回 `run_id` |
| `GET` | `/api/runs/{run_id}` | 查询任务状态与最终回答 |
| `GET` | `/api/runs/{run_id}/events` | SSE 事件流（支持 `?after=` / `?last_sequence=` / `Last-Event-ID` 续传） |
| `POST` | `/api/runs/{run_id}/cancel` | 取消任务 |
| `GET` | `/api/history/repos` | 按仓库分组的历史列表 |
| `GET` | `/api/history/runs` | 指定仓库下的任务列表 |
| `GET` | `/api/history/runs/{run_id}` | 任务详情与事件回放 |
| `DELETE` | `/api/history/runs/{run_id}` | 删除历史任务 |

主要 SSE 事件：`run_started`、`repo_connected`、`indexing_progress`、`routing_selected`、`retrieval_completed`、`tool_started`、`tool_completed`、`usage_updated`、`plan_event`、`verification_updated`、`answer_final`、`run_completed` / `run_failed` / `run_cancelled`、`run_status`。

每条 SSE 事件带 `id: <sequence>`。客户端断线后可用最后收到的 sequence 重连（`?after=N` 或 `Last-Event-ID`），服务端先补发历史事件再衔接实时事件；任务已进入终态时会补发剩余事件、发送 `run_status` 后关闭连接。

---

## 项目结构

```text
CodeInsight-Agent/
├── app/
│   ├── api/                 # FastAPI 入口、任务管理、SSE、历史路由
│   ├── agents/              # Agent 装配与系统提示词
│   ├── tools/               # 仓库检索 / 读取 / 记忆工具
│   ├── orchestration/       # 路由、计划、执行、验证、证据、运行记录
│   ├── rag/                 # 切片、增量索引、混合检索、向量存储
│   ├── repository/          # 本地 / GitHub 仓库 Provider
│   ├── mcp/                 # GitHub MCP 客户端
│   ├── memory/              # 短期 checkpointer、长期记忆、偏好注入
│   ├── observability/       # Tracer、TokenBudget、回调与中间件
│   ├── context/             # 上下文构建
│   ├── db/                  # runs 表 ORM / DAO
│   ├── config.py            # 集中配置
│   ├── indexing.py          # 索引构建共享入口
│   └── event_mapper.py      # Tracer 事件 → SSE 事件映射
├── db/
│   ├── schema.sql           # 完整数据库结构（12 张表，空库初始化用）
│   └── migrations/          # 增量迁移脚本（启动时也会自动补齐字段）
├── docs/
│   └── images/              # README 界面截图
├── web/                     # Vue 3 前端
│   └── src/
│       ├── App.vue
│       ├── api.ts
│       └── main.ts
├── tests/                   # 单元 / 集成测试
├── evals/                   # 评测数据集、评分器、运行器、报告
│   ├── dataset/             # 27 题数据集（含意图 / 预期策略标注）
│   └── reports/             # 人工结论（结论文档入库，自动报告不入库）
├── scripts/                 # 手动验证脚本（GitHub / MCP / E2E）
├── requirements.txt
├── .env.example
├── ORCHESTRATION_DECISION.md
└── README.md
```

---

## 测试与评测

### 运行单元测试

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

### 校验评测数据集

```bash
.venv\Scripts\python.exe -m evals.run_eval --validate-only
```

### 运行策略对比评测

```bash
# 只跑指定任务，对比 react 与 plan
.venv\Scripts\python.exe -m evals.run_eval --ids simple-01 --variants react,plan

# 自动路由 vs 强制策略（Auto / Direct / ReAct 全量对比）
.venv\Scripts\python.exe -m evals.run_eval --variants auto,direct,react --concurrency 3

# 按类别筛选
.venv\Scripts\python.exe -m evals.run_eval --category architecture --limit 3
```

自动生成的评测结果输出到 `evals/results/`（原始 JSONL）与 `evals/reports/`（Markdown/JSON 报告），默认不提交到 Git；人工撰写的结论（`ROUTING_EVAL.md`、`CONCLUSIONS.md`）随源码提交。

每道评测题除答案与证据外，还标注了 `intent`（意图类别）、`expected_strategy` / `expected_strategy_reason`（预期策略及判断依据，仅作分析参考）。报告除质量与开销外，会单列：

- 每题的规则策略、LLM 建议、最终策略、采纳原因；
- 误路由明细（过度路由 / 欠路由）；
- Auto 与最佳强制策略的同题配对质量差与 Token/耗时对比。

当前结论见 [ROUTING_EVAL.md](evals/reports/ROUTING_EVAL.md)（27 题、81 次真实运行）。

---

## 设计说明与已知限制

- **编排策略取舍**：`plan_execute` 在不稳定场景下开销显著高于 `react`，因此默认交给用户手动选择，而不是由路由器自动升级。完整数据与结论见 [ORCHESTRATION_DECISION.md](ORCHESTRATION_DECISION.md)。
- **自动路由偏置**：当前规则在低置信度时默认 `react`，且 LLM 复核只升不降，因此简单任务可能被路由到 `react`。该偏置已在 27 题评测中量化（6 道过度路由、0 道欠路由，其中仅 1 道有明确成本损失），暂未调整规则，详见 [ROUTING_EVAL.md](evals/reports/ROUTING_EVAL.md)。
- **模型依赖**：当前默认对接阿里云 DashScope 的 OpenAI 兼容接口，更换模型需同时确认对话与 Embedding 能力。
- **GitHub MCP**：按需读取模式需要本机具备 Node.js 与 `npx`；无 Token 时受公开仓库速率限制。
- **首次索引耗时**：大仓库首次全量索引需要调用 Embedding 接口，耗时与文件数量、网络状况相关；后续任务只做增量检查。
- **代码范围**：索引只覆盖文本代码文件，二进制、超大文件与非代码数据目录会被跳过。
- **安全**：项目不会执行被分析仓库中的任何代码；`.env` 与运行时产物均不纳入版本控制。

### 断点恢复的可靠性边界

- **仅 `direct` / `react` 可 checkpoint 恢复**：`plan_execute` 的分步 Agent 未接 checkpointer，崩溃后会被标记 `failed`，不会重跑。需要恢复能力的深度分析请使用 `react`。
- **旧任务不可恢复**：升级前创建、没有 `thread_id` 的历史任务无法关联 checkpoint，恢复调度器会将其标记 `failed` 并写入原因。
- **无 checkpoint 时的重跑条件很严格**：只有「从未产生任何执行事件」的任务才允许以首次执行方式重跑；已有事件但无 checkpoint 的任务会直接失败，避免把重新执行伪装成恢复。
- **工具幂等不是 exactly-once**：「副作用已发生、幂等记录未写库」的窗口仍可能重放一次，需要工具自身幂等或外部去重配合；只读工具在恢复时允许重新执行。
- **Python 线程无法被强制终止**：取消是协作式的（`CancellationProbe` 周期检查内存标记、租约归属与数据库 `cancel_requested`），卡在不可中断调用的线程只能等待其自然返回；编排器内部超时检查也不等于后台线程硬终止。
- **租约接管存在延迟**：Worker 崩溃后，任务必须等 `RUN_LEASE_SECONDS` 过期才会被其它进程认领，启动恢复不是「立即」接管。
- **跨进程 SSE 依赖数据库轮询**：事件补发与终态判断通过 `run_events` / `runs` 查询完成，多进程部署时实时推送只发生在持有该任务内存队列的进程，其它进程通过重连补发获得完整事件。
- **恢复次数不设上限**：`attempt` 只做记录与观测，反复崩溃 + 反复恢复会持续累加，尚未实现「超过 N 次即放弃」的策略。

---


