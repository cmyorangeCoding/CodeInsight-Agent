# CodeInsight-Agent

> 面向代码仓库的智能分析 Agent：接入本地或 GitHub 仓库，用自然语言提问，自动检索代码、追踪调用链、给出带文件路径与行号的结论。

CodeInsight-Agent 是一个「代码仓库理解」方向的 LLM Agent 项目。它把**仓库访问、代码切片、混合检索、Agent 编排、记忆、可观测性、任务持久化**串成一条完整链路，并通过 Vue 3 前端以流式方式实时展示分析过程与代码证据。

---

## 界面预览

> 截图统一放在 `docs/images/`，按 `01、02、03…` 顺序编号。需要新增截图时继续按序号命名并追加即可。

### 主界面与仓库接入

|  |  |
|---|---|
| ![主界面](docs/images/01-home.png) | ![仓库配置](docs/images/02-repo-config.png) |

### 执行过程与代码证据

|  |  |
|---|---|
| ![执行进度](docs/images/03-progress.png) | ![代码证据](docs/images/04-evidence.png) |

### 最终回答

![最终回答](docs/images/05-answer.png)

---

## 项目介绍

传统代码搜索只能给出「文本命中」，IDE 助手又通常绑定在单个编辑器里。CodeInsight-Agent 想解决的是：

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

编排层提供三种执行策略，由「规则 + 轻量模型」路由器选择：

| 策略 | 适用场景 |
|---|---|
| `direct` | 定位型问题（某个类/函数在哪个文件） |
| `react` | 需要多轮工具调用的中等复杂问题 |
| `plan_execute` | 用户显式要求的深度分析、跨模块调用链 |

`plan_execute` 默认**不自动启用**，需要用户手动选择。这个取舍来自真实评测数据，详见 [ORCHESTRATION_DECISION.md](ORCHESTRATION_DECISION.md)。

### 5. 全链路可观测 + SSE 实时推送

- `Tracer` 记录工具调用、RAG 检索、LLM 用量、路由决策、计划步骤、验证结论。
- 通过 SSE 实时推送到前端，任务执行过程不是「等最终结果」，而是边跑边看。
- `TokenBudget` + 中间件对总 token、单次输入、输出预留做硬性约束，超限安全终止。

### 6. 短期 + 长期记忆

- **短期记忆**：LangGraph `PostgresSaver` checkpointer，按 `repo-{repository_id}` 固定 `thread_id`，同一仓库的多次提问自动续接上下文，不同仓库完全隔离。
- **长期记忆**：`PostgresStore` 向量记忆库，保存用户偏好与项目事实，通过中间件在每轮请求中按需注入系统提示词。
- 工具集提供 `search_project_memory` / `search_user_memory` / `save_long_term_memory`，由模型判断何时读写。

### 7. 历史任务持久化与完整回放

每次任务的问题、回答、状态、策略、耗时与完整事件链路都会写入 PostgreSQL。前端按仓库分组展示历史，并可直接恢复当时的执行进度与代码证据。

### 8. 自带评测体系

`evals/` 提供任务数据集、评分器、对比运行器与报告产出，可复现不同编排策略在真实仓库问题上的质量、耗时与 token 消耗。

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
    O --> DB[(runs 历史表)]
