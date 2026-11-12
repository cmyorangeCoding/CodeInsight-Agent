/** FastAPI 后端 API 客户端。 */

export interface CreateRunRequest {
  question: string;
  repo_source: 'local' | 'github';
  repo_path?: string;
  github_url?: string;
  strategy?: string;
  /** 传了 = 在该任务里继续追问；不传 = 新建任务 */
  task_id?: string;
  user_id?: string;
}

export interface CreateRunResponse {
  run_id: string;
  task_id: string;
  thread_id: string;
  status: string;
  repository_id: string;
}

export interface RunStatus {
  run_id: string;
  task_id?: string;
  status: string;
  question: string;
  repo_source: string;
  repo_identifier: string;
  repository_id: string;
  strategy?: string;
  answer: string;
  error?: string;
  elapsed_ms?: number;
}

export interface Health {
  status: string;
  python_version: string;
  repository_id: string;
  github_mcp_available: boolean;
}

export interface ConfigResponse {
  github_token_set: boolean;
  github_token_masked: string;
}

export interface RunEvent {
  run_id: string;
  sequence: number;
  type: string;
  timestamp: number;
  data: Record<string, any>;
}

// ---- 历史任务相关 ----
export interface HistoryRun {
  run_id: string;
  task_id: string | null;
  repo_source: string;
  repo_identifier: string;
  repository_id: string | null;
  question: string;
  answer: string;
  status: string;
  strategy: string | null;
  duration_ms: number | null;
  created_at: string | null;
  updated_at: string | null;
  events: Array<{ type: string; data: Record<string, any> }> | null;
}

export interface RepoGroup {
  repo_source: string;
  repo_identifier: string;
  run_count: number;
}

/** 任务：同一 task_id 下多轮提问的聚合（一个任务 = 一次多轮会话） */
export interface TaskGroup {
  task_id: string;
  repo_source: string;
  repo_identifier: string;
  repository_id: string | null;
  title: string;
  run_count: number;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface TaskDetail {
  task: TaskGroup;
  runs: HistoryRun[];
}

const BASE = import.meta.env.VITE_API_BASE_URL || '';

async function jsonFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json', ...(opts?.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => jsonFetch<Health>('/api/health'),
  createRun: (req: CreateRunRequest) =>
    jsonFetch<CreateRunResponse>('/api/runs', { method: 'POST', body: JSON.stringify(req) }),
  getRun: (runId: string) => jsonFetch<RunStatus>(`/api/runs/${runId}`),
  cancelRun: (runId: string) => jsonFetch<{ run_id: string; cancelled: boolean }>(
    `/api/runs/${runId}/cancel`, { method: 'POST' }),
  eventsUrl: (runId: string) => `${BASE}/api/runs/${runId}/events`,

  // --- 运行时配置 ---
  config: {
    get: () => jsonFetch<ConfigResponse>('/api/config'),
    update: (github_token: string) =>
      jsonFetch<ConfigResponse>('/api/config', {
        method: 'POST',
        body: JSON.stringify({ github_token }),
      }),
  },

  // --- 历史任务 ---
  history: {
    listRepos: () => jsonFetch<{ items: RepoGroup[] }>('/api/history/repos'),
    listTasks: (params?: { repo_source?: string; repo_identifier?: string }) => {
      const qs = new URLSearchParams();
      if (params?.repo_source) qs.set('repo_source', params.repo_source);
      if (params?.repo_identifier) qs.set('repo_identifier', params.repo_identifier);
      const suffix = qs.toString() ? `?${qs.toString()}` : '';
      return jsonFetch<{ items: TaskGroup[] }>(`/api/history/tasks${suffix}`);
    },
    getTask: (taskId: string) => jsonFetch<TaskDetail>(`/api/history/tasks/${taskId}`),
    deleteTask: (taskId: string) =>
      jsonFetch<{ task_id: string; deleted_count: number }>(
        `/api/history/tasks/${taskId}`, { method: 'DELETE' }),
    listRuns: (params?: { repo_source?: string; repo_identifier?: string; limit?: number; offset?: number }) => {
      const qs = new URLSearchParams();
      if (params?.repo_source) qs.set('repo_source', params.repo_source);
      if (params?.repo_identifier) qs.set('repo_identifier', params.repo_identifier);
      if (params?.limit) qs.set('limit', String(params.limit));
      if (params?.offset) qs.set('offset', String(params.offset));
      const suffix = qs.toString() ? `?${qs.toString()}` : '';
      return jsonFetch<{ total: number; items: HistoryRun[] }>(`/api/history/runs${suffix}`);
    },
    deleteRun: (runId: string) =>
      jsonFetch<{ run_id: string; deleted: boolean }>(`/api/history/runs/${runId}`, { method: 'DELETE' }),
    deleteByRepo: (repo_source: string, repo_identifier: string) => {
      const qs = new URLSearchParams({ repo_source, repo_identifier });
      return jsonFetch<{ deleted_count: number }>(`/api/history/runs?${qs.toString()}`, { method: 'DELETE' });
    },
  },
};
