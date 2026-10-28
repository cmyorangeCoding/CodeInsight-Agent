"""第一版评估任务集：25 个基于当前真实仓库的代码分析问题。

覆盖类别（每类 5 个）：
    simple_lookup          简单定位查询
    cross_module_chain     跨模块调用链
    architecture           架构分析
    code_review            代码审查
    history_decision       历史决策与项目知识

证据来源说明：
- 前 4 类的 required_evidence 指向真实文件 + 符号 + **稳定代码锚点**（anchor），
  构建数据集时由脚本在当前仓库中定位 anchor 并写入真实行号；运行时再次校验 anchor 仍有效。
- history_decision 类的事实来自**代码内注释**（仓库中没有 README/docs，且 Agent 工具集里
  没有 git 工具、扫描也会跳过 .git 目录，所以**提交历史不可访问**；该类可核验来源仅限代码内注释与
  代码事实，不臆测作者动机，只复述代码中写明的内容）。

字段含义：
    id / category / question / difficulty / orchestration_complexity / expected_answer
    difficulty                题目「读懂」的难度：easy / medium / hard（主观标注，用于人读）
    orchestration_complexity  题目「编排」复杂度：low / medium / high，
                              口径 = 题目天然需要的检索步数与是否存在「上一步结果决定下一步」的依赖链
                              （low: 1 步无依赖；medium: 2~3 步有依赖或需跨 2~3 文件对比；
                                high: 4 步以上、跨多模块且需拼装）——用于判断 Plan 策略是否有发挥空间
    required_facts            必须命中的事实（any_of 命中其一即算命中，确定性判定）
    required_evidence         必须提供的证据（file_path + symbol + anchor）
    optional_evidence         加分证据
    forbidden_or_unsupported_claims  出现即扣分的错误断言
    scoring_rubric            评分权重说明
"""
from __future__ import annotations

