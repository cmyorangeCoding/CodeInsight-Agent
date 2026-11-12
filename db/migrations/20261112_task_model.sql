-- 2026-11-12 任务模型：仓库 → 任务（task_id，多轮会话）→ run（每一轮提问）
--
-- 变更点：
-- 1. runs 增加 task_id：同一任务的多轮 run 共享一个 task_id；
-- 2. 迁移前的历史 run 各自成为一个单轮任务（task_id = run_id），
--    它们原有的 thread_id（run-xxx）继续沿用，不会断链；
-- 3. 新增 (task_id, created_at) 索引，支撑任务内按对话顺序查询。
--
-- 说明：runs.requested_thread_id 已废弃（任务线程本身就是会话上下文来源），
-- 这里不删除该列，避免影响已有数据；新代码不再读写它。

ALTER TABLE public.runs ADD COLUMN IF NOT EXISTS task_id character varying(32);

UPDATE public.runs SET task_id = run_id WHERE task_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_runs_task_time
    ON public.runs USING btree (task_id, created_at);
