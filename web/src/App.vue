<script setup lang="ts">
import { ref, computed, onUnmounted, onMounted, watch, type Ref } from 'vue';
import { api, type RunEvent, type RunStatus, type CreateRunRequest, type HistoryRun, type RepoGroup } from './api';
import { marked } from 'marked';
import hljs from 'highlight.js';
import 'highlight.js/styles/github.css';

// ============================= marked 配置 =============================
marked.setOptions({ gfm: true, breaks: true });
const renderer = new marked.Renderer();
// marked v13+：renderer.code 签名变成接收对象参数 ({text, lang, escaped})
renderer.code = function ({ text, lang }: { text: string; lang?: string }) {
  const language = lang && hljs.getLanguage(lang) ? lang : 'plaintext';
  const highlighted = hljs.highlight(text, { language }).value;
  return `<pre class="hljs"><code class="hljs language-${language}">${highlighted}</code></pre>`;
};
marked.use({ renderer });

// ============================= 仓库分组 =============================
// 左侧栏第一层：仓库列表（后端返回所有出现过的仓库 + run 数）
const repoGroups = ref<RepoGroup[]>([]);
// 当前选中的仓库分组（点左侧仓库项时更新）
const activeRepo: Ref<RepoGroup | null> = ref<RepoGroup | null>(null);
// 当前选中仓库下的 runs
const runs = ref<HistoryRun[]>([]);
// runs 列表加载中
const loadingRuns = ref(false);

// 加载所有仓库分组
async function loadRepos() {
  try {
    const res = await api.history.listRepos();
    repoGroups.value = res.items;
    // 如果当前没选中仓库，默认选第一个
    if (!activeRepo.value && res.items.length > 0) {
      activeRepo.value = res.items[0];
    }
  } catch { /* 后端没起或 DB 连不上时静默 */ }
}

// 加载某个仓库下的 runs
async function loadRunsOf(repo: RepoGroup | null) {
  if (!repo) { runs.value = []; return; }
  loadingRuns.value = true;
  try {
    const res = await api.history.listRuns({
      repo_source: repo.repo_source,
      repo_identifier: repo.repo_identifier,
      limit: 200,
    });
    runs.value = res.items;
  } catch { /* 静默 */ }
  finally { loadingRuns.value = false; }
}

// 监听 activeRepo 变化 → 重新拉 runs
watch(activeRepo, (repo) => loadRunsOf(repo), { immediate: false });

// ============================= GitHub Token 配置 =============================
async function loadTokenConfig() {
  try {
    const cfg = await api.config.get();
    githubTokenSet.value = cfg.github_token_set;
    githubTokenMasked.value = cfg.github_token_masked;
  } catch { /* 后端可能没启动，静默 */ }
}
async function saveToken() {
  const token = githubTokenInput.value.trim();
  tokenSaveLoading.value = true;
  tokenSaveMsg.value = '';
  try {
    const cfg = await api.config.update(token);
    githubTokenSet.value = cfg.github_token_set;
    githubTokenMasked.value = cfg.github_token_masked;
    githubTokenInput.value = '';
    tokenSaveMsg.value = cfg.github_token_set ? '✅ Token 已保存，下次分析自动生效' : '✅ Token 已清除';
    setTimeout(() => { tokenSaveMsg.value = ''; }, 3000);
  } catch (e: any) {
    tokenSaveMsg.value = '❌ ' + (e?.message || '保存失败');
  } finally {
    tokenSaveLoading.value = false;
  }
}
// 页面加载时读一次 token 状态
loadTokenConfig();

// ============================= UI 状态 =============================
const sidebarCollapsed = ref(false);
function toggleSidebar() { sidebarCollapsed.value = !sidebarCollapsed.value; }
// 进度/证据面板折叠（收起后回答独占全宽）
const progressCollapsed = ref(false);
function toggleProgress() { progressCollapsed.value = !progressCollapsed.value; }
// 回答面板最大化（全宽）
const answerFullscreen = ref(false);
function toggleAnswerFull() { answerFullscreen.value = !answerFullscreen.value; }

// ============================= 主视图状态 =============================
const repoSource = ref<'local' | 'github'>('github');
const localPath = ref('E:\\GiteePro\\CodeInsight-Agent');
const githubUrl = ref('langchain-ai/langchain');

// 路由策略：auto / direct / react / plan
const strategy = ref<string>('auto');

// GitHub Token 动态配置
const githubTokenInput = ref('');
const githubTokenMasked = ref('');
const githubTokenSet = ref(false);
const tokenSaveMsg = ref('');
const tokenSaveLoading = ref(false);

const question = ref('');
const running = ref(false);
const currentRunId: Ref<string | null> = ref<string | null>(null);
const runStatus: Ref<RunStatus | null> = ref<RunStatus | null>(null);
const events = ref<RunEvent[]>([]);
let eventSource: { close(): void } | null = null;
const errorMsg = ref('');

const selectedHistory = computed(() =>
  runs.value.find(h => h.run_id === currentRunId.value) || null
);