TASKS: list[dict] = [
    # ============================================================ simple_lookup
    {
        "id": "simple-01",
        "category": "simple_lookup",
        "difficulty": "easy",
        "orchestration_complexity": "low",
        "question": "get_llm() 函数定义在哪个文件？它返回什么对象，显式设置了哪个特殊参数（值是什么）？",
        "expected_answer": "定义在 app/models/llm.py；返回 ChatOpenAI 实例，读取 DASHSCOPE_MODEL / "
                           "DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL，并通过 extra_body 显式设置 enable_thinking=False"
                           "（关闭深度思考）。",
        "required_facts": [
            {"text": "get_llm 定义在 app/models/llm.py", "any_of": ["app/models/llm.py"]},
            {"text": "返回 ChatOpenAI 实例", "any_of": ["ChatOpenAI"]},
            {"text": "显式设置 enable_thinking=False", "any_of": ["enable_thinking"]},
        ],
        "required_evidence": [
            {"file_path": "app/models/llm.py", "symbol": "get_llm", "anchor": "def get_llm():",
             "note": "get_llm 定义处"},
            {"file_path": "app/models/llm.py", "symbol": "ChatOpenAI", "anchor": "return ChatOpenAI(",
             "note": "返回 ChatOpenAI 实例"},
        ],
        "optional_evidence": [
            {"file_path": "app/models/llm.py", "symbol": "DASHSCOPE_MODEL",
             "anchor": 'model=os.getenv("DASHSCOPE_MODEL")', "note": "模型名从环境变量读取"},
        ],
        "forbidden_or_unsupported_claims": [
            "get_llm 定义在 app/main.py",
            "get_llm 开启了 enable_thinking",
            "enable_thinking=True",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "三个必需要素：文件位置、返回类型、thinking 参数（当前为显式关闭）"},
    },
    {
        "id": "simple-02",
        "category": "simple_lookup",
        "difficulty": "easy",
        "orchestration_complexity": "low",
        "question": "提供给 Agent 的混合检索工具名是什么？它内部依赖哪个检索类、调用哪个方法？",
        "expected_answer": "工具名为 search_code_rag（类 SearchCodeRAGTool），定义在 app/tools/repository_tools.py；"
                           "依赖 app/rag/retriever.py 的 CodeRetriever，调用其 hybrid_search 方法。",
        "required_facts": [
            {"text": "工具名 search_code_rag", "any_of": ["search_code_rag"]},
            {"text": "依赖 CodeRetriever", "any_of": ["CodeRetriever"]},
            {"text": "调用 hybrid_search", "any_of": ["hybrid_search"]},
        ],
        "required_evidence": [
            {"file_path": "app/tools/repository_tools.py", "symbol": "SearchCodeRAGTool",
             "anchor": 'name: str = "search_code_rag"', "note": "工具名注册处"},
            {"file_path": "app/rag/retriever.py", "symbol": "CodeRetriever",
             "anchor": "def hybrid_search(self, query: str)", "note": "混合检索入口"},
        ],
        "optional_evidence": [
            {"file_path": "app/tools/repository_tools.py", "symbol": "SearchCodeRAGTool",
             "anchor": "results = self.retriever.hybrid_search(query)", "note": "工具内部调用点"},
        ],
        "forbidden_or_unsupported_claims": [
            "工具名是 search_code",
            "依赖 RepositoryAnalyzer.search_code",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "工具名 + 依赖类 + 方法名三要素"},
    },
    {
        "id": "simple-03",
        "category": "simple_lookup",
        "difficulty": "easy",
        "orchestration_complexity": "low",
        "question": "哪个函数负责把单个文件切成代码 chunk？它按语言分了哪些分支？",
        "expected_answer": "app/rag/chunker.py 的 chunk_file；python → _chunk_python，java → _chunk_java，"
                           "markdown → _chunk_markdown，其他语言统一走 _line_window_chunk。",
        "required_facts": [
            {"text": "chunk_file 在 app/rag/chunker.py", "any_of": ["chunk_file"]},
            {"text": "按语言分支", "any_of": ["python", "java", "markdown"]},
            {"text": "通用回退按行窗口切块", "any_of": ["_line_window_chunk", "行窗口"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/chunker.py", "symbol": "chunk_file",
             "anchor": "def chunk_file(content: str, file_path: str)", "note": "切块主入口"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/chunker.py", "symbol": "_line_window_chunk",
             "anchor": "def _line_window_chunk(", "note": "通用行窗口切块"},
        ],
        "forbidden_or_unsupported_claims": [
            "切块由 ingestion.py 的 chunk_file 完成",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "函数名 + 语言分支 + 回退策略"},
    },
    {
        "id": "simple-04",
        "category": "simple_lookup",
        "difficulty": "easy",
        "orchestration_complexity": "low",
        "question": "TokenBudget 在调用模型前做上下文检查的方法叫什么？它用什么库、什么编码估算 token？",
        "expected_answer": "方法名 check_context；estimate_tokens 使用 tiktoken 的 cl100k_base 编码估算，"
                           "计数异常时按字符数 // 2 粗略兜底。",
        "required_facts": [
            {"text": "方法名 check_context", "any_of": ["check_context"]},
            {"text": "使用 tiktoken", "any_of": ["tiktoken"]},
            {"text": "编码 cl100k_base", "any_of": ["cl100k_base"]},
        ],
        "required_evidence": [
            {"file_path": "app/observability/token_budget.py", "symbol": "TokenBudget",
             "anchor": "def check_context(", "note": "上下文预检查方法"},
            {"file_path": "app/observability/token_budget.py", "symbol": "estimate_tokens",
             "anchor": 'encoding = tiktoken.get_encoding("cl100k_base")', "note": "token 估算实现"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "使用模型自带 tokenizer 精确计数",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "方法名 + 库 + 编码名"},
    },
    {
        "id": "simple-05",
        "category": "simple_lookup",
        "difficulty": "medium",
        "orchestration_complexity": "low",
        "question": "MemoryRecord.to_value() 返回的字典里为什么必须包含一个顶层 text 字段？",
        "expected_answer": "因为在 app/memory/postgres_resources.py 中 PostgresStore 的索引配置 fields=[\"text\"]，"
                           "Store 需要对该字段做向量化以支持语义检索；缺了顶层 text 字段，记忆的语义检索无法工作。",
        "required_facts": [
            {"text": "Store 索引字段是 text", "any_of": ["text"]},
            {"text": "用于向量化/语义检索", "any_of": ["向量", "embed", "语义检索"]},
            {"text": "配置位置在 postgres_resources", "any_of": ["postgres_resources"]},
        ],
        "required_evidence": [
            {"file_path": "app/memory/models.py", "symbol": "MemoryRecord",
             "anchor": '"text": self.content,', "note": "显式提供顶层 text 字段"},
            {"file_path": "app/memory/postgres_resources.py", "symbol": "open_memory_resources",
             "anchor": '"fields": ["text"]', "note": "Store 索引字段配置"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "text 字段只是为了人类可读",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "必须同时指出 models 与 postgres_resources 两侧"},
    },

    # ======================================================= cross_module_chain
    {
        "id": "chain-01",
        "category": "cross_module_chain",
        "difficulty": "medium",
        "orchestration_complexity": "high",
        "question": "从 Web API 入口收到用户提问，到真正调用大模型，请给出完整调用链（涉及哪些文件与函数）。",
        "expected_answer": "app/api/main.py 的 _run_orchestrator_sync() 创建 TokenBudget/Tracer/retriever 后统一装配 "
                           "app/orchestration/executor.py 的 Orchestrator：经复杂度路由（auto 仅在 direct/react "
                           "间选择，plan 仅手动启用）后由 agent_factory(mode) 调用 create_agent 组装 Agent。"
                           "模型实例来自 get_llm()（app/models/llm.py）的 ChatOpenAI，最终 agent.invoke(...) "
                           "触发真正的模型调用。原命令行入口与 baseline 单 Agent 分支均已移除。",
        "required_facts": [
            {"text": "入口 app/api/main.py 的 _run_orchestrator_sync", "any_of": ["app/api/main.py", "_run_orchestrator_sync"]},
            {"text": "编排层 Orchestrator", "any_of": ["Orchestrator", "编排"]},
            {"text": "create_agent 组装", "any_of": ["create_agent"]},
            {"text": "get_llm → ChatOpenAI", "any_of": ["get_llm"]},
        ],
        "required_evidence": [
            {"file_path": "app/api/main.py", "symbol": "_run_orchestrator_sync",
             "anchor": "orchestrator = Orchestrator(",
             "note": "编排层装配点"},
            {"file_path": "app/orchestration/executor.py", "symbol": "Orchestrator",
             "anchor": "class Orchestrator", "note": "编排控制器"},
            {"file_path": "app/models/llm.py", "symbol": "get_llm", "anchor": "def get_llm():",
             "note": "模型实例来源"},
        ],
        "optional_evidence": [
            {"file_path": "app/agents/repository_agent.py", "symbol": "create_repository_agent_factory",
             "anchor": "def create_repository_agent_factory(", "note": "按模式组装 Agent 的工厂"},
        ],
        "forbidden_or_unsupported_claims": [
            "main.py 直接实例化 ChatOpenAI",
            "main.py 中仍存在 baseline 分支直接调用 create_repository_agent",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "链路必须覆盖 main → Orchestrator（→ create_agent）→ llm.py；单一编排路径"},
    },
    {
        "id": "chain-02",
        "category": "cross_module_chain",
        "difficulty": "hard",
        "orchestration_complexity": "medium",
        "question": "用户提问后，RAG 检索结果是如何产生并返回给模型的？请追溯从工具到检索器再到数据库查询的链路。",
        "expected_answer": "SearchCodeRAGTool._run 调用 CodeRetriever.hybrid_search；hybrid_search 分别执行 "
                           "vector_search（CodeChunk.embedding.cosine_distance 向量近邻）与 keyword_search"
                           "（content_tsv @@ plainto_tsquery + ts_rank_cd 全文检索），再用 reciprocal_rank_fusion "
                           "做 RRF 融合，取 Top-N 片段拼成文本返回给模型。",
        "required_facts": [
            {"text": "工具调用 CodeRetriever.hybrid_search", "any_of": ["hybrid_search"]},
            {"text": "向量检索 vector_search", "any_of": ["vector_search"]},
            {"text": "关键词检索 keyword_search", "any_of": ["keyword_search"]},
            {"text": "RRF 融合 reciprocal_rank_fusion", "any_of": ["reciprocal_rank_fusion", "RRF"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/retriever.py", "symbol": "CodeRetriever",
             "anchor": "def hybrid_search(self, query: str)", "note": "混合检索入口"},
            {"file_path": "app/rag/retriever.py", "symbol": "reciprocal_rank_fusion",
             "anchor": "def reciprocal_rank_fusion(", "note": "RRF 融合"},
            {"file_path": "app/rag/retriever.py", "symbol": "CodeRetriever.vector_search",
             "anchor": "def vector_search(self, query: str)", "note": "向量召回"},
            {"file_path": "app/rag/retriever.py", "symbol": "CodeRetriever.keyword_search",
             "anchor": "def keyword_search(self, query: str)", "note": "关键词召回"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/retriever.py", "symbol": "CodeRetriever.vector_search",
             "anchor": "CodeChunk.embedding.cosine_distance(query_vec)", "note": "pgvector 余弦距离查询"},
        ],
        "forbidden_or_unsupported_claims": [
            "只用向量检索，没有关键词召回",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "必须同时覆盖向量与关键词两路召回及融合"},
    },
    {
        "id": "chain-03",
        "category": "cross_module_chain",
        "difficulty": "medium",
        "orchestration_complexity": "medium",
        "question": "当模型调用超出 Token 预算时，是在哪一层被拦截的？请描述拦截链路与抛出的异常类型。",
        "expected_answer": "在 app/observability/budget_middleware.py 的 TokenBudgetMiddleware.wrap_model_call 中拦截："
                           "先读 budget.summary() 判断是否已超预算，再序列化消息与工具描述调用 budget.check_context；"
                           "超限时抛出 TokenBudgetExceededError（继承 RuntimeError）。该异常由编排层 "
                           "app/orchestration/executor.py 的 Orchestrator 捕获并降级为「执行被 Token 预算中断」的部分回答，"
                           "由 main.py 的 print_result 打印预算报告。",
        "required_facts": [
            {"text": "拦截点是 TokenBudgetMiddleware.wrap_model_call", "any_of": ["TokenBudgetMiddleware", "wrap_model_call"]},
            {"text": "调用 check_context", "any_of": ["check_context"]},
            {"text": "异常 TokenBudgetExceededError", "any_of": ["TokenBudgetExceededError"]},
        ],
        "required_evidence": [
            {"file_path": "app/observability/budget_middleware.py", "symbol": "TokenBudgetMiddleware",
             "anchor": "def wrap_model_call(self, request, handler)", "note": "预算拦截钩子"},
            {"file_path": "app/observability/budget_middleware.py", "symbol": "TokenBudgetExceededError",
             "anchor": "raise TokenBudgetExceededError(", "note": "抛出的异常"},
            {"file_path": "app/orchestration/executor.py", "symbol": "Orchestrator",
             "anchor": "except TokenBudgetExceededError as exc:", "note": "编排层捕获并降级为部分回答"},
        ],
        "optional_evidence": [
            {"file_path": "app/observability/budget_middleware.py", "symbol": "TokenBudgetExceededError",
             "anchor": "class TokenBudgetExceededError(RuntimeError):", "note": "异常定义"},
        ],
        "forbidden_or_unsupported_claims": [
            "在 TokenBudget.check_context 内部直接捕获并终止",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "中间件 + 检查方法 + 异常类型"},
    },
    {
        "id": "chain-04",
        "category": "cross_module_chain",
        "difficulty": "hard",
        "orchestration_complexity": "high",
        "question": "长期记忆从保存工具调用到最终写入数据库，经过哪些步骤与校验？",
        "expected_answer": "SaveLongTermMemoryTool._run → LongTermMemory.save：先做 memory_policy 的 "
                           "validate_scope_category / validate_source / validate_key / validate_content（含敏感信息过滤），"
                           "再 store.get 精确读取同键记录，用 decide_update 做冲突判定（duplicate/update/rejected），"
                           "最后构造 MemoryRecord 并 store.put(namespace, key, value) 写入。",
        "required_facts": [
            {"text": "工具 SaveLongTermMemoryTool", "any_of": ["SaveLongTermMemoryTool"]},
            {"text": "策略校验 validate_*", "any_of": ["validate_source", "validate_content", "validate_scope_category"]},
            {"text": "冲突判定 decide_update", "any_of": ["decide_update"]},
            {"text": "写入 store.put", "any_of": ["store.put", "put("]},
        ],
        "required_evidence": [
            {"file_path": "app/tools/memory_tools.py", "symbol": "SaveLongTermMemoryTool",
             "anchor": "class SaveLongTermMemoryTool(BaseTool):", "note": "保存工具"},
            {"file_path": "app/memory/long_term_memory.py", "symbol": "LongTermMemory",
             "anchor": "def save(", "note": "保存主流程"},
            {"file_path": "app/memory/memory_policy.py", "symbol": "validate_source",
             "anchor": "def validate_source(category: str, source: str)", "note": "来源白名单校验"},
            {"file_path": "app/memory/long_term_memory.py", "symbol": "LongTermMemory",
             "anchor": "self.store.put(namespace, memory_key, record.to_value())", "note": "最终写入"},
        ],
        "optional_evidence": [
            {"file_path": "app/memory/memory_policy.py", "symbol": "decide_update",
             "anchor": "def decide_update(", "note": "冲突处理"},
        ],
        "forbidden_or_unsupported_claims": [
            "未确认的模型建议可以直接保存为项目决策",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "工具 → 策略校验 → 冲突判定 → 写入四段"},
    },
    {
        "id": "chain-05",
        "category": "cross_module_chain",
        "difficulty": "hard",
        "orchestration_complexity": "high",
        "question": "仓库索引（切片 + 向量化 + 入库）的完整流程是什么？由哪个入口触发？",
        "expected_answer": "入口是 app/indexing.py 的 ensure_index（force=True 时强制增量重建）。流程：RepositoryIndexer.index_repository "
                           "→ repository.index_files 扫描文件 → read_file 读取 → _sha256 与已有 hash 比对实现增量 → "
                           "chunk_file 切块 → embeddings.embed_documents 分批（每批 10 条）向量化 → "
                           "CodeVectorStore.upsert_file_chunks 事务内先删旧后写新，落到 code_chunks 表。",
        "required_facts": [
            {"text": "入口 ensure_index", "any_of": ["ensure_index"]},
            {"text": "index_repository 主流程", "any_of": ["index_repository"]},
            {"text": "chunk_file 切块", "any_of": ["chunk_file"]},
            {"text": "upsert_file_chunks 入库", "any_of": ["upsert_file_chunks"]},
        ],
        "required_evidence": [
            {"file_path": "app/indexing.py", "symbol": "ensure_index", "anchor": "def ensure_index(",
             "note": "索引触发入口"},
            {"file_path": "app/rag/ingestion.py", "symbol": "RepositoryIndexer",
             "anchor": "def index_repository(", "note": "索引主流程"},
            {"file_path": "app/rag/code_vector_store.py", "symbol": "CodeVectorStore",
             "anchor": "def upsert_file_chunks(", "note": "入库封装"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/ingestion.py", "symbol": "RepositoryIndexer",
             "anchor": "chunks = chunk_file(content, file_path)", "note": "切块调用点"},
            {"file_path": "app/rag/ingestion.py", "symbol": "RepositoryIndexer",
             "anchor": "vectors.extend(self.embeddings.embed_documents(batch))", "note": "分批向量化调用点"},
        ],
        "forbidden_or_unsupported_claims": [
            "索引是全量重建，没有增量判断",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "入口 + 主流程 + 切块 + 入库"},
    },

    # ============================================================ architecture
    {
        "id": "arch-01",
        "category": "architecture",
        "difficulty": "hard",
        "orchestration_complexity": "medium",
        "question": "本项目的 Repository Agent 是如何创建与注册工具的？中间件又是如何装配的？",
        "expected_answer": "create_repository_agent 先调用 build_repository_tools 组装工具（get_repo_tree / read_file / "
                           "SearchCodeTool，可选 SearchCodeRAGTool 与三类记忆工具），再用 create_agent(model=get_llm(), "
                           "tools=tools, system_prompt=REPOSITORY_SYSTEM_PROMPT, middleware=[UserPreferenceMiddleware, "
                           "SummarizationMiddleware, TokenBudgetMiddleware], checkpointer, store) 组装为 LangGraph Agent；"
                           "偏好中间件放在最前，预算中间件放在最后。",
        "required_facts": [
            {"text": "工具来自 build_repository_tools", "any_of": ["build_repository_tools"]},
            {"text": "create_agent 装配", "any_of": ["create_agent"]},
            {"text": "中间件包含 SummarizationMiddleware", "any_of": ["SummarizationMiddleware"]},
            {"text": "偏好中间件 UserPreferenceMiddleware", "any_of": ["UserPreferenceMiddleware"]},
        ],
        "required_evidence": [
            {"file_path": "app/agents/repository_agent.py", "symbol": "build_repository_tools",
             "anchor": "def build_repository_tools(", "note": "工具构建函数"},
            {"file_path": "app/agents/repository_agent.py", "symbol": "create_repository_agent",
             "anchor": "agent = create_agent(", "note": "Agent 装配点"},
            {"file_path": "app/agents/repository_agent.py", "symbol": "create_repository_agent",
             "anchor": "SummarizationMiddleware(", "note": "摘要中间件装配"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "每个工具都在独立文件中单独注册",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "必须说明工具构建函数与 create_agent 装配两段"},
    },
    {
        "id": "arch-02",
        "category": "architecture",
        "difficulty": "hard",
        "orchestration_complexity": "medium",
        "question": "Agent 的各中间件在请求-模型调用生命周期中分别做什么？执行时机有何区别？",
        "expected_answer": "UserPreferenceMiddleware 在 before_agent（每轮请求仅一次）失效偏好缓存，在 wrap_model_call"
                           "（每次模型调用）把偏好文本追加到 system message 之后（不覆盖原提示词）；SummarizationMiddleware "
                           "在消息 token 达到 7000 时压缩历史并保留最近 10 条；TokenBudgetMiddleware 在 wrap_model_call "
                           "每次模型调用前做预算校验并可能抛 TokenBudgetExceededError。",
        "required_facts": [
            {"text": "before_agent 失效缓存", "any_of": ["before_agent"]},
            {"text": "wrap_model_call 注入/校验", "any_of": ["wrap_model_call"]},
            {"text": "摘要触发阈值 7000", "any_of": ["7000"]},
        ],
        "required_evidence": [
            {"file_path": "app/memory/memory_context.py", "symbol": "UserPreferenceMiddleware",
             "anchor": "def before_agent(self, state, runtime)", "note": "每轮请求失效缓存"},
            {"file_path": "app/memory/memory_context.py", "symbol": "UserPreferenceMiddleware",
             "anchor": "def wrap_model_call(self, request, handler)", "note": "每次模型调用注入偏好"},
            {"file_path": "app/agents/repository_agent.py", "symbol": "create_repository_agent",
             "anchor": 'trigger=("tokens", 7000)', "note": "摘要触发阈值"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "偏好注入使用 dynamic_prompt 覆盖原系统提示词",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "三种中间件 + 两种执行时机"},
    },
    {
        "id": "arch-03",
        "category": "architecture",
        "difficulty": "hard",
        "orchestration_complexity": "high",
        "question": "RAG 相关模块（chunker / code_vector_store / ingestion / retriever）各自的职责边界是什么？",
        "expected_answer": "chunker 只负责把单文件切成片段（不扫描、不入库、不向量化）；code_vector_store 只封装 "
                           "code_chunks 表的增删改查（ORM，不含切块与检索）；ingestion 负责扫描→hash→切块→向量化→入库"
                           "（不负责检索）；retriever 负责向量与关键词召回及 RRF 融合（不切块、不入库）。",
        "required_facts": [
            {"text": "chunker 只切块", "any_of": ["chunker"]},
            {"text": "code_vector_store 只做表 CRUD", "any_of": ["code_vector_store"]},
            {"text": "ingestion 负责索引流程", "any_of": ["ingestion"]},
            {"text": "retriever 负责召回融合", "any_of": ["retriever"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/chunker.py", "symbol": None,
             "anchor": "职责：把单个源码文件切成适合检索的片段", "note": "chunker 职责声明"},
            {"file_path": "app/rag/code_vector_store.py", "symbol": None,
             "anchor": "职责：封装 code_chunks 表的增删改查", "note": "store 职责声明"},
            {"file_path": "app/rag/ingestion.py", "symbol": None,
             "anchor": "职责：扫描仓库", "note": "ingestion 职责声明"},
            {"file_path": "app/rag/retriever.py", "symbol": None,
             "anchor": "职责：向量召回 + 关键词召回 + RRF 融合", "note": "retriever 职责声明"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "ingestion 同时负责检索召回",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "四个模块职责各占一份，边界不能混淆"},
    },
    {
        "id": "arch-04",
        "category": "architecture",
        "difficulty": "hard",
        "orchestration_complexity": "medium",
        "question": "长期记忆的命名空间如何组织？scope 与 category 的白名单与来源规则是什么？",
        "expected_answer": "命名空间为 (users, user_id, preferences)、(users, user_id, profile)、"
                           "(projects, project_id, facts)、(projects, project_id, decisions)；ALLOWED_CATEGORIES 规定 "
                           "user 只能存 preference/profile，project 只能存 fact/decision；ALLOWED_SOURCES 规定 decision "
                           "只接受 user_stated/user_confirmed（model_proposed 被拒绝），fact 还接受 code_verified。",
        "required_facts": [
            {"text": "user/project 命名空间分离", "any_of": ["users", "projects"]},
            {"text": "scope-category 白名单", "any_of": ["ALLOWED_CATEGORIES"]},
            {"text": "来源白名单 ALLOWED_SOURCES", "any_of": ["ALLOWED_SOURCES"]},
            {"text": "model_proposed 不能存为决策", "any_of": ["model_proposed"]},
        ],
        "required_evidence": [
            {"file_path": "app/memory/long_term_memory.py", "symbol": "user_preferences_namespace",
             "anchor": "def user_preferences_namespace(user_id: str)", "note": "命名空间构造"},
            {"file_path": "app/memory/memory_policy.py", "symbol": None,
             "anchor": "ALLOWED_CATEGORIES: dict[str, set[str]]", "note": "类别白名单"},
            {"file_path": "app/memory/memory_policy.py", "symbol": None,
             "anchor": "ALLOWED_SOURCES: dict[str, set[str]]", "note": "来源白名单"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "项目决策可以由模型自行提出后直接保存",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "命名空间 + 两个白名单 + 决策来源限制"},
    },
    {
        "id": "arch-05",
        "category": "architecture",
        "difficulty": "medium",
        "orchestration_complexity": "medium",
        "question": "项目整体如何分层？app/repository/ 这一层在当前架构中扮演什么角色？",
        "expected_answer": "分层：app/agents（Agent 装配）、app/tools（仓库访问工具）、app/orchestration（复杂度路由、"
                           "计划执行、验证与运行记录的编排层）、app/observability（trace 与预算）、app/memory（长期记忆）、"
                           "app/rag（索引与检索）、app/repository（仓库访问抽象层）、app/api（Web 后端）、"
                           "app/mcp（GitHub MCP 客户端）。app/repository 定义了 RepositoryProvider 协议"
                           "（Protocol 结构化类型，非继承）：LocalRepositoryProvider 包装本地 RepositoryAnalyzer，"
                           "GitHubRepositoryProvider 走 GitHub MCP / 本地克隆读取；工具层按协议注入 provider，"
                           "不再绑定全局 REPO_PATH 单例。",
        "required_facts": [
            {"text": "列出主要分层目录", "any_of": ["agents", "tools", "rag"]},
            {"text": "repository 是仓库访问抽象/协议层", "any_of": ["抽象", "协议", "Protocol", "rovider"]},
            {"text": "本地与 GitHub 两种实现", "any_of": ["Local", "GitHub", "本地", "远程"]},
        ],
        "required_evidence": [
            {"file_path": "app/repository/__init__.py", "symbol": None,
             "anchor": "class RepositoryProvider(Protocol):", "note": "Provider 协议接口定义"},
            {"file_path": "app/repository/__init__.py", "symbol": None,
             "anchor": "class LocalRepositoryProvider:", "note": "本地仓库实现"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "工具层通过全局 REPO_PATH 单例访问仓库",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "分层说明 + repository 抽象层定位"},
    },

    # ============================================================= code_review
    {
        "id": "review-01",
        "category": "code_review",
        "difficulty": "hard",
        "orchestration_complexity": "low",
        "question": "ingestion.RepositoryIndexer.index_repository 中，chunk 与向量列表的配对是否存在隐患？请指出。",
        "expected_answer": "存在隐患：代码用 zip(chunks, vectors) 组装入库数据。若 embeddings.embed_documents 返回的向量数与 "
                           "chunks 数不一致（截断/异常返回），zip 会静默按较短一方截断，导致部分 chunk 丢失且没有任何断言或告警。"
                           "建议在组装前校验 len(chunks) == len(vectors)。",
        "required_facts": [
            {"text": "使用 zip 配对", "any_of": ["zip"]},
            {"text": "长度不一致会静默截断", "any_of": ["截断", "长度", "丢失"]},
            {"text": "缺少长度断言/校验", "any_of": ["断言", "校验", "没有", "未"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/ingestion.py", "symbol": "RepositoryIndexer",
             "anchor": "for chunk, emb in zip(chunks, vectors):", "note": "zip 配对处"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/ingestion.py", "symbol": "RepositoryIndexer",
             "anchor": "vectors.extend(self.embeddings.embed_documents(batch))", "note": "向量来源"},
        ],
        "forbidden_or_unsupported_claims": [
            "代码已对 chunks 与 vectors 长度做严格校验",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "必须定位到 zip 配对处并说明静默截断风险"},
    },
    {
        "id": "review-02",
        "category": "code_review",
        "difficulty": "hard",
        "orchestration_complexity": "low",
        "question": "CodeVectorStore.upsert_file_chunks 的事务与异常回滚处理是否完备？",
        "expected_answer": "不完备：方法在同一 session 内执行 delete → add → commit，没有 try/except 与 session.rollback "
                           "的回滚边界。若中途抛异常（例如向量写入失败），异常会向上抛（ingestion 捕获后跳过该文件），"
                           "但当前 session 可能残留未回滚状态，影响后续复用同一 session 的操作。",
        "required_facts": [
            {"text": "先删后写再 commit", "any_of": ["commit"]},
            {"text": "没有显式回滚", "any_of": ["rollback", "回滚"]},
            {"text": "异常向上抛由调用方处理", "any_of": ["异常", "ingestion", "跳过"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/code_vector_store.py", "symbol": "CodeVectorStore",
             "anchor": "def upsert_file_chunks(", "note": "方法定义"},
            {"file_path": "app/rag/code_vector_store.py", "symbol": "CodeVectorStore",
             "anchor": "self.session.commit()", "note": "提交点（无 rollback 分支）"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/ingestion.py", "symbol": "RepositoryIndexer",
             "anchor": "print(f\"  [跳过] 入库失败 {file_path}: {e}\")", "note": "调用方异常处理"},
        ],
        "forbidden_or_unsupported_claims": [
            "方法内已经使用 try/except 回滚事务",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "必须说明缺少回滚边界及其后果"},
    },
    {
        "id": "review-03",
        "category": "code_review",
        "difficulty": "hard",
        "orchestration_complexity": "medium",
        "question": "RepositoryAnalyzer.search_code 与 ingestion 对文件大小的处理是否一致？这会造成什么影响？",
        "expected_answer": "不一致：ingestion 定义了 MAX_FILE_SIZE = 500KB，超过则跳过文件；而 RepositoryAnalyzer.search_code "
                           "没有任何文件大小上限，会对所有目标后缀文件整篇 read_text().splitlines() 逐行扫描，"
                           "大文件会显著拖慢搜索甚至读入超大文本，二者策略不统一。",
        "required_facts": [
            {"text": "ingestion 有 500KB 上限", "any_of": ["500", "MAX_FILE_SIZE"]},
            {"text": "search_code 没有大小上限", "any_of": ["上限", "限制", "没有", "size"]},
            {"text": "影响是性能/内存开销", "any_of": ["性能", "慢", "内存", "开销"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/ingestion.py", "symbol": None,
             "anchor": "MAX_FILE_SIZE = 500 * 1024", "note": "索引侧文件大小上限"},
            {"file_path": "app/tools/repository.py", "symbol": "RepositoryAnalyzer",
             "anchor": "def search_code(", "note": "搜索实现（无大小上限）"},
        ],
        "optional_evidence": [
            {"file_path": "app/tools/repository.py", "symbol": "RepositoryAnalyzer",
             "anchor": "for file_path in self.repo_path.rglob(\"*\"):", "note": "无差别遍历全部文件"},
        ],
        "forbidden_or_unsupported_claims": [
            "search_code 与 ingestion 使用同一个大小上限常量",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "必须对比两侧策略并给出影响"},
    },
    {
        "id": "review-04",
        "category": "code_review",
        "difficulty": "hard",
        "orchestration_complexity": "low",
        "question": "UserPreferenceMiddleware 按 thread_id 缓存用户偏好，请分析 thread_id 获取失败时的行为与潜在影响。",
        "expected_answer": "_thread_id 通过 langgraph.config.get_config() 读取 configurable.thread_id，任何异常都返回 "
                           "\"__default__\"。因此所有拿不到 thread_id 的请求会共用同一个 \"__default__\" 缓存键，"
                           "导致偏好可能被错误复用（跨请求串用），同时 before_agent 也无法按真实请求失效该缓存。",
        "required_facts": [
            {"text": "失败时回落 __default__", "any_of": ["__default__"]},
            {"text": "读取 configurable.thread_id / get_config", "any_of": ["get_config", "thread_id"]},
            {"text": "共用缓存键导致串用/复用", "any_of": ["缓存", "串", "复用"]},
        ],
        "required_evidence": [
            {"file_path": "app/memory/memory_context.py", "symbol": "UserPreferenceMiddleware",
             "anchor": 'return str(configurable.get("thread_id") or "__default__")', "note": "thread_id 兜底值"},
            {"file_path": "app/memory/memory_context.py", "symbol": "UserPreferenceMiddleware",
             "anchor": "def before_agent(self, state, runtime)", "note": "按请求失效缓存的入口"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "拿不到 thread_id 时会直接报错中断请求",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "必须定位兜底值并说明共享缓存键的影响"},
    },
    {
        "id": "review-05",
        "category": "code_review",
        "difficulty": "hard",
        "orchestration_complexity": "low",
        "question": "RepositoryAnalyzer.read_file 的路径越界防护与忽略目录判断是否严谨？有哪些边界问题？",
        "expected_answer": "越界防护用 `self.repo_path not in file_path.parents` 判断。当 relative_path 解析后等于仓库根目录本身"
                           "（例如传入 \".\"）时，其 parents 不含 repo_path，会被误判为越界并抛 ValueError（错误信息不准确）；"
                           "另外 _should_ignore 只按路径片段名精确匹配 IGNORE_DIRS，不处理大小写差异与符号链接。防护整体有效，"
                           "但根目录与软链接边界不严谨。",
        "required_facts": [
            {"text": "越界用 parents 判断", "any_of": ["parents"]},
            {"text": "传入 \".\" 会被误判越界", "any_of": ["越界", "outside", "\".\""]},
            {"text": "_should_ignore 按片段名匹配", "any_of": ["_should_ignore"]},
        ],
        "required_evidence": [
            {"file_path": "app/tools/repository.py", "symbol": "RepositoryAnalyzer",
             "anchor": "if self.repo_path not in file_path.parents:", "note": "越界判断"},
            {"file_path": "app/tools/repository.py", "symbol": "RepositoryAnalyzer",
             "anchor": "def _should_ignore(self, path: Path)", "note": "忽略目录判断"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "read_file 完全没有越界防护",
        ],
        "scoring_rubric": {"fact_weight": 0.6, "evidence_weight": 0.4,
                           "note": "越界逻辑 + 忽略逻辑各说明一处边界"},
    },

    # ======================================================== history_decision
    {
        "id": "hist-01",
        "category": "history_decision",
        "difficulty": "hard",
        "orchestration_complexity": "medium",
        "question": "项目为什么选择 PostgreSQL + pgvector 作为向量存储，而不是独立向量数据库？请给出可核验依据。",
        "expected_answer": "可核验依据来自代码：app/rag/models.py 中 CodeChunk.embedding 使用 pgvector 的 "
                           "Vector(1024)；app/memory/postgres_resources.py 同时用 PostgresSaver（checkpointer）与 "
                           "PostgresStore（向量记忆）复用同一个 PostgreSQL 实例，且 create_session_factory 用 "
                           "postgresql+psycopg:// 连接同一数据库。即把向量能力叠加在已有的 PostgreSQL 上，"
                           "无需再引入独立向量库组件。注意：仓库无 README/docs，Agent 也无法读取 git 提交历史，"
                           "因此本结论只能从上述代码事实推出，不应声称引用了提交记录。",
        "required_facts": [
            {"text": "使用 pgvector 的 Vector 类型", "any_of": ["pgvector"]},
            {"text": "维度 1024", "any_of": ["1024"]},
            {"text": "复用 PostgreSQL 的 PostgresStore/PostgresSaver", "any_of": ["PostgresStore", "PostgresSaver", "PostgreSQL"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/models.py", "symbol": "CodeChunk",
             "anchor": "embedding = Column(Vector(1024), nullable=False)", "note": "pgvector 字段"},
            {"file_path": "app/memory/postgres_resources.py", "symbol": "open_memory_resources",
             "anchor": "PostgresStore.from_conn_string(", "note": "向量记忆复用 PG"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/code_vector_store.py", "symbol": "create_session_factory",
             "anchor": "postgresql+psycopg://", "note": "使用 psycopg3 连接 PostgreSQL"},
        ],
        "forbidden_or_unsupported_claims": [
            "文档明确说明选择 pgvector 是因为 HNSW 索引性能最优",
            "git 提交记录显示该方案由某次提交引入",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "依据必须落在代码事实上，不得臆测未记录的动机"},
        "evidence_source_note": "仓库无 README/docs，且 Agent 无 git 工具；依据仅限代码内可核实事实。",
    },
    {
        "id": "hist-02",
        "category": "history_decision",
        "difficulty": "hard",
        "orchestration_complexity": "low",
        "question": "摘要压缩为什么改用独立的小模型（qwen-flash）而不是主模型？证据是什么？",
        "expected_answer": "证据来自代码注释：app/agents/repository_agent.py 中 SummarizationMiddleware 的配置注释"
                           "写明「摘要用【专用小模型】（SUMMARIZATION_MODEL 配置，默认 qwen-flash），摘要不需要推理能力，"
                           "用主模型做会显著拖慢整轮响应（实测 53~70s → 1~2s）」；app/models/llm.py 的 get_summarization_llm "
                           "读取 SUMMARIZATION_MODEL 环境变量（默认 qwen-flash）。即摘要只需压缩历史、不需要推理能力，"
                           "用小模型换取速度和成本。",
        "required_facts": [
            {"text": "默认模型 qwen-flash", "any_of": ["qwen-flash"]},
            {"text": "实测 53~70 秒 → 1~2 秒", "any_of": ["53", "70"]},
            {"text": "通过 SUMMARIZATION_MODEL 配置", "any_of": ["SUMMARIZATION_MODEL"]},
        ],
        "required_evidence": [
            {"file_path": "app/agents/repository_agent.py", "symbol": "create_repository_agent",
             "anchor": "实测 53~70s → 1~2s", "note": "SummarizationMiddleware 注释中的实测结论"},
            {"file_path": "app/models/llm.py", "symbol": "get_summarization_llm",
             "anchor": "def get_summarization_llm():", "note": "摘要专用模型函数，读 SUMMARIZATION_MODEL"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "因为 qwen-flash 的推理能力比主模型更强",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "必须引用注释中的实测数字，不得编造其他依据"},
        "evidence_source_note": "依据为代码内注释（仓库无 README/docs）。",
    },
    {
        "id": "hist-03",
        "category": "history_decision",
        "difficulty": "hard",
        "orchestration_complexity": "low",
        "question": "为什么 Embedding 调用要显式设置 check_embedding_ctx_length=False？依据是什么？",
        "expected_answer": "依据来自 app/memory/postgres_resources.py 中 create_embeddings 的注释：LangChain 默认 "
                           "check_embedding_ctx_length=True 会先把文本转成 token 数组再发送，而 DashScope 只接受原始字符串，"
                           "否则报错「contents is neither str nor list of str」；因此显式设置 check_embedding_ctx_length=False。",
        "required_facts": [
            {"text": "设置 check_embedding_ctx_length=False", "any_of": ["check_embedding_ctx_length"]},
            {"text": "DashScope 只接受原始字符串", "any_of": ["DashScope", "字符串"]},
            {"text": "否则报 contents is neither str nor list of str", "any_of": ["contents", "str"]},
        ],
        "required_evidence": [
            {"file_path": "app/memory/postgres_resources.py", "symbol": "create_embeddings",
             "anchor": "check_embedding_ctx_length=False,", "note": "关键参数设置"},
            {"file_path": "app/memory/postgres_resources.py", "symbol": "create_embeddings",
             "anchor": "DashScope 只接受原始字符串", "note": "注释中的原因说明"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "设置该参数是为了节省 token 成本",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "必须引用注释中的报错信息作为依据"},
        "evidence_source_note": "依据为代码内注释。",
    },
    {
        "id": "hist-04",
        "category": "history_decision",
        "difficulty": "medium",
        "orchestration_complexity": "low",
        "question": "向量化为什么必须分批、且单批不超过 10 条？依据是什么？",
        "expected_answer": "依据来自 app/rag/ingestion.py：EMBED_BATCH_SIZE = 10，注释写明「DashScope 限制单次最多 10 条文本，"
                           "超过会报 batch size is invalid」；index_repository 按该批次循环调用 embeddings.embed_documents。",
        "required_facts": [
            {"text": "批量大小 10", "any_of": ["10"]},
            {"text": "常量 EMBED_BATCH_SIZE", "any_of": ["EMBED_BATCH_SIZE"]},
            {"text": "DashScope 限制 batch size 报错", "any_of": ["batch size is invalid", "限制"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/ingestion.py", "symbol": None,
             "anchor": "EMBED_BATCH_SIZE = 10", "note": "批量大小常量"},
            {"file_path": "app/rag/ingestion.py", "symbol": None,
             "anchor": "batch size is invalid", "note": "注释中的报错依据"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/ingestion.py", "symbol": "RepositoryIndexer",
             "anchor": "for i in range(0, len(embedding_texts), EMBED_BATCH_SIZE):", "note": "分批循环"},
        ],
        "forbidden_or_unsupported_claims": [
            "因为单批 10 条的速度最快",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "常量 + 注释依据"},
        "evidence_source_note": "依据为代码内注释。",
    },
    {
        "id": "hist-05",
        "category": "history_decision",
        "difficulty": "hard",
        "orchestration_complexity": "low",
        "question": "项目为什么用 psycopg3 驱动，并在连接串上显式写成 postgresql+psycopg:// ？",
        "expected_answer": "依据来自 app/rag/code_vector_store.py 中 create_session_factory 的注释：SQLAlchemy 的 "
                           "\"postgresql://\" 默认指向 psycopg2 驱动，而本项目安装的是 psycopg3（包名 psycopg），"
                           "因此统一改写为 \"postgresql+psycopg://\" 以明确驱动。运行环境中确实安装的是 psycopg 3.x "
                           "（requirements/环境已装 psycopg 3.3.6），与其说明一致。",
        "required_facts": [
            {"text": "SQLAlchemy 默认指向 psycopg2", "any_of": ["psycopg2"]},
            {"text": "本项目使用 psycopg3", "any_of": ["psycopg3"]},
            {"text": "改写为 postgresql+psycopg://", "any_of": ["postgresql+psycopg"]},
        ],
        "required_evidence": [
            {"file_path": "app/rag/code_vector_store.py", "symbol": "create_session_factory",
             "anchor": "def create_session_factory()", "note": "连接串构造"},
            {"file_path": "app/rag/code_vector_store.py", "symbol": "create_session_factory",
             "anchor": 'url = url.replace("postgresql://", "postgresql+psycopg://", 1)', "note": "驱动改写"},
        ],
        "optional_evidence": [
            {"file_path": "app/rag/code_vector_store.py", "symbol": None,
             "anchor": "本项目装的是 psycopg3", "note": "注释中的原因说明"},
        ],
        "forbidden_or_unsupported_claims": [
            "psycopg3 是因为性能比 psycopg2 快 10 倍才选择的（无记录依据）",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "必须说明默认驱动与显式改写的关系"},
        "evidence_source_note": "依据为代码内注释与已安装依赖版本。",
    },

    # ======================================================== multi_part_audit（为 Plan 设计的多子任务题）
    {
        "id": "audit-01",
        "category": "multi_part_audit",
        "difficulty": "hard",
        "orchestration_complexity": "high",
        "question": "编排入口的 Agent 装配（create_repository_agent_factory）在 direct / react / step 三种模式上有哪些差异？"
                    "运行记录（RunRecord/RunLog）由哪些文件实现？请按文件给出依据。",
        "expected_answer": "app/agents/repository_agent.py 的 create_repository_agent_factory 按模式装配：direct/react 注入用户偏好"
                           "（UserPreferenceMiddleware）、挂摘要压缩（SummarizationMiddleware，7000 tokens 触发）"
                           "与 checkpointer，并保留记忆工具；step 为一次性任务工人——不注入偏好、无摘要、无 "
                           "checkpointer，且从工具列表中过滤掉三个记忆工具。运行记录由 "
                           "app/orchestration/models.py 的 RunRecord（字段定义）与 app/orchestration/records.py 的 "
                           "RunLog（JSONL 持久化）实现。",
        "required_facts": [
            {"text": "装配工厂 create_repository_agent_factory", "any_of": ["create_repository_agent_factory"]},
            {"text": "step 不注入偏好/无记忆工具", "any_of": ["step"]},
            {"text": "RunRecord 定义在 app/orchestration/models.py", "any_of": ["models.py"]},
            {"text": "RunLog 持久化在 app/orchestration/records.py", "any_of": ["records.py"]},
        ],
        "required_evidence": [
            {"file_path": "app/agents/repository_agent.py", "symbol": "create_repository_agent_factory",
             "anchor": "def create_repository_agent_factory(llm, tools, budget, tracer, memory, checkpointer=None):",
             "note": "编排入口装配工厂"},
            {"file_path": "app/agents/repository_agent.py", "symbol": "create_repository_agent_factory",
             "anchor": "SummarizationMiddleware(", "note": "direct/react 摘要压缩装配"},
            {"file_path": "app/orchestration/models.py", "symbol": "RunRecord",
             "anchor": "class RunRecord(BaseModel):", "note": "运行记录字段定义"},
            {"file_path": "app/orchestration/records.py", "symbol": "RunLog",
             "anchor": "class RunLog:", "note": "运行记录 JSONL 持久化"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "step 模式也会注入用户偏好",
            "装配工厂仍定义在 app/main.py 中",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "四个独立子问题（三模式差异 + 两个记录实现文件），任一遗漏即不完整"},
    },
    {
        "id": "audit-02",
        "category": "multi_part_audit",
        "difficulty": "hard",
        "orchestration_complexity": "high",
        "question": "单次输入 Token 上限（max_input_tokens）在项目里一共有几个配置点？当前取值分别是多少？"
                    "超出时抛出的异常类定义在哪个文件？请枚举全部配置点，不要遗漏。",
        "expected_answer": "共两个配置点：app/api/main.py 的 TokenBudget(max_input_tokens=60000) 与 "
                           "evals/config.py 的 MAX_INPUT_TOKENS = 60_000（评估运行器使用）。超出时抛出 "
                           "TokenBudgetExceededError，该异常类定义在 app/observability/budget_middleware.py。",
        "required_facts": [
            {"text": "app/api/main.py 配置 60000", "any_of": ["60000", "60_000"]},
            {"text": "evals/config.py 也有一处配置", "any_of": ["evals/config.py", "MAX_INPUT_TOKENS"]},
            {"text": "异常定义在 app/observability/budget_middleware.py", "any_of": ["budget_middleware"]},
        ],
        "required_evidence": [
            {"file_path": "app/api/main.py", "symbol": "_run_orchestrator_sync",
             "anchor": "max_input_tokens=60000,", "note": "Web 后端预算配置"},
            {"file_path": "evals/config.py", "symbol": "MAX_INPUT_TOKENS",
             "anchor": "MAX_INPUT_TOKENS = 60_000", "note": "评估侧预算配置"},
            {"file_path": "app/observability/budget_middleware.py", "symbol": "TokenBudgetExceededError",
             "anchor": "class TokenBudgetExceededError(RuntimeError):", "note": "超限异常定义"},
        ],
        "optional_evidence": [],
        "forbidden_or_unsupported_claims": [
            "单次输入上限为 30000",
            "只有一个配置点",
            "TokenBudgetExceededError 定义在 app/observability/token_budget.py",
        ],
        "scoring_rubric": {"fact_weight": 0.5, "evidence_weight": 0.5,
                           "note": "枚举完整性是关键：漏掉任一配置点即扣分"},
    },
]
