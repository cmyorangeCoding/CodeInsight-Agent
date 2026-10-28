-- =============================================================================
-- CodeInsight-Agent 数据库结构快照
-- 适用：PostgreSQL 14+ + pgvector 0.5+
--
-- 表分组：
--   1. code_chunks          —— RAG 代码向量索引表（项目自有，必须初始化）
--   2. runs                 —— 历史任务表（路径、问题、回答、事件快照；后端启动时也会自动创建）
--   3. checkpoint_*         —— LangGraph 短期记忆（对话断点、线程状态）
--   4. store / store_vectors —— LangGraph 长期记忆（项目事实、用户偏好、向量检索）
--
-- 说明：
--   - checkpoint_* / store_* 正常由 PostgresSaver.setup() 与 PostgresStore.setup()
--     在首次任务运行时自动创建；这里保留完整结构快照，便于对照与空库一次性初始化。
--   - 本文件适用于空库首次初始化；重复执行时 ALTER TABLE ADD CONSTRAINT 会报重复约束，
--     可忽略或先删除同名约束。
--
-- 用法：
--   psql "postgresql://user:password@127.0.0.1:5432/codeinsight" -f db/schema.sql
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;


-- =============================================================================
-- 1. RAG 代码向量索引表
--    embedding: 1024 维向量（text-embedding-v3）
--    content_tsv: 生成列，用于 PostgreSQL 全文检索；与向量召回做 RRF 融合
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.code_chunks (
    id              text NOT NULL,
    repository_id   text NOT NULL,
    file_path       text NOT NULL,
    symbol_name     text,
    language        text,
    line_start      integer NOT NULL,
    line_end        integer NOT NULL,
    content         text NOT NULL,
    embedding_text  text NOT NULL,
    embedding       vector(1024) NOT NULL,
    content_hash    text NOT NULL,
    indexed_at      timestamp with time zone DEFAULT now() NOT NULL,
    content_tsv     tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'simple'::regconfig,
            ((COALESCE(file_path, ''::text) || ' '::text)
             || COALESCE(symbol_name, ''::text) || ' '::text)
             || COALESCE(content, ''::text)
        )
    ) STORED
);

ALTER TABLE ONLY public.code_chunks
    ADD CONSTRAINT code_chunks_pkey PRIMARY KEY (id);

CREATE INDEX IF NOT EXISTS idx_code_chunks_repository
    ON public.code_chunks USING btree (repository_id);
CREATE INDEX IF NOT EXISTS idx_code_chunks_file
    ON public.code_chunks USING btree (repository_id, file_path);
CREATE INDEX IF NOT EXISTS idx_code_chunks_tsv
    ON public.code_chunks USING gin (content_tsv);
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding
    ON public.code_chunks USING hnsw (embedding vector_cosine_ops);


-- =============================================================================
-- 2. 历史任务表
--    events_json: 完整执行事件快照（与实时 SSE 同格式），用于历史回放
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.runs (
    run_id           character varying(32) NOT NULL,
    repo_source      character varying(16) NOT NULL,
    repo_identifier  character varying(512) NOT NULL,
    repository_id    character varying(128),
    question         text NOT NULL,
    answer           text,
    status           character varying(16) NOT NULL,
    strategy         character varying(32),
    duration_ms      double precision,
    created_at       timestamp without time zone NOT NULL,
    updated_at       timestamp without time zone NOT NULL,
    events_json      text
);

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_pkey PRIMARY KEY (run_id);

CREATE INDEX IF NOT EXISTS idx_runs_created
    ON public.runs USING btree (created_at);
CREATE INDEX IF NOT EXISTS idx_runs_repo_time
    ON public.runs USING btree (repo_source, repo_identifier, created_at);


-- =============================================================================
-- 3. LangGraph 短期记忆（checkpointer）
--    thread_id 与仓库绑定：repo-{repository_id}，同一仓库多次提问自动续接上下文
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.checkpoint_migrations (
    v integer NOT NULL
);

ALTER TABLE ONLY public.checkpoint_migrations
    ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);

CREATE TABLE IF NOT EXISTS public.checkpoints (
    thread_id             text NOT NULL,
    checkpoint_ns         text DEFAULT ''::text NOT NULL,
    checkpoint_id         text NOT NULL,
    parent_checkpoint_id  text,
    type                  text,
    checkpoint            jsonb NOT NULL,
    metadata              jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);

CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx
    ON public.checkpoints USING btree (thread_id);

CREATE TABLE IF NOT EXISTS public.checkpoint_blobs (
    thread_id      text NOT NULL,
    checkpoint_ns  text DEFAULT ''::text NOT NULL,
    channel        text NOT NULL,
    version        text NOT NULL,
    type           text NOT NULL,
    blob           bytea
);

ALTER TABLE ONLY public.checkpoint_blobs
    ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);

CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx
    ON public.checkpoint_blobs USING btree (thread_id);

CREATE TABLE IF NOT EXISTS public.checkpoint_writes (
    thread_id      text NOT NULL,
    checkpoint_ns  text DEFAULT ''::text NOT NULL,
    checkpoint_id  text NOT NULL,
    task_id        text NOT NULL,
    idx            integer NOT NULL,
    channel        text NOT NULL,
    type           text,
    blob           bytea NOT NULL,
    task_path      text DEFAULT ''::text NOT NULL
);

ALTER TABLE ONLY public.checkpoint_writes
    ADD CONSTRAINT checkpoint_writes_pkey
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);

CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx
    ON public.checkpoint_writes USING btree (thread_id);


-- =============================================================================
-- 4. LangGraph 长期记忆（PostgresStore）
--    store: 记忆记录本体；store_vectors: 对 text 字段的 1024 维向量索引
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.store_migrations (
    v integer NOT NULL
);

ALTER TABLE ONLY public.store_migrations
    ADD CONSTRAINT store_migrations_pkey PRIMARY KEY (v);

CREATE TABLE IF NOT EXISTS public.vector_migrations (
    v integer NOT NULL
);

ALTER TABLE ONLY public.vector_migrations
    ADD CONSTRAINT vector_migrations_pkey PRIMARY KEY (v);

CREATE TABLE IF NOT EXISTS public.store (
    prefix       text NOT NULL,
    key          text NOT NULL,
    value        jsonb NOT NULL,
    created_at   timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at   timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    expires_at   timestamp with time zone,
    ttl_minutes  integer
);

ALTER TABLE ONLY public.store
    ADD CONSTRAINT store_pkey PRIMARY KEY (prefix, key);

CREATE INDEX IF NOT EXISTS idx_store_expires_at
    ON public.store USING btree (expires_at) WHERE (expires_at IS NOT NULL);
CREATE INDEX IF NOT EXISTS store_prefix_idx
    ON public.store USING btree (prefix text_pattern_ops);

CREATE TABLE IF NOT EXISTS public.store_vectors (
    prefix      text NOT NULL,
    key         text NOT NULL,
    field_name  text NOT NULL,
    embedding   vector(1024),
    created_at  timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at  timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE ONLY public.store_vectors
    ADD CONSTRAINT store_vectors_pkey PRIMARY KEY (prefix, key, field_name);
ALTER TABLE ONLY public.store_vectors
    ADD CONSTRAINT store_vectors_prefix_key_fkey
    FOREIGN KEY (prefix, key) REFERENCES public.store(prefix, key) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS store_vectors_embedding_idx
    ON public.store_vectors USING hnsw (embedding vector_cosine_ops);