```

### 分层说明

| 层 | 目录 | 职责 |
|---|---|---|
| 前端展示层 | `web/` | Vue 3 + Vite + TypeScript，SSE 订阅、Markdown/代码高亮、历史回看 |
| API 层 | `app/api/` | FastAPI 路由、任务管理、SSE 事件流、历史 CRUD |
| 编排层 | `app/orchestration/` | 复杂度路由、计划生成、执行器、验证器、证据收集、运行记录 |
| Agent / 工具层 | `app/agents/`、`app/tools/` | LangChain Agent、仓库工具、记忆工具 |
| 检索层 | `app/rag/` | 切片、增量索引、向量/关键词混合检索 |
| 仓库访问层 | `app/repository/`、`app/mcp/` | 本地 Provider、GitHub Provider、GitHub MCP 客户端 |
| 记忆层 | `app/memory/` | 短期 checkpointer、长期向量记忆、偏好上下文注入 |
| 观测层 | `app/observability/` | Tracer、TokenBudget、预算中间件、回调 |
| 持久化层 | `app/db/` | runs 表 ORM 与 DAO |

### 一次请求的执行流程

1. 前端 `POST /api/runs` 创建任务，立即拿到 `run_id`。
2. 后端把任务提交到线程池，前端通过 `GET /api/runs/{run_id}/events` 建立 SSE 连接。
3. 后端构建仓库 Provider，执行增量索引（本地仓库或克隆成功的 GitHub 仓库）。
4. 复杂度路由器决定 `direct` / `react` / `plan_execute`。
5. Agent 通过 RAG 检索、grep、`read_file`、记忆工具收集证据。
6. 编排层汇总证据并生成最终回答；复杂策略额外经过 Verifier 校验。
7. 执行事件实时推送，任务结束后回答与事件快照写入 `runs` 表。

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

完整建表脚本见 [db/schema.sql](db/schema.sql)，共 10 张表，分四组：

| 分组 | 表 | 用途 |
|---|---|---|
| RAG 索引 | `code_chunks` | 代码切片 + 1024 维向量 + 全文检索列 `content_tsv` |
| 历史任务 | `runs` | 仓库路径、问题、最终回答、耗时、事件快照 `events_json` |
| 短期记忆 | `checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations` | LangGraph checkpointer，按 `repo-{repository_id}` 保存对话线程状态 |
| 长期记忆 | `store`、`store_vectors`、`store_migrations`、`vector_migrations` | PostgresStore 保存用户偏好 / 项目事实，并对文本做向量检索 |

- **短期记忆**保存「对话进行到哪一步」，负责同一仓库的多轮提问续接，不同仓库完全隔离。
- **长期记忆**保存「跨会话仍然有用的信息」，由 Agent 通过记忆工具按需读写。
- **历史任务**的 `events_json` 与实时 SSE 事件格式一致，历史回看时直接复用前端渲染逻辑，执行进度与代码证据可完整恢复。

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
| `GET` | `/api/runs/{run_id}/events` | SSE 事件流 |
| `POST` | `/api/runs/{run_id}/cancel` | 取消任务 |
| `GET` | `/api/history/repos` | 按仓库分组的历史列表 |
| `GET` | `/api/history/runs` | 指定仓库下的任务列表 |
| `GET` | `/api/history/runs/{run_id}` | 任务详情与事件回放 |
| `DELETE` | `/api/history/runs/{run_id}` | 删除历史任务 |

主要 SSE 事件：`run_started`、`repo_connected`、`indexing_progress`、`routing_selected`、`retrieval_completed`、`tool_started`、`tool_completed`、`usage_updated`、`plan_event`、`verification_updated`、`answer_final`、`run_completed` / `run_failed`。

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
│   └── schema.sql           # 完整数据库结构（10 张表，空库初始化用）
├── docs/
│   └── images/              # README 界面截图
├── web/                     # Vue 3 前端
│   └── src/
│       ├── App.vue
│       ├── api.ts
│       └── main.ts
├── tests/                   # 单元 / 集成测试
├── evals/                   # 评测数据集、评分器、运行器、报告
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
.venv\Scripts\python.exe evals\run_eval.py --validate-only
```

### 运行策略对比评测

```bash
# 只跑指定任务，对比 react 与 plan
.venv\Scripts\python.exe evals\run_eval.py --ids simple-01 --variants react,plan

# 按类别筛选
.venv\Scripts\python.exe evals\run_eval.py --category architecture --limit 3
```

评测结果输出到 `evals/results/`（原始 JSONL）与 `evals/reports/`（Markdown/JSON 报告），这两个目录默认不提交到 Git。

---

## 设计说明与已知限制

- **编排策略取舍**：`plan_execute` 在不稳定场景下开销显著高于 `react`，因此默认交给用户手动选择，而不是由路由器自动升级。完整数据与结论见 [ORCHESTRATION_DECISION.md](ORCHESTRATION_DECISION.md)。
- **模型依赖**：当前默认对接阿里云 DashScope 的 OpenAI 兼容接口，更换模型需同时确认对话与 Embedding 能力。
- **GitHub MCP**：按需读取模式需要本机具备 Node.js 与 `npx`；无 Token 时受公开仓库速率限制。
- **首次索引耗时**：大仓库首次全量索引需要调用 Embedding 接口，耗时与文件数量、网络状况相关；后续任务只做增量检查。
- **代码范围**：索引只覆盖文本代码文件，二进制、超大文件与非代码数据目录会被跳过。
- **安全**：项目不会执行被分析仓库中的任何代码；`.env` 与运行时产物均不纳入版本控制。

---

## 许可证

本项目暂未声明开源许可证。如需开源发布，请补充 `LICENSE` 文件。