// ============================= 计算属性 =============================
const finalAnswer = computed(() => {
  if (currentRunId.value) {
    const evt = events.value.slice().reverse().find(e => e.type === 'answer_final');
    if (evt) return (evt.data?.answer as string) || '';
  }
  if (selectedHistory.value?.answer) return selectedHistory.value.answer;
  return runStatus.value?.answer || '';
});

const answerHtml = computed(() => {
  if (!finalAnswer.value) return '';
  try { return marked.parse(finalAnswer.value) as string; }
  catch { return finalAnswer.value; }
});

const retrievalEvents = computed(() =>
  events.value.filter(e => e.type === 'retrieval_completed')
);

// ============================= 工具函数 =============================
function clearView() {
  running.value = false;
  currentRunId.value = null;
  runStatus.value = null;
  events.value = [];
  question.value = '';
  errorMsg.value = '';
  eventSource?.close();
  eventSource = null;
}

// 格式化 ISO 时间戳（后端返回的 created_at 是 ISO 字符串）
function formatTime(isoStr: string | null): string {
  if (!isoStr) return '';
  const ts = new Date(isoStr).getTime();
  if (isNaN(ts)) return isoStr;
  const d = new Date(ts);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n: number) => n.toString().padStart(2, '0');
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (sameDay) return hm;
  const diffDays = Math.floor((now.getTime() - ts) / 86400000);
  if (diffDays === 1) return `昨天 ${hm}`;
  if (diffDays < 7) return `${diffDays} 天前 ${hm}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

// ============================= 创建新任务 =============================
async function onSubmit() {
  if (!question.value.trim() || running.value) return;
  running.value = true;
  errorMsg.value = '';
  events.value = [];

  const req: CreateRunRequest = {
    question: question.value,
    repo_source: repoSource.value,
    repo_path: repoSource.value === 'local' ? localPath.value : undefined,
    github_url: repoSource.value === 'github' ? githubUrl.value : undefined,
    strategy: strategy.value,
  };

  try {
    const res = await api.createRun(req);
    currentRunId.value = res.run_id;

    // 如果后端返回的仓库分组不在 repoGroups 里（新建仓库），刷新 repoGroups
    const repoId = repoSource.value === 'local' ? localPath.value : githubUrl.value || '';
    const repoExists = repoGroups.value.some(
      g => g.repo_source === repoSource.value && g.repo_identifier === repoId
    );
    if (!repoExists) {
      await loadRepos();
    }

    // 选中当前仓库
    activeRepo.value = repoGroups.value.find(
      g => g.repo_source === repoSource.value && g.repo_identifier === repoId
    ) || null;

    subscribeEvents(res.run_id);

    setTimeout(async () => {
      try { runStatus.value = await api.getRun(res.run_id); } catch { /* ignore */ }
    }, 200);
  } catch (e: any) {
    errorMsg.value = e?.message || String(e);
    running.value = false;
    currentRunId.value = null;
  }
}

// ============================= 订阅 SSE =============================
function subscribeEvents(id: string) {
  const url = api.eventsUrl(id);
  const es = new EventSource(url);
  const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled'];

  const handler = (type: string) => (evt: MessageEvent) => {
    try {
      const data = JSON.parse(evt.data);
      events.value.push({ type, ...data });
    } catch {
      try { events.value.push(JSON.parse(evt.data)); } catch { /* ignore */ }
    }
    if (TERMINAL_TYPES.includes(type)) {
      running.value = false;
      es.close();
      eventSource = null;
      fetchFinalStatus(id);
    }
  };

  const KNOWN_TYPES = [
    'run_started', 'repo_connected', 'routing_selected', 'decision_event',
    'indexing_started', 'indexing_progress', 'indexing_completed',
    'retrieval_completed',
    'tool_started', 'tool_completed',
    'llm_started', 'llm_error', 'usage_updated',
    'plan_event', 'verification_updated',
    'answer_delta', 'answer_final',
    'run_completed', 'run_failed', 'run_cancelled',
    'info',
  ];
  KNOWN_TYPES.forEach(t => es.addEventListener(t, handler(t)));

  es.onmessage = (evt) => {
    try { events.value.push(JSON.parse(evt.data)); } catch { /* ignore */ }
  };

  es.onerror = () => {
    es.close();
    const lastType = events.value[events.value.length - 1]?.type;
    if (lastType && !TERMINAL_TYPES.includes(lastType)) {
      setTimeout(async () => {
        try {
          const st = await api.getRun(id);
          runStatus.value = st;
          if (st.status !== 'running') {
            running.value = false;
            fetchFinalStatus(id);
          }
        } catch { /* ignore */ }
      }, 500);
    } else {
      running.value = false;
    }
  };

  eventSource = { close: () => es.close() };
}

// 任务结束后刷新 runs 列表（后端已自动写 DB，前端只需重新拉）
async function fetchFinalStatus(id: string) {
  try {
    runStatus.value = await api.getRun(id);
  } catch { /* ignore */ }
  // 刷新当前仓库下的 runs（新 run 会出现在顶部）
  if (activeRepo.value) {
    await loadRunsOf(activeRepo.value);
  }
  await loadRepos();  // 更新各仓库的 run_count
}

// ============================= 点击仓库分组 → 切换左侧栏 runs =============================
function selectRepoGroup(group: RepoGroup) {
  activeRepo.value = group;
  clearView();
}

// ============================= 点击 run → 查看历史 =============================
async function selectRun(item: HistoryRun) {
  if (running.value) return;
  clearView();
  currentRunId.value = item.run_id;
  question.value = item.question;

  // 优先用 DB 里存的 events 快照（恢复完整的执行进度 + 代码证据）
  const created = new Date(item.created_at || Date.now()).getTime();
  if (item.events && item.events.length > 0) {
    // Tracer 存的是 [{type, data}]，前端 RunEvent 需要补 run_id / sequence / timestamp
    events.value = item.events.map((ev, i) => ({
      run_id: item.run_id,
      sequence: i + 1,
      type: ev.type,
      timestamp: created + i * 100,  // 递增 100ms 避免时间相同
      data: ev.data || {},
    }));
    // 如果 events 里没有 answer_final（旧数据可能没存），补上
    if (!item.events.some(e => e.type === 'answer_final')) {
      events.value.push({
        run_id: item.run_id,
        sequence: events.value.length + 1,
        type: 'answer_final',
        timestamp: created,
        data: { answer: item.answer },
      });
    }
    if (!item.events.some(e => e.type === 'run_completed' || e.type === 'run_failed')) {
      events.value.push({
        run_id: item.run_id,
        sequence: events.value.length + 1,
        type: item.status === 'failed' ? 'run_failed' : 'run_completed',
        timestamp: created,
        data: { status: item.status, duration_ms: item.duration_ms || 0 },
      });
    }
  } else {
    // 兜底：旧数据（events 列为空），只拼 answer_final + run_completed
    events.value = [];
    if (item.answer) {
      events.value.push({
        run_id: item.run_id, sequence: 1, type: 'answer_final',
        timestamp: created, data: { answer: item.answer },
      });
      events.value.push({
        run_id: item.run_id, sequence: 2, type: 'run_completed',
        timestamp: created,
        data: { status: item.status, duration_ms: item.duration_ms || 0 },
      });
    }
  }

  try {
    runStatus.value = await api.getRun(item.run_id);
  } catch { /* ignore */ }
}

// ============================= 删除 run =============================
async function deleteRun(runId: string) {
  if (!confirm('确定删除这条历史记录？')) return;
  try {
    await api.history.deleteRun(runId);
    runs.value = runs.value.filter(r => r.run_id !== runId);
    if (currentRunId.value === runId) clearView();
    await loadRepos();
  } catch { /* ignore */ }
}

// ============================= 清空当前仓库的所有 run =============================
async function clearCurrentRepo() {
  if (!activeRepo.value) return;
  if (!confirm(`确定清空仓库 "${activeRepo.value.repo_identifier}" 的所有历史记录？`)) return;
  try {
    await api.history.deleteByRepo(activeRepo.value.repo_source, activeRepo.value.repo_identifier);
    runs.value = [];
    if (currentRunId.value) clearView();
    await loadRepos();
    if (repoGroups.value.length > 0) {
      activeRepo.value = repoGroups.value[0];
    } else {
      activeRepo.value = null;
    }
  } catch { /* ignore */ }
}

// ============================= 取消任务 =============================
async function onCancel() {
  if (!currentRunId.value) return;
  try { await api.cancelRun(currentRunId.value); } catch { /* ignore */ }
}

// ============================= 新建空白对话 =============================
function newChat() {
  if (running.value) return;
  clearView();
}

// ============================= 生命周期 =============================
onMounted(() => {
  loadRepos();
});
onUnmounted(() => { eventSource?.close(); });
</script>

<template>
  <div class="app-layout" :class="{ collapsed: sidebarCollapsed }">
    <!-- ========================= 左侧栏：仓库分组 + 任务 ========================= -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <div class="sidebar-header">
        <div class="logo-row">
          <div class="logo">🧠 CodeInsight</div>
          <button class="toggle-btn" @click="toggleSidebar" :title="sidebarCollapsed ? '展开侧栏' : '收起侧栏'">
            {{ sidebarCollapsed ? '»' : '«' }}
          </button>
        </div>
        <button v-if="!sidebarCollapsed" class="new-chat-btn" @click="newChat" :disabled="running">
          <span class="plus">＋</span> 新建对话
        </button>
      </div>

      <!-- 仓库分组列表 -->
      <div class="repo-groups">
        <div v-if="repoGroups.length === 0" class="repo-empty">
          暂无历史记录
        </div>

        <div v-for="group in repoGroups" :key="group.repo_source + '::' + group.repo_identifier"
             class="repo-group"
             :class="{ active: activeRepo?.repo_source === group.repo_source && activeRepo?.repo_identifier === group.repo_identifier }">
          <!-- 仓库标题行（点击切换） -->
          <div class="repo-header" @click="selectRepoGroup(group)">
            <span class="repo-icon">{{ group.repo_source === 'github' ? '🐙' : '📁' }}</span>
            <span class="repo-name" :title="group.repo_identifier">
              {{ group.repo_identifier.length > 35 ? group.repo_identifier.slice(-35) : group.repo_identifier }}
            </span>
            <span class="repo-count">{{ group.run_count }}</span>
          </div>

          <!-- 该仓库下的 runs -->
          <div v-if="activeRepo?.repo_source === group.repo_source && activeRepo?.repo_identifier === group.repo_identifier"
               class="repo-runs">
            <div v-if="loadingRuns" class="repo-runs-loading">加载中…</div>
            <div v-else-if="runs.length === 0" class="repo-runs-empty">暂无任务</div>
            <div v-else class="run-items">
              <div v-for="item in runs" :key="item.run_id"
                   class="run-item"
                   :class="{ active: currentRunId === item.run_id }"
                   @click="selectRun(item)">
                <div class="ri-header">
                  <span class="ri-status" :class="'status-' + item.status">
                    {{ item.status === 'completed' ? '✓' : item.status === 'failed' ? '✗' : item.status === 'running' ? '⏳' : '○' }}
                  </span>
                  <span class="ri-time">{{ formatTime(item.created_at) }}</span>
                  <button class="ri-delete" @click.stop="deleteRun(item.run_id)" title="删除">✕</button>
                </div>
                <div class="ri-question">
                  {{ item.question.length > 45 ? item.question.slice(0, 45) + '…' : item.question }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 底部 -->
      <div v-if="activeRepo && runs.length > 0" class="sidebar-footer">
        <button class="clear-all-btn" @click="clearCurrentRepo">
          清空 · {{ activeRepo.repo_identifier.slice(-20) }}
        </button>
      </div>
    </aside>

    <!-- ========================= 右侧主内容 ========================= -->
    <div class="main-content">
      <header>
        <div class="header-row">
          <h1>🧠 CodeInsight-Agent</h1>
          <button v-if="sidebarCollapsed" class="toggle-btn" @click="toggleSidebar" title="展开侧栏">«</button>
        </div>
        <p class="subtitle">代码库分析工作台 —— 输入问题，实时查看 Agent 执行过程与代码证据</p>
      </header>

      <main :class="{ 'progress-collapsed': progressCollapsed, 'answer-full': answerFullscreen }">
        <!-- A. 仓库配置区 -->
        <section class="panel repo-config">
          <h2>📦 仓库配置</h2>
          <div class="radio-group">
            <label><input type="radio" v-model="repoSource" value="local" /> 本地仓库</label>
            <label><input type="radio" v-model="repoSource" value="github" /> GitHub 仓库</label>
          </div>
          <div v-if="repoSource === 'local'" class="input-row">
            <label>本地路径：</label>
            <input v-model="localPath" placeholder="E:\GiteePro\CodeInsight-Agent" />
          </div>
          <div v-else class="github-config">
            <div class="input-row">
              <label>仓库地址：</label>
              <input v-model="githubUrl" placeholder="langchain-ai/langchain 或完整 URL" />
            </div>
            <div class="token-row">
              <label title="GitHub Token 未配置时，匿名限流仅 60 次/小时；配置后 5000 次/小时">
                GitHub Token：
              </label>
              <span class="token-status" :class="{ ok: githubTokenSet, warn: !githubTokenSet }">
                {{ githubTokenSet ? '已配置 ' + githubTokenMasked : '未配置（匿名模式）' }}
              </span>
              <div class="token-input-wrap">
                <input
                  v-model="githubTokenInput"
                  placeholder="粘贴 ghp_ 或 github_pat_ 开头的 Token，留空清除"
                  type="password"
                  autocomplete="off"
                />
                <button @click="saveToken" :disabled="tokenSaveLoading || !githubTokenInput.trim()">
                  {{ tokenSaveLoading ? '保存中…' : '💾 保存' }}
                </button>
              </div>
              <p v-if="tokenSaveMsg" class="token-msg">{{ tokenSaveMsg }}</p>
            </div>
          </div>

          <!-- 路由策略选择 -->
          <div class="strategy-row">
            <label>🧭 分析策略：</label>
            <select v-model="strategy" title="Auto 会根据问题复杂度自动选">
              <option value="auto">Auto（自动路由，推荐）</option>
              <option value="react">ReAct（深度推理 · 多轮工具交互）</option>
              <option value="direct">Direct（快速定位 · 1~2 次工具收敛）</option>
              <option value="plan">Plan（【专业模式】分步规划 · 先规划再执行）</option>
            </select>
            <span class="strategy-hint" v-if="strategy !== 'auto'">
              已强制：{{ strategy === 'react' ? 'ReAct 多轮推理' : strategy === 'direct' ? 'Direct 快速定位' : 'Plan 分步执行' }}
            </span>
          </div>
        </section>

        <!-- B. 对话区 -->
        <section class="panel chat">
          <h2>💬 对话</h2>
          <textarea
            v-model="question"
            placeholder="输入代码分析问题，例如：这个项目的核心架构是什么？"
            :disabled="running"
            @keydown.ctrl.enter="onSubmit"
            rows="3"
          />
          <div class="button-row">
            <button @click="onSubmit" :disabled="running || !question.trim()">
              {{ running ? '分析中…' : '🚀 分析' }}
            </button>
            <button v-if="running" @click="onCancel" class="cancel">⏹ 取消</button>
          </div>
          <p v-if="errorMsg" class="error">⚠ {{ errorMsg }}</p>
        </section>

        <!-- C+E. 左栏：执行进度 + 代码证据（可折叠整个左栏） -->
        <div class="left-column">
          <section class="panel progress">
            <div class="panel-header">
              <h2>⏱ 执行进度</h2>
              <button class="panel-btn" @click="toggleProgress" :title="progressCollapsed ? '展开左栏' : '收起左栏'">
                {{ progressCollapsed ? '»' : '«' }}
              </button>
            </div>
              <div v-if="events.length === 0" class="empty">
                {{ running ? '等待任务开始…' : '（历史任务无实时事件流，以下为最终结果）' }}
              </div>
              <ul v-else class="event-list">
            <li v-for="(evt, i) in events" :key="i" :class="'evt-' + evt.type">
              <span class="evt-seq">#{{ evt.sequence }}</span>
              <span class="evt-type">[{{ evt.type }}]</span>
              <span class="evt-data">
                <template v-if="evt.type === 'run_started'">开始</template>
                <template v-else-if="evt.type === 'repo_connected'">
                  仓库已连接：{{ evt.data?.name || evt.data?.path || evt.data }}
                </template>
                <template v-else-if="evt.type === 'routing_selected'">策略：{{ evt.data?.strategy }}</template>
                <template v-else-if="evt.type === 'decision_event'">
                  路由：{{ evt.data?.strategy }}（{{ evt.data?.source }}, 置信度 {{ evt.data?.confidence }}）
                </template>
                <template v-else-if="evt.type === 'indexing_started'">
                  开始索引仓库…
                </template>
                <template v-else-if="evt.type === 'indexing_progress'">
                  <span class="evt-progress">📦 索引中 {{ evt.data?.seq }}/{{ evt.data?.total }}</span>
                  <span class="evt-sub">（已处理 {{ evt.data?.indexed }} 新文件）</span>
                </template>
                <template v-else-if="evt.type === 'indexing_completed'">
                  索引：{{ evt.data?.note || 'done' }}
                </template>
                <template v-else-if="evt.type === 'retrieval_completed'">
                  RAG 检索：候选 {{ evt.data?.candidate_count }} → 选中 {{ evt.data?.selected_count }}
                </template>
                <template v-else-if="evt.type === 'tool_started'">
                  🛠 调用工具：{{ evt.data?.tool }}
                </template>
                <template v-else-if="evt.type === 'tool_completed'">
                  ✅ 工具完成：{{ evt.data?.tool }}
                  <span v-if="evt.data?.elapsed_ms">({{ evt.data?.elapsed_ms }}ms)</span>
                  <span v-if="evt.data?.status === 'error'" class="evt-error"> — {{ evt.data?.error }}</span>
                </template>
                <template v-else-if="evt.type === 'llm_started'">
                  🤖 LLM 思考中… ({{ evt.data?.model }})
                </template>
                <template v-else-if="evt.type === 'llm_error'">
                  ❌ LLM 错误：{{ evt.data?.error }}
                </template>
                <template v-else-if="evt.type === 'usage_updated'">
                  Token：in={{ evt.data?.input_tokens }} out={{ evt.data?.output_tokens }}
                </template>
                <template v-else-if="evt.type === 'plan_event'">
                  📋 Plan：{{ evt.data?.action }}
                  <span v-if="evt.data?.step_count">({{ evt.data?.step_count }} 步骤)</span>
                  <span v-if="evt.data?.error" class="evt-error"> — {{ evt.data?.error }}</span>
                </template>
                <template v-else-if="evt.type === 'verification_updated'">
                  验证：{{ evt.data?.passed ? '✅ 通过' : '⚠ 未通过' }}
                  <span v-if="evt.data?.issues?.length"> ({{ evt.data?.issues?.length }} 问题)</span>
                </template>
                <template v-else-if="evt.type === 'run_completed'">
                  🏁 完成 ({{ evt.data?.status }}) — {{ evt.data?.duration_ms }}ms
                </template>
                <template v-else-if="evt.type === 'run_failed'">❌ 失败：{{ evt.data?.error }}</template>
                <template v-else-if="evt.type === 'run_cancelled'">⏹ 已取消</template>
                <template v-else-if="evt.type === 'info'">ℹ {{ JSON.stringify(evt.data).slice(0, 120) }}</template>
                <template v-else>{{ evt.type }} {{ JSON.stringify(evt.data).slice(0, 100) }}</template>
              </span>
            </li>
          </ul>
          </section>

          <!-- E. 代码证据区（也在左栏里） -->
          <section class="panel evidence">
            <div class="panel-header">
              <h2>🔍 代码证据</h2>
            </div>
            <div v-if="retrievalEvents.length === 0" class="empty">暂无检索证据</div>
            <div v-else v-for="(revt, ri) in retrievalEvents" :key="ri" class="evidence-group">
              <p class="evidence-meta">
                RRF 融合 ({{ revt.data?.elapsed_ms }}ms):
                候选 {{ revt.data?.candidate_count }} → 选中 {{ revt.data?.selected_count }}
              </p>
              <ul class="chunk-list">
                <li v-for="(chunk, ci) in revt.data?.chunks" :key="ci" class="chunk">
                  <div class="chunk-header">
                    <code>{{ chunk.file_path }}</code>
                    <span v-if="chunk.line_start">{{ chunk.line_start }}-{{ chunk.line_end }}</span>
                    <span class="rrf">rrf={{ chunk.rrf_score?.toFixed(3) }}</span>
                  </div>
                  <div v-if="chunk.symbol_name" class="symbol">符号: {{ chunk.symbol_name }}</div>
                </li>
              </ul>
            </div>
          </section>
        </div>

        <!-- D. 最终回答（独立右栏，可展开全宽） -->
        <section class="panel answer" :class="{ full: answerFullscreen }">
          <div class="panel-header">
            <h2>📝 最终回答</h2>
            <div class="panel-actions">
              <button class="panel-btn" @click="toggleProgress" v-if="progressCollapsed" title="展开左栏">«</button>
              <button class="panel-btn" @click="toggleAnswerFull" :title="answerFullscreen ? '还原' : '放大'">
                {{ answerFullscreen ? '↙' : '↗' }}
              </button>
            </div>
          </div>
          <div v-if="!finalAnswer" class="empty">等待回答生成…</div>
          <div v-else class="answer-markdown" v-html="answerHtml"></div>
        </section>
      </main>
    </div>
  </div>
</template>

<style>
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #f5f7fa;
  color: #1f2937;
}
.app-layout { display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; transition: grid-template-columns 0.25s ease; }
.app-layout.collapsed { grid-template-columns: 0 1fr; }

/* ===== 左侧栏 ===== */
.sidebar {
  background: #ffffff; border-right: 1px solid #e5e7eb;
  display: flex; flex-direction: column;
  position: sticky; top: 0; height: 100vh;
  overflow: hidden; transition: width 0.25s ease;
}
.sidebar.collapsed {
  width: 0; border-right: none;
}
.sidebar.collapsed > * { display: none; }
.sidebar.collapsed .sidebar-header { display: block; }
.sidebar.collapsed .logo-row { flex-direction: column; gap: 4px; }
.sidebar.collapsed .logo { display: none; }
.sidebar.collapsed .toggle-btn { transform: rotate(180deg); }

.sidebar-header { padding: 16px; border-bottom: 1px solid #f3f4f6; flex-shrink: 0; }
.logo-row {
  display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
}
.logo { font-size: 16px; font-weight: 700; color: #111827; flex: 1; }

/* 折叠/展开按钮 —— 侧栏和主 header 都能用 */
.toggle-btn {
  background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 6px;
  width: 28px; height: 28px; cursor: pointer; font-size: 14px; color: #6b7280;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  transition: background 0.15s, color 0.15s;
}
.toggle-btn:hover { background: #e5e7eb; color: #111827; }
.new-chat-btn {
  width: 100%; background: #3b82f6; border: none; border-radius: 8px;
  padding: 10px 14px; color: white; font-weight: 500; cursor: pointer; font-size: 14px;
  display: flex; align-items: center; justify-content: center; gap: 6px; transition: background 0.15s;
}
.new-chat-btn:hover:not(:disabled) { background: #2563eb; }
.new-chat-btn:disabled { background: #d1d5db; cursor: not-allowed; }
.new-chat-btn .plus { font-size: 18px; font-weight: 700; line-height: 1; }

/* 仓库分组容器 */
.repo-groups { flex: 1; overflow-y: auto; padding: 8px; }
.repo-groups::-webkit-scrollbar { width: 5px; }
.repo-groups::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }

.repo-empty { text-align: center; color: #9ca3af; font-size: 13px; padding: 24px 12px; }

/* 仓库分组项 */
.repo-group { margin-bottom: 8px; }
.repo-header {
  display: flex; align-items: center; gap: 6px;
  padding: 10px 12px; border-radius: 8px;
  cursor: pointer; transition: background 0.1s;
  background: #f9fafb; border: 1px solid #f3f4f6;
}
.repo-header:hover { background: #f3f4f6; }
.repo-group.active .repo-header {
  background: #eff6ff; border-color: #bfdbfe;
}
.repo-icon { font-size: 14px; flex-shrink: 0; }
.repo-name {
  flex: 1; font-size: 12px; color: #374151;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-family: monospace;
}
.repo-count {
  background: #e5e7eb; color: #6b7280;
  font-size: 11px; padding: 1px 6px; border-radius: 10px;
  flex-shrink: 0; min-width: 20px; text-align: center;
}
.repo-group.active .repo-count { background: #bfdbfe; color: #1d4ed8; }

/* 该仓库下的 runs */
.repo-runs { margin-top: 4px; padding-left: 4px; }
.repo-runs-loading { font-size: 12px; color: #9ca3af; padding: 4px 12px; }
.repo-runs-empty { font-size: 12px; color: #9ca3af; padding: 4px 12px; }

.run-items { padding-left: 6px; border-left: 1px solid #e5e7eb; margin-left: 8px; }
.run-item {
  padding: 8px 10px; border-radius: 6px; cursor: pointer;
  margin-bottom: 3px; transition: background 0.1s;
}
.run-item:hover { background: #f3f4f6; }
.run-item.active { background: #eff6ff; }

.ri-header { display: flex; align-items: center; gap: 5px; font-size: 10px; color: #9ca3af; margin-bottom: 3px; }
.ri-status { font-size: 9px; flex-shrink: 0; }
.status-completed { color: #059669; }
.status-failed { color: #dc2626; }
.status-running { color: #d97706; }
.status-pending { color: #9ca3af; }
.status-cancelled { color: #dc2626; }
.ri-time { flex: 1; }
.ri-delete {
  border: none; background: transparent; color: #9ca3af; cursor: pointer; font-size: 10px;
  padding: 0 2px; opacity: 0; transition: opacity 0.15s, color 0.15s;
}
.run-item:hover .ri-delete { opacity: 1; }
.ri-delete:hover { color: #dc2626; }

.ri-question {
  font-size: 12px; color: #374151; line-height: 1.4;
  word-break: break-word;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}

/* 底部 */
.sidebar-footer { padding: 10px 16px; border-top: 1px solid #f3f4f6; }
.clear-all-btn {
  width: 100%; background: transparent; border: 1px solid #fecaca;
  border-radius: 6px; padding: 6px 10px; color: #dc2626; font-size: 12px;
  cursor: pointer; transition: background 0.15s;
}
.clear-all-btn:hover { background: #fef2f2; }

/* ===== 主内容 ===== */
.main-content { padding: 24px 32px; width: 100%; }
.header-row { display: flex; align-items: center; gap: 12px; }
header h1 { margin: 0 0 4px; font-size: 24px; color: #111827; flex: 1; }
header .subtitle { margin: 0 0 24px; color: #6b7280; }
main { display: grid; grid-template-columns: 400px 1fr; gap: 16px; }
/* 左栏折叠或 answer 独立全屏 → 单列铺满 */
main.progress-collapsed,
main.answer-full { grid-template-columns: 1fr; }
main.progress-collapsed .left-column { display: none; }
main.answer-full .left-column { display: none; }

.repo-config, .chat { grid-column: 1 / -1; }

/* ========= 左栏（进度 + 证据） ========= */
.left-column { display: flex; flex-direction: column; gap: 16px; }

/* ========= 右栏（回答） ========= */
.panel.answer { transition: all 0.25s ease; }
.panel.answer.full { /* 单独全屏不额外加样式，answer-full 在 main 上处理 */ }

/* ========= 面板 header（标题 + 操作按钮） ========= */
.panel-header { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; border-bottom: 1px solid #f3f4f6; padding-bottom: 8px; }
.panel-header h2 { flex: 1; margin: 0 !important; border: none !important; padding: 0 !important; }
.panel-actions { display: flex; gap: 4px; }
.panel-btn {
  background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 6px;
  width: 28px; height: 28px; cursor: pointer; font-size: 13px; color: #6b7280;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s, color 0.15s;
}
.panel-btn:hover { background: #e5e7eb; color: #111827; }

.panel {
  background: #ffffff; border-radius: 12px; padding: 16px 20px;
  border: 1px solid #e5e7eb; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.panel h2 {
  margin: 0 0 12px; font-size: 16px; font-weight: 600; color: #111827;
  border-bottom: 1px solid #f3f4f6; padding-bottom: 8px;
}
.empty { color: #9ca3af; font-style: italic; font-size: 13px; }

.radio-group { display: flex; gap: 20px; margin-bottom: 12px; color: #374151; }
.input-row { display: flex; gap: 8px; align-items: center; }
.input-row label { white-space: nowrap; color: #6b7280; }

input, textarea {
  box-sizing: border-box; background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 10px 12px; color: #1f2937; font-family: inherit; font-size: 14px;
  transition: border-color 0.15s, background 0.15s;
}
/* 普通块级容器里 → 铺满 100% */
textarea { width: 100%; resize: vertical; min-height: 80px; }
/* flex 容器里的 input → 自动伸缩（label + input 一排） */
.input-row input { flex: 1; width: auto; }
input:focus, textarea:focus {
  outline: none; border-color: #3b82f6; background: #ffffff;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
}

.button-row { margin-top: 10px; display: flex; gap: 10px; }
button {
  background: #3b82f6; border: none; border-radius: 8px; padding: 10px 20px;
  color: white; font-weight: 500; cursor: pointer; font-size: 14px; transition: background 0.15s;
}
button:hover:not(:disabled) { background: #2563eb; }
button:disabled { background: #d1d5db; cursor: not-allowed; }
button.cancel { background: #ef4444; }
button.cancel:hover { background: #dc2626; }
.error { color: #dc2626; margin-top: 8px; }

/* GitHub Token 配置区 */
.github-config { display: flex; flex-direction: column; gap: 12px; }
.token-row { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; background: #f9fafb; border-radius: 8px; border: 1px solid #e5e7eb; }
.token-row > label { font-weight: 500; color: #374151; font-size: 13px; }
.token-status { font-size: 12px; padding: 2px 8px; border-radius: 4px; width: fit-content; }
.token-status.ok { background: #dcfce7; color: #166534; }
.token-status.warn { background: #fef3c7; color: #92400e; }
.token-input-wrap { display: flex; gap: 8px; align-items: center; }
.token-input-wrap input { flex: 1; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; font-family: monospace; }
.token-input-wrap button { padding: 6px 14px; font-size: 13px; }
.token-msg { margin: 0; font-size: 12px; color: #059669; }

/* 路由策略选择 */
.strategy-row { display: flex; gap: 10px; align-items: center; margin-top: 12px; padding-top: 12px; border-top: 1px solid #e5e7eb; }
.strategy-row label { font-weight: 500; color: #374151; font-size: 14px; }
.strategy-row select { padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; background: white; font-size: 13px; min-width: 200px; }
.strategy-hint { font-size: 12px; color: #6b7280; }

.event-list { list-style: none; padding: 0; margin: 0; max-height: 400px; overflow-y: auto; }
.event-list li {
  padding: 8px 10px; border-bottom: 1px solid #f3f4f6; font-size: 13px;
  display: flex; gap: 8px; align-items: flex-start;
}
.event-list li:last-child { border-bottom: none; }
.event-list li:hover { background: #f9fafb; }
.evt-seq { color: #9ca3af; font-variant-numeric: tabular-nums; flex-shrink: 0; }
.evt-type {
  color: #6366f1; font-family: monospace; font-size: 12px;
  background: #eef2ff; padding: 1px 6px; border-radius: 4px; flex-shrink: 0;
}
.evt-data { color: #374151; flex: 1; word-break: break-word; }
.evt-error { color: #dc2626; }
.evt-run_completed .evt-type { color: #059669; background: #ecfdf5; }
.evt-run_failed .evt-type, .evt-run_cancelled .evt-type { color: #dc2626; background: #fef2f2; }
.evt-tool_error .evt-type, .evt-llm_error .evt-type { color: #dc2626; background: #fef2f2; }

.answer-markdown {
  line-height: 1.75; font-size: 15px; color: #1f2937;
}
.answer-markdown h1 { font-size: 20px; margin: 16px 0 8px; color: #111827; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }
.answer-markdown h2 { font-size: 17px; margin: 14px 0 6px; color: #1f2937; }
.answer-markdown h3 { font-size: 15px; margin: 12px 0 4px; color: #374151; }
.answer-markdown h4 { font-size: 14px; margin: 10px 0 4px; color: #374151; }
.answer-markdown p { margin: 8px 0; }
.answer-markdown strong { color: #111827; font-weight: 600; }
.answer-markdown em { color: #4b5563; }
.answer-markdown ul, .answer-markdown ol { padding-left: 24px; margin: 8px 0; }
.answer-markdown li { margin: 3px 0; }
.answer-markdown a { color: #3b82f6; text-decoration: none; }
.answer-markdown a:hover { text-decoration: underline; }
.answer-markdown :not(pre) > code {
  background: #f3f4f6; color: #be185d; padding: 1px 6px; border-radius: 4px; font-size: 13px;
  font-family: "Cascadia Code", "Fira Code", Consolas, monospace;
}
.answer-markdown pre {
  background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 12px 14px; overflow-x: auto; margin: 10px 0; font-size: 13px; line-height: 1.55;
}
.answer-markdown pre code { background: transparent; color: #24292f; padding: 0; font-size: inherit; }
.answer-markdown table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
.answer-markdown th, .answer-markdown td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
.answer-markdown th { background: #f9fafb; font-weight: 600; }
.answer-markdown blockquote {
  border-left: 3px solid #d1d5db; margin: 10px 0; padding: 4px 12px;
  color: #6b7280; background: #f9fafb; border-radius: 0 6px 6px 0;
}

.chunk-list { list-style: none; padding: 0; margin: 0; }
.chunk {
  background: #f9fafb; border-radius: 8px; padding: 10px 12px;
  margin-bottom: 8px; border: 1px solid #f3f4f6; transition: border-color 0.15s;
}
.chunk:hover { border-color: #3b82f6; background: #f0f9ff; }
.chunk-header { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.chunk-header code { color: #7c3aed; font-size: 13px; background: #ede9fe; padding: 1px 6px; border-radius: 4px; }
.chunk-header span { color: #6b7280; font-size: 12px; }
.chunk-header .rrf { margin-left: auto; color: #059669; font-weight: 500; }
.symbol { color: #d97706; font-size: 12px; margin-top: 4px; font-family: monospace; }
.evidence-group { margin-bottom: 16px; }
.evidence-meta { color: #6b7280; font-size: 12px; margin: 0 0 8px; }

.event-list::-webkit-scrollbar, .answer-markdown::-webkit-scrollbar { width: 6px; }
.event-list::-webkit-scrollbar-thumb, .answer-markdown::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }

@media (max-width: 1024px) {
  main { grid-template-columns: 1fr; }
  .app-layout { grid-template-columns: 240px 1fr; }
}
@media (max-width: 768px) {
  .app-layout { grid-template-columns: 1fr; }
  .sidebar { position: relative; height: auto; border-right: none; border-bottom: 1px solid #e5e7eb; }
}
</style>
