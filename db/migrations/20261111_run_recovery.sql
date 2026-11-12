-- =============================================================================
-- 迁移：CodeInsight-Agent 业务任务断点恢复（2026-11）
-- 适用：PostgreSQL 14+，可重复执行（IF NOT EXISTS）
--
-- 兼容性：
--   - 只做 ADD COLUMN / CREATE TABLE，不重写已有历史记录；
--   - 历史 runs 的 thread_id 保持 NULL：它们当初使用的是 repo-{repository_id}
--     共享线程，无法可靠对应到某一次执行，恢复调度器会把它们标记 failed 并写原因，
--     不会伪装成「已恢复」。
-- =============================================================================

BEGIN;

-- 1) runs 表增量字段 ---------------------------------------------------------
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS thread_id           varchar(64);
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS requested_thread_id varchar(128);
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS requested_strategy  varchar(32);
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS user_id             varchar(64);
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS attempt             integer NOT NULL DEFAULT 0;
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS worker_id           varchar(64);
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS lease_until         timestamp without time zone;
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS cancel_requested    boolean NOT NULL DEFAULT false;
ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS last_error          text;

CREATE INDEX IF NOT EXISTS idx_runs_status_lease
    ON public.runs (status, lease_until);

-- 2) 事件逐条持久化（SSE 续传 / 崩溃补发） ----------------------------------
CREATE TABLE IF NOT EXISTS public.run_events (
    id          serial PRIMARY KEY,
    run_id      varchar(32) NOT NULL,
    sequence    integer NOT NULL,
    event_type  varchar(32) NOT NULL,
    data        text,
    created_at  timestamp without time zone NOT NULL,
    CONSTRAINT uq_run_events_run_seq UNIQUE (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_run_events_run_seq
    ON public.run_events (run_id, sequence);

-- 3) 有副作用工具的幂等记录 ------------------------------------------------
CREATE TABLE IF NOT EXISTS public.tool_executions (
    idempotency_key  varchar(160) NOT NULL,
    run_id           varchar(32) NOT NULL,
    tool_name        varchar(64) NOT NULL,
    status           varchar(16) NOT NULL,
    result           text,
    error            text,
    attempt          integer NOT NULL DEFAULT 1,
    created_at       timestamp without time zone NOT NULL,
    updated_at       timestamp without time zone NOT NULL,
    CONSTRAINT tool_executions_pkey PRIMARY KEY (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_tool_executions_run
    ON public.tool_executions (run_id);

COMMIT;

-- 说明：
--   应用启动时 app/db/__init__.py:init_tables() 会自动执行等价的 ADD COLUMN IF NOT EXISTS
--   与 create_all，因此手工迁移不是必须的；此脚本用于受控环境 / DBA 审核场景。
