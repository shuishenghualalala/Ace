/**
 * 定时任务页：cron 引擎管理面板。
 *
 * 数据源：GET /api/cron/jobs + /api/cron/stats
 * 操作：创建 / 启停 / 立即触发 / 删除（对应 /api/cron/jobs POST|DELETE|toggle|run）
 *
 * 布局：
 *   1. 顶部 KPI 4 张（总数 / 启用 / 周期 / 一次性 + 24h 失败数）
 *   2. 工具栏：搜索 + 类型筛选 + 新建按钮
 *   3. cron 列表：每行展示 名称 / 调度 / 目标会话 / 下次触发 / 上次结果 / 启停开关 / 操作
 *   4. 详情侧栏：选中行时显示完整字段 + 完整 query + 历史时间线（last_run_at）
 *   5. 新建侧栏：name / schedule / query / session_id / deliver
 */

import { backendApi, type CronDeliveryTarget, type CronJob, type CronJobRun } from '../backend-client';
import { $, escapeHtml, notify, state, type TabKey } from '../state';

export type FilterKey = 'all' | 'enabled' | 'disabled' | 'interval' | 'once';

type OpenSessionFn = (sessionId: string) => void | Promise<void>;

let openSessionFn: OpenSessionFn = async () => {};
let setTabFn: (tab: TabKey) => void = () => {};

/** index.ts 注入：跳转对话页查看 cron 执行结果。 */
export function setCronCallbacks(opts: { openSession: OpenSessionFn; setTab: (tab: TabKey) => void }): void {
  openSessionFn = opts.openSession;
  setTabFn = opts.setTab;
}

async function openJobConversation(job: CronJob): Promise<void> {
  if (isNewSessionDelivery(job)) {
    notify('该任务每次触发都会新建会话，没有固定对话可查看');
    return;
  }
  const sid = job.session_id?.trim();
  if (!sid) {
    notify('该任务未绑定会话，无法查看对话结果');
    return;
  }
  setTabFn('chat');
  await openSessionFn(sid);
  notify('已打开目标会话，可在对话区查看执行记录');
}

interface ViewState {
  jobs: CronJob[];
  /** KPI stats computed locally from jobs (master backend no longer exposes /api/cron/stats). */
  stats: { total: number; enabled: number; disabled: number; interval: number; once: number; failed_recent: number; upcoming_60s: number };
  filter: FilterKey;
  scope: 'all' | 'current';
  search: string;
  selectedId: string | null;
  drawer: 'detail' | 'create' | null;
  loading: boolean;
  error: string | null;
  lastLoadedAt: number | null;
  detailRuns: CronJobRun[];
  detailRunsLoading: boolean;
  deliveryTargets: CronDeliveryTarget[];
  deliveryTargetsLoaded: boolean;
}

const view: ViewState = {
  jobs: [],
  stats: computeStats([]),
  filter: 'all',
  scope: 'all',
  search: '',
  selectedId: null,
  drawer: null,
  loading: false,
  error: null,
  lastLoadedAt: null,
  detailRuns: [],
  detailRunsLoading: false,
  deliveryTargets: [
    { id: 'new_session', label: '新会话', platform: 'local' },
    { id: 'local', label: '当前会话', platform: 'local' },
  ],
  deliveryTargetsLoaded: false,
};

let cronPageSlots: {
  shell: HTMLElement;
  kpi: HTMLElement;
  toolbar: HTMLElement;
  list: HTMLElement;
  drawer: HTMLElement;
} | null = null;

const FILTER_LABELS: Record<FilterKey, string> = {
  all: '全部',
  enabled: '启用中',
  disabled: '已停用',
  interval: '周期',
  once: '一次性',
};

const CRON_TEMPLATES = [
  {
    id: 'daily-news',
    title: '每日 AI 新闻推送',
    desc: '每天汇总 AI 领域的重要动态，筛选 3-5 条值得关注的信息。',
    icon: 'newspaper',
    schedule: 'every 1d',
    query: '关注当天 AI 领域的重要动态，侧重 AI coding 与具身智能方向。筛选 3-5 条有价值的信息，简要说明事件内容及值得关注的原因。',
  },
  {
    id: 'weekly-report',
    title: '每周工作周报',
    desc: '汇总本周项目进展、风险和下周建议动作。',
    icon: 'clipboard',
    schedule: 'every 7d',
    query: '请汇总本周项目进展，按已完成、进行中、风险阻塞、下周建议动作输出一份简洁周报。',
  },
  {
    id: 'meeting-prep',
    title: '会议前准备',
    desc: '会议开始前整理议题、目标、待确认问题和关键结论。',
    icon: 'checks',
    schedule: 'in 2h',
    query: '请帮我整理即将开始会议的准备材料：会议目标、议题清单、待确认问题、需要提前看的背景和建议结论。',
  },
  {
    id: 'daily-reminder',
    title: '每日提醒',
    desc: '固定提醒一个日常动作，并给出简短上下文。',
    icon: 'alarm',
    schedule: 'every 1d',
    query: '提醒我完成今天的固定事项，并根据当前上下文给出一个简短建议。',
  },
] as const;

/** 周期任务快捷间隔（value 为后端 parse_schedule 可识别的字符串）。 */
export const INTERVAL_PRESETS = [
  { id: '30m', label: '每 30 分钟', value: 'every 30m' },
  { id: '1h', label: '每 1 小时', value: 'every 1h' },
  { id: '1d', label: '每天', value: 'every 1d' },
] as const;

/** 一次性任务快捷延迟。 */
export const ONCE_PRESETS = [
  { id: '30m', label: '30 分钟后', value: '30分钟后' },
  { id: '1h', label: '1 小时后', value: '1小时后' },
] as const;

const SCHEDULE_UNIT_SUFFIX: Record<string, string> = { s: '秒', m: '分钟', h: '小时', d: '天' };

/** 根据数字 + 单位生成 schedule 字符串（供表单与单测使用）。 */
export function buildScheduleString(mode: 'interval' | 'once', amount: number, unit: string): string {
  const n = Math.max(1, Math.floor(amount));
  const u = unit in SCHEDULE_UNIT_SUFFIX ? unit : 'm';
  if (mode === 'interval') return `every ${n}${u}`;
  return `${n}${SCHEDULE_UNIT_SUFFIX[u]}后`;
}

const BJ_TZ = 'Asia/Shanghai';

/** 把任意 Date 转换为北京时间（Asia/Shanghai）的年月日时分秒。 */
function toBeijingComponents(d: Date): { year: number; month: number; day: number; hour: number; minute: number; second: number } {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: BJ_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const get = (type: string) => parseInt(parts.find((p) => p.type === type)?.value || '0', 10);
  return {
    year: get('year'),
    month: get('month'),
    day: get('day'),
    hour: get('hour'),
    minute: get('minute'),
    second: get('second'),
  };
}

/** 将 Date 格式化为 `<input type="date">` 所需值（北京时间）。 */
export function formatDateInputValue(d: Date): string {
  const c = toBeijingComponents(d);
  const mm = String(c.month).padStart(2, '0');
  const dd = String(c.day).padStart(2, '0');
  return `${c.year}-${mm}-${dd}`;
}

/** 将 Date 格式化为 `<input type="time">` 所需值（北京时间）。 */
export function formatTimeInputValue(d: Date): string {
  const c = toBeijingComponents(d);
  const hh = String(c.hour).padStart(2, '0');
  const mi = String(c.minute).padStart(2, '0');
  return `${hh}:${mi}`;
}

/** 从日期 + 时间输入（北京时间）构建真实 UTC Date；无效时返回 null。 */
export function parseDateTimeInputs(dateStr: string, timeStr: string): Date | null {
  if (!dateStr || !timeStr) return null;
  const d = new Date(`${dateStr}T${timeStr}:00+08:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 一次性任务：把绝对起始时刻（真实 UTC Date）转为后端可识别的相对 schedule（`in Xs`）。 */
export function buildOnceScheduleFromDatetime(target: Date, now: Date = new Date()): string {
  const seconds = Math.max(1, Math.round((target.getTime() - now.getTime()) / 1000));
  return `in ${seconds}s`;
}

/** 周期任务：把每日触发时刻转为 `每天H点M分`（后端 cron trigger）。 */
export function buildDailyScheduleFromTime(hour: number, minute: number): string {
  const h = Math.min(23, Math.max(0, Math.floor(hour)));
  const m = Math.min(59, Math.max(0, Math.floor(minute)));
  return m > 0 ? `每天${h}点${m}分` : `每天${h}点`;
}

/** 判断任务是否按「每次触发新建会话」投递。 */
function isLocalOrigin(job: CronJob): boolean {
  const d = (job.deliver || '').trim();
  if (d !== 'origin') return false;
  const platform = String((job.origin_source as Record<string, unknown>)?.platform || '').toLowerCase();
  return platform === '' || platform === 'local';
}

function isNewSessionDelivery(job: CronJob): boolean {
  const d = (job.deliver || '').trim();
  return d === '' || d === 'new_session' || isLocalOrigin(job);
}

/** 将任务的 session_id 显示为友好的目标标签。 */
function jobSessionLabel(job: CronJob): string {
  if (isNewSessionDelivery(job)) return '新会话';
  const d = (job.deliver || '').trim();
  if (d === 'origin') return '来源渠道';
  if (!job.session_id) return '—';
  return job.session_id;
}

/** 将 schedule 字符串转为用户可读的摘要。 */
export function scheduleSummaryText(schedule: string): string {
  const s = schedule.trim();
  const lower = s.toLowerCase();
  if (lower.startsWith('every ')) {
    const hit = INTERVAL_PRESETS.find((p) => p.value === lower);
    if (hit) return hit.label;
    return `周期 · ${s.replace(/^every\s+/i, '每 ')}`;
  }
  const onceHit = ONCE_PRESETS.find((p) => p.value === s);
  if (onceHit) return onceHit.label;
  if (s.endsWith('后') || s.endsWith('之后')) return `一次性 · ${s}`;
  if (lower.startsWith('in ')) {
    const raw = s.replace(/^in\s+/i, '').trim();
    const secMatch = /^(\d+)\s*s(?:ec(?:ond)?s?)?$/i.exec(raw);
    if (secMatch) {
      const sec = Number(secMatch[1]);
      if (sec < 60) return `一次性 · ${sec} 秒后`;
      if (sec < 3600) return `一次性 · ${Math.round(sec / 60)} 分钟后`;
      if (sec < 86400) return `一次性 · ${Math.round(sec / 3600)} 小时后`;
      return `一次性 · ${Math.round(sec / 86400)} 天后`;
    }
    return `一次性 · ${raw} 后`;
  }
  if (s.startsWith('每天')) return s;
  return s;
}

/** 判断 schedule 字符串是否表示周期性（重复）调度。 */
export function isRecurringSchedule(schedule: string): boolean {
  const s = schedule.trim();
  const lower = s.toLowerCase();
  if (lower.startsWith('every ')) return true;
  if (s.startsWith('每天') || s.startsWith('每周')) return true;
  if (s === '每小时' || s === '每1小时') return true;
  // 每 10 分钟 / 每 30 秒 / 每 2 小时 / 每 3 天 等
  if (/^每\d+\s*(秒钟|秒|分钟|分|小时|时|天)$/.test(s)) return true;
  return false;
}

// ---------------------------------------------------------------------------
// 时间 / 调度 格式化
// ---------------------------------------------------------------------------

export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return s ? `${m} 分 ${s} 秒` : `${m} 分钟`;
  }
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.round((seconds % 86400) / 3600);
  return h ? `${d} 天 ${h} 小时` : `${d} 天`;
}

export function formatTimestamp(ts: number, opts: { future?: boolean } = {}): string {
  if (!ts || ts <= 0) return '—';
  const now = Date.now() / 1000;
  const diff = ts - now;
  const abs = Math.abs(diff);
  let rel: string;
  if (abs < 60) rel = '刚刚';
  else if (abs < 3600) rel = `${Math.round(abs / 60)} 分钟`;
  else if (abs < 86400) rel = `${Math.round(abs / 3600)} 小时`;
  else rel = `${Math.round(abs / 86400)} 天`;
  const prefix = opts.future ? '还有 ' : '';
  const suffix = diff >= 0 ? '后' : '前';
  return diff >= 0 ? `${prefix}${rel}${suffix}` : `${rel}${suffix}`;
}

function formatBjAbsolute(value: string | number | undefined): string {
  if (!value) return '—';
  if (typeof value === 'number') {
    if (value <= 0) return '—';
    const c = toBeijingComponents(new Date(value * 1000));
    const mm = String(c.month).padStart(2, '0');
    const dd = String(c.day).padStart(2, '0');
    const hh = String(c.hour).padStart(2, '0');
    const mi = String(c.minute).padStart(2, '0');
    return `${c.year}-${mm}-${dd} ${hh}:${mi}`;
  }
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/);
  if (m) return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  return String(value);
}

/** 判断任务是否为周期性（重复）任务。 */
export function isRecurringJob(job: CronJob): boolean {
  return job.kind === 'interval' || job.kind === 'cron';
}

/** Compute KPI stats from job list (master backend no longer exposes /api/cron/stats). */
function computeStats(jobs: CronJob[]): { total: number; enabled: number; disabled: number; interval: number; once: number; failed_recent: number; upcoming_60s: number } {
  const now = Date.now() / 1000;
  let enabled = 0, interval = 0, once = 0, failed_recent = 0, upcoming_60s = 0;
  for (const j of jobs) {
    if (j.enabled) {
      enabled++;
      if (j.next_run_at > 0 && j.next_run_at - now < 60) upcoming_60s++;
    }
    if (isRecurringJob(j)) interval++;
    else if (j.kind === 'once') once++;
    if (j.last_status && j.last_status.startsWith('failed') && j.last_run_at > 0 && now - j.last_run_at < 86400) {
      failed_recent++;
    }
  }
  return { total: jobs.length, enabled, disabled: jobs.length - enabled, interval, once, failed_recent, upcoming_60s };
}

function statusLabel(job: CronJob): { text: string; tone: string } {
  if (!job.enabled) return { text: '已停用', tone: 'is-paused' };
  if (job.last_status.startsWith('failed:')) return { text: '上次失败', tone: 'is-failed' };
  if (job.last_status === 'completed') return { text: '上次成功', tone: 'is-done' };
  if (job.last_status === 'cancelled') return { text: '已取消', tone: 'is-paused' };
  if (job.last_status === 'running') return { text: '执行中', tone: 'is-running' };
  if (job.last_status) return { text: job.last_status, tone: 'is-info' };
  return { text: '未运行', tone: 'is-idle' };
}

function scheduleLabel(job: CronJob): string {
  if (job.schedule_summary) return job.schedule_summary;
  if (job.kind === 'interval') {
    const secs = (job.trigger_payload?.seconds as number) ?? 0;
    return secs > 0 ? `每 ${formatDuration(secs)}` : '周期任务';
  }
  if (job.kind === 'cron') {
    return job.schedule || '定时触发';
  }
  return job.next_run_at > 0 ? `一次性 · ${formatBjAbsolute(job.next_run_at_bj)}` : '一次性任务';
}

function runStatusLabel(run: CronJobRun): { text: string; tone: string } {
  const st = run.status || '';
  if (st === 'completed') return { text: '成功', tone: 'is-done' };
  if (st === 'running') return { text: '执行中', tone: 'is-running' };
  if (st.startsWith('failed')) return { text: '失败', tone: 'is-failed' };
  if (st === 'cancelled') return { text: '已取消', tone: 'is-paused' };
  return { text: st || '未知', tone: 'is-info' };
}

function renderRunTimeline(): string {
  if (view.detailRunsLoading) {
    return `<div class="cron-runs__loading"><div class="cron-spinner"></div>正在加载执行记录…</div>`;
  }
  if (view.detailRuns.length === 0) {
    return `<div class="cron-runs__empty">尚无执行记录。触发一次后，结果会写入下方绑定的对话会话。</div>`;
  }
  return `<ol class="cron-runs">
    ${view.detailRuns.map((run) => {
      const st = runStatusLabel(run);
      const when = run.started_at > 0 ? formatBjAbsolute(run.started_at_bj) : '—';
      const dur = run.duration_seconds != null ? formatDuration(run.duration_seconds) : (st.tone === 'is-running' ? '进行中' : '—');
      const err = run.error_message?.trim();
      return `<li class="cron-runs__item">
        <div class="cron-runs__item-head">
          <span class="cron-status ${st.tone}">${escapeHtml(st.text)}</span>
          <span class="cron-runs__time">${escapeHtml(when)}</span>
        </div>
        <div class="cron-runs__meta">耗时 ${escapeHtml(dur)}</div>
        ${err ? `<div class="cron-runs__error">${escapeHtml(err)}</div>` : ''}
      </li>`;
    }).join('')}
  </ol>`;
}

function renderTemplateIcon(icon: string): string {
  const icons: Record<string, string> = {
    newspaper: 'icon-file',
    clipboard: 'icon-task',
    checks: 'icon-check',
    alarm: 'process-clock',
  };
  return `<svg class="mw-icon" width="40" height="40" viewBox="0 0 24 24" aria-hidden="true"><use href="#${icons[icon] || icons.alarm}"></use></svg>`;
}

function renderTemplates(layout: 'empty' | 'drawer' = 'empty'): string {
  const cls = layout === 'drawer' ? 'cron-template-grid cron-template-grid--drawer' : 'cron-template-grid';
  return `<div class="${cls}">
    ${CRON_TEMPLATES.map((tpl) => `
      <button type="button" class="cron-template-card" data-template="${tpl.id}">
        <span class="cron-template-card__icon">${renderTemplateIcon(tpl.icon)}</span>
        <span class="cron-template-card__copy">
          <span class="cron-template-card__title">${escapeHtml(tpl.title)}</span>
          <span class="cron-template-card__desc">${escapeHtml(tpl.desc)}</span>
        </span>
      </button>
    `).join('')}
  </div>`;
}

function bindTemplateCards(): void {
  document.querySelectorAll<HTMLElement>('[data-template]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-template');
      if (id) applyTemplate(id);
    });
  });
}

function friendlyCronError(message: string | null): string {
  if (!message) return '暂时无法读取定时任务。';
  if (message.includes('未提供接口') || message.includes('/api/cron')) {
    return '当前后端版本还没有暴露定时任务接口，请重启 gateway 或确认已运行最新后端。';
  }
  if (message.includes('Failed to fetch') || message.includes('NetworkError') || message.includes('未连接')) {
    return '当前未连接服务，连接恢复后会自动读取任务。';
  }
  return message;
}

// ---------------------------------------------------------------------------
// 数据
// ---------------------------------------------------------------------------

async function loadDeliveryTargets(): Promise<void> {
  if (view.deliveryTargetsLoaded) return;
  try {
    const res = await backendApi.cronDeliveryTargets();
    const defaults: CronDeliveryTarget[] = [
      { id: 'new_session', label: '新会话', platform: 'local' },
      { id: 'local', label: '当前会话', platform: 'local' },
    ];
    const extras = (res.targets || []).filter((t) => t.id !== 'new_session' && t.id !== 'local');
    view.deliveryTargets = [...defaults, ...extras];
    view.deliveryTargetsLoaded = true;
  } catch {
    view.deliveryTargets = [
      { id: 'new_session', label: '新会话', platform: 'local' },
      { id: 'local', label: '当前会话', platform: 'local' },
    ];
  }
}

async function loadCron(opts: { silent?: boolean } = {}): Promise<void> {
  if (!state.backendConnected) {
    view.error = '未连接服务，无法读取定时任务';
    view.jobs = [];
    view.loading = false;
    if (view.drawer === 'create') {
      renderListOnly();
    } else {
      renderTasksTab();
    }
    return; /* removed by T22 patch */
  }
  if (loadCronInFlight) return;
  loadCronInFlight = true;
  if (!opts.silent) {
    view.loading = true;
    if (view.drawer === 'create') {
      renderListOnly();
    } else {
      renderTasksTab();
    }
  }
  try {
    const sid = view.scope === 'current' ? (state.activeSessionId || undefined) : undefined;
    const list = await backendApi.cronJobs(sid);
    view.jobs = list.jobs || [];
    view.stats = computeStats(view.jobs);
    view.error = null;
    view.lastLoadedAt = Date.now();
  } catch (err) {
    if (!opts.silent) {
      view.error = (err as Error)?.message || '加载失败';
      view.jobs = [];
    }
  } finally {
    view.loading = false;
    loadCronInFlight = false;
    // 创建侧栏打开时只刷新列表，避免整页重绘导致输入框失焦。
    if (view.drawer === 'create') {
      renderListOnly();
    } else {
      renderTasksTab();
    }
    syncCronPoll();
    if (view.drawer === 'detail' && view.selectedId) {
      void loadJobDetail(view.selectedId, { silent: true });
    }
  }
}

async function loadJobDetail(jobId: string, opts: { silent?: boolean } = {}): Promise<void> {
  if (!opts.silent) {
    view.detailRunsLoading = true;
    view.detailRuns = [];
    renderTasksTab();
  }
  try {
    const detail = await backendApi.cronJobDetail(jobId);
    view.detailRuns = detail.runs || [];
  } catch {
    if (!opts.silent) view.detailRuns = [];
  } finally {
    view.detailRunsLoading = false;
    renderTasksTab();
  }
}

let cronPollTimer: ReturnType<typeof setInterval> | null = null;

function syncCronPoll(): void {
  const hasRunning = view.jobs.some((j) => j.last_status === 'running')
    || view.detailRuns.some((r) => r.status === 'running');
  if (hasRunning && cronPollTimer === null) {
    cronPollTimer = setInterval(() => void loadCron({ silent: true }), 3000);
  } else if (!hasRunning && cronPollTimer !== null) {
    clearInterval(cronPollTimer);
    cronPollTimer = null;
  }
}

export function filteredJobs(): CronJob[] {
  const q = view.search.trim().toLowerCase();
  const sid = state.activeSessionId || '';
  const inScope = (view.scope === 'current' && sid)
    ? (j: CronJob) => j.session_id === sid
    : (_j: CronJob) => true;
  return view.jobs.filter(inScope).filter((j) => {
    if (view.filter === 'enabled' && !j.enabled) return false;
    if (view.filter === 'disabled' && j.enabled) return false;
    if (view.filter === 'interval' && !isRecurringJob(j)) return false;
    if (view.filter === 'once' && isRecurringJob(j)) return false;
    if (q) {
      const hay = `${j.name} ${j.query} ${j.session_id || ''} ${jobSessionLabel(j)} ${j.deliver || ''} ${j.id}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

// ---------------------------------------------------------------------------
// 渲染：KPI / 工具栏 / 列表 / 侧栏
// ---------------------------------------------------------------------------

const KPI_ICONS: Record<'total' | 'enabled' | 'interval' | 'failed', string> = {
  total: 'icon-task',
  enabled: 'status-running',
  interval: 'process-clock',
  failed: 'process-error',
};

function renderKpiIcon(kind: 'total' | 'enabled' | 'interval' | 'failed'): string {
  return `<span class="cron-kpi__icon" aria-hidden="true"><svg class="mw-icon" viewBox="0 0 24 24"><use href="#${KPI_ICONS[kind]}"></use></svg></span>`;
}

function renderKpi(): string {
  const s = view.stats;
  const items: { label: string; value: number; hint: string; tone: string; icon: 'total' | 'enabled' | 'interval' | 'failed' }[] = [
    { label: '总任务', value: s.total, hint: s.enabled ? `${s.enabled} 启用` : '尚未创建', tone: 'is-blue', icon: 'total' },
    { label: '启用中', value: s.enabled, hint: s.disabled ? `${s.disabled} 停用` : '全部启用', tone: 'is-green', icon: 'enabled' },
    { label: '周期任务', value: s.interval, hint: s.upcoming_60s ? `${s.upcoming_60s} 个 60s 内触发` : '—', tone: 'is-amber', icon: 'interval' },
    { label: '24h 失败', value: s.failed_recent, hint: s.failed_recent ? '需关注' : '一切正常', tone: s.failed_recent ? 'is-rose' : 'is-slate', icon: 'failed' },
  ];
  return `<div class="cron-kpi">
    ${items
      .map(
        (it) => `
      <div class="cron-kpi__card ${it.tone}">
        <div class="cron-kpi__body">
          <div class="cron-kpi__label">${escapeHtml(it.label)}</div>
          <div class="cron-kpi__value">${it.value}</div>
          <div class="cron-kpi__hint">${escapeHtml(it.hint)}</div>
        </div>
        ${renderKpiIcon(it.icon)}
      </div>`,
      )
      .join('')}
  </div>`;
}

function renderToolbar(): string {
  const filters: FilterKey[] = ['all', 'enabled', 'disabled', 'interval', 'once'];
  return `<div class="cron-toolbar">
    <div class="cron-toolbar__filters">
      ${filters
        .map(
          (k) => `<button type="button" class="cron-chip${view.filter === k ? ' is-active' : ''}" data-filter="${k}">${FILTER_LABELS[k]}</button>`,
        )
        .join('')}
    </div>
    <div class="cron-toolbar__search">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input type="search" id="cron-search" placeholder="搜索 名称 / 指令 / 会话 / id" value="${escapeHtml(view.search)}" />
    </div>
    <div class="cron-toolbar__actions">
      <button type="button" class="cron-btn" id="cron-refresh-btn" title="刷新">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/></svg>
        刷新
      </button>
      <button type="button" class="cron-btn cron-btn--primary" id="cron-create-btn">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        新建定时任务
      </button>
    </div>
  </div>`;
}

function renderJobRow(job: CronJob): string {
  const st = statusLabel(job);
  const isSelected = job.id === view.selectedId;
  const sched = scheduleLabel(job);
  const nextText = job.enabled
    ? job.next_run_at > 0
      ? formatTimestamp(job.next_run_at, { future: true })
      : '已排队'
    : '已停用';
  const lastText = job.last_run_at > 0
    ? `${formatTimestamp(job.last_run_at)}（${formatBjAbsolute(job.last_run_at_bj)}）`
    : '从未运行';
  return `<tr class="cron-row${isSelected ? ' is-selected' : ''}${job.enabled ? '' : ' is-disabled'}" data-job-id="${escapeHtml(job.id)}">
    <td>
      <div class="cron-row__name">${escapeHtml(job.name || '(未命名)')}</div>
      <div class="cron-row__id">${escapeHtml(job.id)}</div>
    </td>
    <td>
      <span class="cron-tag cron-tag--${job.kind}">${isRecurringJob(job) ? '周期' : '一次性'}</span>
      <span class="cron-row__schedule">${escapeHtml(sched)}</span>
    </td>
    <td>
      <div class="cron-row__query" title="${escapeHtml(job.query)}">${escapeHtml(job.query || '—')}</div>
    </td>
    <td>
      <div class="cron-row__session" title="${escapeHtml(isNewSessionDelivery(job) ? '每次触发新建会话' : job.session_id)}">${escapeHtml(jobSessionLabel(job))}</div>
      ${!isNewSessionDelivery(job) && job.deliver ? `<div class="cron-row__deliver">→ ${escapeHtml(job.deliver)}</div>` : ''}
    </td>
    <td>
      <div class="cron-row__when">${escapeHtml(nextText)}</div>
      <div class="cron-row__when-abs">${formatBjAbsolute(job.next_run_at_bj)}</div>
    </td>
    <td>
      <span class="cron-status ${st.tone}">${escapeHtml(st.text)}</span>
      <div class="cron-row__last">${escapeHtml(lastText)}</div>
    </td>
    <td class="cron-row__actions">
      <button type="button" class="cron-icon-btn" data-view-session="${escapeHtml(job.id)}" title="查看对话结果">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      </button>
      <label class="cron-switch" title="${job.enabled ? '停用' : '启用'}">
        <input type="checkbox" data-toggle="${escapeHtml(job.id)}" ${job.enabled ? 'checked' : ''} />
        <span class="cron-switch__track"></span>
      </label>
      <button type="button" class="cron-icon-btn" data-run="${escapeHtml(job.id)}" title="立即触发">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" stroke="none"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <button type="button" class="cron-icon-btn" data-delete="${escapeHtml(job.id)}" title="删除">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
      </button>
    </td>
  </tr>`;
}

function renderList(): string {
  const rows = filteredJobs();
  if (view.loading) {
    return `<div class="cron-list__empty"><div class="cron-spinner"></div>正在加载定时任务…</div>`;
  }
  if (view.error) {
    return `<div class="cron-list__notice cron-list__notice--warn">
      <div class="cron-list__notice-icon">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>
      </div>
      <div class="cron-list__notice-copy">
        <div class="cron-list__notice-title">定时任务暂不可用</div>
        <div class="cron-list__notice-desc">${escapeHtml(friendlyCronError(view.error))}</div>
      </div>
      <button type="button" class="cron-btn" id="cron-retry-btn">重试</button>
    </div>`;
  }
  if (view.jobs.length === 0) {
    return `<div class="cron-empty">
      <div class="cron-empty__head">
        <div>
          <div class="cron-empty__title">选择一个常用自动化任务</div>
          <div class="cron-empty__desc">模板只会预填名称、调度和指令，创建前仍可自由调整。</div>
        </div>
        <button type="button" class="cron-btn cron-btn--primary" id="cron-empty-create">从空白创建</button>
      </div>
      ${renderTemplates('empty')}
    </div>`;
  }
  if (rows.length === 0) {
    return `<div class="cron-list__empty">
      <div class="cron-list__empty-title">没有匹配的任务</div>
      <div class="cron-list__empty-desc">试试切换筛选或清空搜索。</div>
    </div>`;
  }
  return `<div class="cron-list__scroll">
    <table class="cron-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>设定时长</th>
          <th>指令</th>
          <th>目标会话</th>
          <th>下次触发</th>
          <th>状态</th>
          <th class="cron-table__actions">操作</th>
        </tr>
      </thead>
      <tbody>${rows.map(renderJobRow).join('')}</tbody>
    </table>
  </div>`;
}

/** 仅刷新 cron 列表区，不触碰侧栏 DOM；用于创建侧栏打开时后台轮询刷新，避免输入框失焦。 */
function renderListOnly(): void {
  const list = document.querySelector('.cron-list');
  if (!list) return;
  list.innerHTML = renderList();
  bindListEvents();
  bindTemplateCards();
}

// 侧栏：详情
function renderDetailDrawer(): string {
  const job = view.jobs.find((j) => j.id === view.selectedId);
  if (!job) return '';
  const st = statusLabel(job);
  return `<aside class="cron-drawer" id="cron-drawer">
    <div class="cron-drawer__head">
      <div class="cron-drawer__head-copy">
        <div class="cron-drawer__id">${escapeHtml(job.id)}</div>
        <h2 class="cron-drawer__title">${escapeHtml(job.name || '(未命名)')}</h2>
        <div class="cron-drawer__sub">
          <span class="cron-tag cron-tag--${job.kind}">${isRecurringJob(job) ? '周期' : job.kind === 'once' ? '一次性' : '定时'}</span>
          <span class="cron-status ${st.tone}">${escapeHtml(st.text)}</span>
        </div>
      </div>
      <button type="button" class="cron-icon-btn" id="cron-drawer-close" aria-label="关闭">×</button>
    </div>
    <div class="cron-drawer__body">
      <div class="cron-drawer__callout">
        <div class="cron-drawer__callout-title">执行结果在哪里？</div>
        <div class="cron-drawer__callout-desc">到点后 Agent 会在目标会话里跑一轮对话。点击下方按钮打开该会话，即可在对话区查看完整输出。</div>
        <button type="button" class="cron-btn cron-btn--primary" data-view-session-drawer="${escapeHtml(job.id)}">查看对话结果</button>
      </div>
      <div>
        <div class="cron-drawer__section-title">执行指令</div>
        <pre class="cron-drawer__query">${escapeHtml(job.query || '—')}</pre>
      </div>
      <div>
        <div class="cron-drawer__section-title">设定时长</div>
        <div class="cron-drawer__grid">
          <div class="cron-drawer__cell">
            <div class="cron-drawer__cell-label">类型</div>
            <div class="cron-drawer__cell-value">${isRecurringJob(job) ? '周期重复' : job.kind === 'once' ? '仅执行一次' : '定时触发'}</div>
          </div>
          <div class="cron-drawer__cell">
            <div class="cron-drawer__cell-label">间隔 / 触发</div>
            <div class="cron-drawer__cell-value">${escapeHtml(scheduleLabel(job))}</div>
          </div>
          <div class="cron-drawer__cell">
            <div class="cron-drawer__cell-label">下次触发</div>
            <div class="cron-drawer__cell-value">${job.next_run_at > 0 ? formatBjAbsolute(job.next_run_at_bj) : '—'}<div class="cron-drawer__cell-sub">${job.enabled && job.next_run_at > 0 ? formatTimestamp(job.next_run_at, { future: true }) : '已停用'}</div></div>
          </div>
          <div class="cron-drawer__cell">
            <div class="cron-drawer__cell-label">投递目标</div>
            <div class="cron-drawer__cell-value cron-drawer__cell-value--mono">${escapeHtml(jobSessionLabel(job))}</div>
          </div>
        </div>
      </div>
      <div>
        <div class="cron-drawer__section-title">执行历史</div>
        ${renderRunTimeline()}
      </div>
    </div>
    <div class="cron-drawer__foot">
      <button type="button" class="cron-btn cron-btn--primary" data-view-session-drawer="${escapeHtml(job.id)}">查看对话</button>
      <button type="button" class="cron-btn" data-run-drawer="${escapeHtml(job.id)}">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" stroke="none"><path d="M8 5v14l11-7z"/></svg>
        立即触发
      </button>
      <button type="button" class="cron-btn" data-toggle-drawer="${escapeHtml(job.id)}">${job.enabled ? '停用' : '启用'}</button>
      <button type="button" class="cron-btn cron-btn--danger" data-delete-drawer="${escapeHtml(job.id)}">删除</button>
    </div>
  </aside>
  <div class="cron-drawer-backdrop" id="cron-drawer-backdrop"></div>`;
}

function defaultDatetimeFields(): { date: string; time: string } {
  const d = new Date();
  d.setMinutes(d.getMinutes() + 30);
  d.setSeconds(0, 0);
  return { date: formatDateInputValue(d), time: formatTimeInputValue(d) };
}

function renderAllSchedulePresetChips(activeValue: string): string {
  return [...INTERVAL_PRESETS, ...ONCE_PRESETS].map((p) => {
    const active = p.value.toLowerCase() === activeValue.toLowerCase() ? ' is-active' : '';
    const mode = ONCE_PRESETS.some((o) => o.value === p.value) ? 'once' : 'interval';
    return `<button type="button" class="cron-schedule-chip${active}" data-schedule-value="${escapeHtml(p.value)}" data-schedule-mode="${mode}">${escapeHtml(p.label)}</button>`;
  }).join('');
}

function renderScheduleDatetimePicker(defaults: { date: string; time: string }): string {
  return `<details class="cron-advanced cron-schedule-datetime" id="cron-datetime-details">
        <summary>自定义起始时间</summary>
        <div class="cron-schedule-datetime__body">
          <div class="cron-datetime-quick" role="group" aria-label="快捷日期">
            <button type="button" class="cron-datetime-quick__btn" data-date-preset="30m">30 分钟后</button>
            <button type="button" class="cron-datetime-quick__btn" data-date-preset="1h">1 小时后</button>
            <button type="button" class="cron-datetime-quick__btn" data-date-preset="tomorrow">明天此时</button>
          </div>
          <div class="cron-datetime-fields">
            <div class="cron-datetime-field cron-datetime-field--active" id="cron-datetime-start-wrap">
              <span class="cron-datetime-field__label">开始时间</span>
              <div class="cron-datetime-field__inputs">
                <label class="cron-datetime-input-wrap" title="日期">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                  <input type="date" id="cron-start-date" value="${escapeHtml(defaults.date)}" aria-label="开始日期" />
                </label>
                <label class="cron-datetime-input-wrap" title="时间">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
                  <input type="time" id="cron-start-time" value="${escapeHtml(defaults.time)}" aria-label="开始时间" />
                </label>
              </div>
            </div>
          </div>
          <p class="cron-datetime-hint" id="cron-datetime-hint">一次性任务将在此刻执行一次；周期任务选「每天」时可设定每日触发时刻。</p>
        </div>
      </details>`;
}

// 侧栏：创建
function renderCreateDrawer(): string {
  const defaultSessionId = state.activeSessionId || '';
  const defaultSchedule = 'every 30m';
  const dtDefaults = defaultDatetimeFields();
  const sessionNote = defaultSessionId
    ? `任务归属到当前会话（${defaultSessionId}），用于权限校验与查找`
    : '请先在「对话」页选择或创建一个会话，再创建定时任务';
  return `<aside class="cron-drawer cron-drawer--create" id="cron-drawer">
    <div class="cron-drawer__head">
      <div class="cron-drawer__head-copy">
        <div class="cron-drawer__id">新建</div>
        <h2 class="cron-drawer__title">新建定时任务</h2>
        <div class="cron-drawer__sub">到点自动把指令发给 Agent，输出会出现在绑定的对话会话里。</div>
      </div>
      <button type="button" class="cron-icon-btn" id="cron-drawer-close" aria-label="关闭">×</button>
    </div>
    <form class="cron-form" id="cron-create-form" autocomplete="off">
      <details class="cron-advanced cron-form__templates" id="cron-templates-details">
        <summary>常用模板</summary>
        <div class="cron-form__templates-body">
          <p class="cron-form__templates-hint">点击后自动填充，可继续编辑</p>
          ${renderTemplates('drawer')}
        </div>
      </details>
      <label class="cron-field cron-field--boxed">
        <span class="cron-field__label">任务名称</span>
        <input type="text" name="name" required maxlength="64" placeholder="例：每日站会同步" />
      </label>
      <input type="hidden" name="schedule" id="cron-schedule-value" value="${escapeHtml(defaultSchedule)}" />
      <input type="hidden" name="session_id" value="${escapeHtml(defaultSessionId)}" />
      <section class="cron-schedule-panel cron-field--boxed">
        <div class="cron-schedule-panel__head">
          <span class="cron-field__label">设定时长</span>
          <div class="cron-schedule-mode" role="radiogroup" aria-label="任务类型">
            <label class="cron-schedule-mode__opt"><input type="radio" name="schedule_mode" value="interval" checked /> 周期重复</label>
            <label class="cron-schedule-mode__opt"><input type="radio" name="schedule_mode" value="once" /> 仅执行一次</label>
          </div>
        </div>
        <div class="cron-schedule-presets cron-schedule-presets--single-row" id="cron-schedule-presets">
          ${renderAllSchedulePresetChips(defaultSchedule)}
        </div>
        ${renderScheduleDatetimePicker(dtDefaults)}
        <div class="cron-schedule-summary" id="cron-schedule-summary">当前：${escapeHtml(scheduleSummaryText(defaultSchedule))}</div>
      </section>
      <label class="cron-field cron-field--boxed">
        <span class="cron-field__label">执行指令</span>
        <textarea name="query" required rows="5" placeholder="到点要发给 Agent 的指令，例如：汇总今日待办并提醒重要事项"></textarea>
      </label>
      <label class="cron-field cron-field--boxed">
        <span class="cron-field__label">投递目标</span>
        <select name="deliver" id="cron-deliver-target" class="cron-select">
          ${view.deliveryTargets.map((t) => `<option value="${escapeHtml(t.id)}" data-platform="${escapeHtml(t.platform)}">${escapeHtml(t.label)}</option>`).join('')}
        </select>
        <span class="cron-field__hint">默认“新会话”：每次触发都会新建一个本地会话并通知你</span>
      </label>
      <div class="cron-form__session-note${defaultSessionId ? '' : ' is-warn'}">${escapeHtml(sessionNote)}</div>
      <div class="cron-form__error" id="cron-form-error" hidden></div>
      <div class="cron-form__actions">
        <button type="button" class="cron-btn" id="cron-form-cancel">取消</button>
        <button type="submit" class="cron-btn cron-btn--primary">创建</button>
      </div>
    </form>
  </aside>
  <div class="cron-drawer-backdrop" id="cron-drawer-backdrop"></div>`;
}

function ensureCronPage(root: HTMLElement): NonNullable<typeof cronPageSlots> {
  if (!cronPageSlots) {
    const shell = document.createElement('section');
    const header = document.createElement('header');
    const page = document.createElement('div');
    const kpi = document.createElement('div');
    const toolbar = document.createElement('div');
    const list = document.createElement('div');
    const drawer = document.createElement('div');
    shell.className = 'page-shell page-shell--cron mw-cron-page';
    shell.dataset.template = 'list-management';
    header.className = 'cron-page__head page-header page-header--hub';
    header.innerHTML = `
      <div class="page-header__copy">
        <h1 class="cron-page__title page-header__title">定时任务</h1>
        <p class="cron-page__desc page-header__desc">有没有一种可能， AI可以在指定时间自己干活不用你手动催，并把结果投递到目标会话或渠道。</p>
      </div>`;
    page.className = 'cron-page';
    kpi.className = 'mw-cron-page__kpi';
    toolbar.className = 'mw-cron-page__toolbar';
    list.className = 'cron-list';
    drawer.className = 'mw-cron-page__drawer';
    page.append(kpi, toolbar, list);
    shell.append(header, page);
    cronPageSlots = { shell, kpi, toolbar, list, drawer };
  }
  if (!root.contains(cronPageSlots.shell)) root.replaceChildren(cronPageSlots.shell, cronPageSlots.drawer);
  return cronPageSlots;
}

function renderTasksTab(): void {
  const root = $('#cron-page-root');
  if (!root) return;
  const slots = ensureCronPage(root);
  slots.kpi.innerHTML = renderKpi();
  slots.toolbar.innerHTML = renderToolbar();
  slots.list.innerHTML = renderList();
  slots.drawer.innerHTML = view.drawer === 'create'
    ? renderCreateDrawer()
    : view.drawer === 'detail'
      ? renderDetailDrawer()
      : '';
  bindEvents();
}

// ---------------------------------------------------------------------------
// 事件
// ---------------------------------------------------------------------------

function bindEvents(): void {
  document.querySelectorAll<HTMLElement>('[data-filter]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const k = btn.getAttribute('data-filter') as FilterKey | null;
      if (!k) return; /* removed by T22 patch */
      view.filter = k;
      renderTasksTab();
    });
  });
  document.querySelectorAll<HTMLElement>('[data-schedule-tip]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const fill = btn.getAttribute('data-schedule-tip');
      if (fill) setScheduleFieldValue(fill);
    });
  });

  const search = document.getElementById('cron-search') as HTMLInputElement | null;
  if (search) {
    search.addEventListener('input', () => {
      view.search = search.value;
      // 只刷新列表区，避免整个页面重渲染导致 input 失焦
      const list = document.querySelector('.cron-list');
      if (list) {
        list.innerHTML = renderList();
        bindListEvents();
        bindTemplateCards();
      }
    });
  }

  document.getElementById('cron-refresh-btn')?.addEventListener('click', () => void loadCron());
  document.getElementById('cron-retry-btn')?.addEventListener('click', () => void loadCron());
  document.getElementById('cron-create-btn')?.addEventListener('click', () => {
    view.drawer = 'create';
    void loadDeliveryTargets();
    renderTasksTab();
  });
  document.getElementById('cron-empty-create')?.addEventListener('click', () => {
    view.drawer = 'create';
    void loadDeliveryTargets();
    renderTasksTab();
  });

  bindListEvents();

  // 侧栏关闭
  document.getElementById('cron-drawer-close')?.addEventListener('click', closeDrawer);
  document.getElementById('cron-drawer-backdrop')?.addEventListener('click', closeDrawer);

  // 详情侧栏里的操作
  document.querySelectorAll<HTMLElement>('[data-run-drawer]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-run-drawer');
      if (!id) return; /* removed by T22 patch */
      void triggerJobRun(id).catch((err) => notify('触发失败：' + err.message));
    });
  });
  document.querySelectorAll<HTMLElement>('[data-toggle-drawer]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-toggle-drawer');
      if (!id) return; /* removed by T22 patch */
      const job = view.jobs.find((j) => j.id === id);
      const enabled = !(job?.enabled ?? false);
      void (enabled ? backendApi.resumeCronJob(id) : backendApi.pauseCronJob(id)).then(() => {
        notify(enabled ? '已启用' : '已停用');
        void loadCron();
      }).catch((err) => notify('操作失败：' + err.message));
    });
  });
  document.querySelectorAll<HTMLElement>('[data-delete-drawer]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-delete-drawer');
      if (!id) return; /* removed by T22 patch */
      const job = view.jobs.find((j) => j.id === id);
      const name = job?.name || id;
      if (!confirm(`确定删除定时任务「${name}」吗？`)) return; /* removed by T22 patch */
      void backendApi.deleteCronJob(id).then(() => {
        view.selectedId = null;
        view.drawer = null;
        notify('已删除');
        void loadCron();
      }).catch((err) => notify('删除失败：' + err.message));
    });
  });

  // 创建表单
  bindTemplateCards();
  bindScheduleForm();
  bindCreateForm();
}

/** 一次性事件委托：绑定到 document，所有渲染都通过冒泡触发，避免每次 render 重新绑。 */
let listEventsBound = false;

/**
 * D10: in-flight guard for loadCron(). Rapid cron-tab clicks (the generic nav
 * listener + bindCronTab's own click handler both fire) used to start multiple
 * concurrent loads. True while a load is running.
 */
let loadCronInFlight = false;
function bindListEvents(): void {
  if (listEventsBound) return; /* removed by T22 patch */
  listEventsBound = true;

  // 行点击 → 打开详情
  document.addEventListener('click', (e) => {
    const target = e.target as HTMLElement;
    const row = target.closest('.cron-row') as HTMLElement | null;
    if (!row) return; /* removed by T22 patch */
    if (target.closest('.cron-row__actions')) return; /* removed by T22 patch */
    const id = row.getAttribute('data-job-id');
    if (!id) return; /* removed by T22 patch */
    view.selectedId = id;
    view.drawer = 'detail';
    renderTasksTab();
    void loadJobDetail(id);
  });

  // 启停开关
  document.addEventListener('change', (e) => {
    const target = e.target as HTMLElement;
    if (!(target instanceof HTMLInputElement) || target.getAttribute('data-toggle') === null) return; /* removed by T22 patch */
    const sw = target;
    const id = sw.getAttribute('data-toggle');
    if (!id) return; /* removed by T22 patch */
    const enabled = sw.checked;
    void (async () => {
      try {
        if (enabled) await backendApi.resumeCronJob(id); else await backendApi.pauseCronJob(id);
        notify(enabled ? '已启用' : '已停用');
        await loadCron();
      } catch (err) {
        notify('操作失败：' + (err as Error).message);
        sw.checked = !enabled;
      }
    })();
  });

  // 立即触发
  document.addEventListener('click', (e) => {
    const target = e.target as HTMLElement;
    const btn = target.closest('[data-run]') as HTMLElement | null;
    if (!btn) return; /* removed by T22 patch */
    e.stopPropagation();
    const id = btn.getAttribute('data-run');
    if (!id) return; /* removed by T22 patch */
    void (async () => {
      try {
        await triggerJobRun(id);
      } catch (err) {
        notify('触发失败：' + (err as Error).message);
      }
    })();
  });

  // 查看对话结果
  document.addEventListener('click', (e) => {
    const target = e.target as HTMLElement;
    const btn = target.closest('[data-view-session], [data-view-session-drawer]') as HTMLElement | null;
    if (!btn) return;
    e.stopPropagation();
    const id = btn.getAttribute('data-view-session') || btn.getAttribute('data-view-session-drawer');
    if (!id) return;
    const job = view.jobs.find((j) => j.id === id);
    if (job) void openJobConversation(job);
  });

  // 删除
  document.addEventListener('click', (e) => {
    const target = e.target as HTMLElement;
    const btn = target.closest('[data-delete]') as HTMLElement | null;
    if (!btn) return; /* removed by T22 patch */
    e.stopPropagation();
    const id = btn.getAttribute('data-delete');
    if (!id) return; /* removed by T22 patch */
    const job = view.jobs.find((j) => j.id === id);
    const name = job?.name || id;
    if (!confirm(`确定删除定时任务「${name}」吗？此操作不可撤销。`)) return; /* removed by T22 patch */
    void (async () => {
      try {
        await backendApi.deleteCronJob(id);
        if (view.selectedId === id) {
          view.selectedId = null;
          view.drawer = null;
        }
        notify('已删除');
        await loadCron();
      } catch (err) {
        notify('删除失败：' + (err as Error).message);
      }
    })();
  });
}

/** 测试钩子：重置委托绑定状态（用于单测）。 */
export function __resetCronListEventsForTest(): void {
  listEventsBound = false;
  cronPageSlots = null;
}

/** 测试钩子：覆盖 view 的可过滤状态（用于 filteredJobs 单测）。 */
export function __setCronViewForTest(patch: { jobs?: CronJob[]; filter?: FilterKey; search?: string }): void {
  if (patch.jobs !== undefined) view.jobs = patch.jobs;
  if (patch.filter !== undefined) view.filter = patch.filter;
  if (patch.search !== undefined) view.search = patch.search;
}

function closeDrawer(): void {
  view.drawer = null;
  const drawer = document.getElementById('cron-drawer');
  const backdrop = document.getElementById('cron-drawer-backdrop');
  drawer?.remove();
  backdrop?.remove();
}

function getScheduleMode(): 'interval' | 'once' {
  const checked = document.querySelector<HTMLInputElement>('input[name="schedule_mode"]:checked');
  return checked?.value === 'once' ? 'once' : 'interval';
}

function updateScheduleSummary(schedule: string): void {
  const hidden = document.getElementById('cron-schedule-value') as HTMLInputElement | null;
  const summary = document.getElementById('cron-schedule-summary');
  if (hidden) hidden.value = schedule;
  if (summary) summary.textContent = `当前：${scheduleSummaryText(schedule)}`;
  document.querySelectorAll<HTMLElement>('.cron-schedule-chip').forEach((chip) => {
    const val = chip.getAttribute('data-schedule-value') || '';
    chip.classList.toggle('is-active', val.toLowerCase() === schedule.toLowerCase());
  });
}

function syncScheduleModePanels(): void {
  const mode = getScheduleMode();
  const hidden = document.getElementById('cron-schedule-value') as HTMLInputElement | null;
  const current = hidden?.value || '';
  const isIntervalSchedule = isRecurringSchedule(current);
  if (mode === 'interval' && !isIntervalSchedule) {
    updateScheduleSummary(INTERVAL_PRESETS[0].value);
  } else if (mode === 'once' && isIntervalSchedule) {
    updateScheduleSummary(ONCE_PRESETS[0].value);
  }
  syncDatetimeHint();
}

function syncDatetimeHint(): void {
  const hint = document.getElementById('cron-datetime-hint');
  if (!hint) return;
  const mode = getScheduleMode();
  hint.textContent = mode === 'once'
    ? '设定具体日期与时间，任务将仅在该时刻执行一次。'
    : '设定每日触发时刻（如 09:00）；或先选上方「每 30 分钟 / 每小时」等间隔预设。';
}

/** 根据 schedule 字符串回填创建表单中的时长控件。 */
export function setScheduleFieldValue(schedule: string): void {
  const trimmed = schedule.trim();
  const mode: 'interval' | 'once' = isRecurringSchedule(trimmed) ? 'interval' : 'once';
  document.querySelectorAll<HTMLInputElement>('input[name="schedule_mode"]').forEach((radio) => {
    radio.checked = radio.value === mode;
  });
  syncScheduleModePanels();
  updateScheduleSummary(trimmed);
}

/** 从自定义日期时间输入解析 schedule；供创建提交与单测使用。 */
export function resolveScheduleFromCustomInputs(
  mode: 'interval' | 'once',
  dateStr: string,
  timeStr: string,
  currentPreset: string,
  now: Date = new Date(),
): { ok: true; schedule: string } | { ok: false; message: string } {
  const target = parseDateTimeInputs(dateStr, timeStr);
  if (!target) return { ok: false, message: '请选择有效的日期和时间' };
  if (target.getTime() <= now.getTime()) return { ok: false, message: '起始时间必须晚于当前时刻' };
  if (mode === 'once') {
    return { ok: true, schedule: buildOnceScheduleFromDatetime(target, now) };
  }
  const preset = currentPreset.trim();
  if (preset.toLowerCase().startsWith('every 1d') || preset.startsWith('每天')) {
    const c = toBeijingComponents(target);
    return { ok: true, schedule: buildDailyScheduleFromTime(c.hour, c.minute) };
  }
  return { ok: false, message: '周期任务请先选择「每天」，或切换到「仅执行一次」' };
}

function isCustomDatetimeOpen(): boolean {
  const el = document.getElementById('cron-datetime-details') as HTMLDetailsElement | null;
  return el?.open ?? false;
}

function syncCustomDatetimeToSchedule(): boolean {
  if (!isCustomDatetimeOpen()) return true;
  const dateStr = (document.getElementById('cron-start-date') as HTMLInputElement | null)?.value || '';
  const timeStr = (document.getElementById('cron-start-time') as HTMLInputElement | null)?.value || '';
  const current = (document.getElementById('cron-schedule-value') as HTMLInputElement | null)?.value || '';
  const resolved = resolveScheduleFromCustomInputs(getScheduleMode(), dateStr, timeStr, current);
  if (!resolved.ok) {
    notify(resolved.message);
    return false;
  }
  updateScheduleSummary(resolved.schedule);
  return true;
}

function applyDatetimePreset(preset: string): void {
  const now = new Date();
  const target = new Date(now);
  if (preset === '30m') target.setMinutes(target.getMinutes() + 30);
  else if (preset === '1h') target.setHours(target.getHours() + 1);
  else if (preset === 'tomorrow') target.setDate(target.getDate() + 1);
  else return;
  target.setSeconds(0, 0);
  const dateInput = document.getElementById('cron-start-date') as HTMLInputElement | null;
  const timeInput = document.getElementById('cron-start-time') as HTMLInputElement | null;
  if (dateInput) dateInput.value = formatDateInputValue(target);
  if (timeInput) timeInput.value = formatTimeInputValue(target);
  if (preset === '30m' || preset === '1h' || preset === 'tomorrow') {
    document.querySelectorAll<HTMLInputElement>('input[name="schedule_mode"]').forEach((radio) => {
      radio.checked = radio.value === 'once';
    });
    syncScheduleModePanels();
  }
  void syncCustomDatetimeToSchedule();
}

function bindScheduleForm(): void {
  document.querySelectorAll<HTMLInputElement>('input[name="schedule_mode"]').forEach((radio) => {
    radio.addEventListener('change', () => syncScheduleModePanels());
  });
  document.querySelectorAll<HTMLElement>('.cron-schedule-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const val = chip.getAttribute('data-schedule-value');
      const chipMode = chip.getAttribute('data-schedule-mode');
      if (chipMode) {
        document.querySelectorAll<HTMLInputElement>('input[name="schedule_mode"]').forEach((radio) => {
          radio.checked = radio.value === chipMode;
        });
      }
      if (val) updateScheduleSummary(val);
      syncDatetimeHint();
    });
  });
  document.querySelectorAll<HTMLElement>('[data-date-preset]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const preset = btn.getAttribute('data-date-preset');
      if (!preset) return;
      applyDatetimePreset(preset);
    });
  });
  document.getElementById('cron-start-date')?.addEventListener('change', () => { void syncCustomDatetimeToSchedule(); });
  document.getElementById('cron-start-time')?.addEventListener('change', () => { void syncCustomDatetimeToSchedule(); });
  document.getElementById('cron-datetime-details')?.addEventListener('toggle', () => {
    if (isCustomDatetimeOpen()) void syncCustomDatetimeToSchedule();
    syncDatetimeHint();
  });
  syncDatetimeHint();
}

async function triggerJobRun(jobId: string): Promise<void> {
  const job = view.jobs.find((j) => j.id === jobId);
  notify('正在执行定时任务…');
  try {
    const result = await backendApi.cronRunNow(jobId);
    await loadCron({ silent: true });
    const refreshed = view.jobs.find((j) => j.id === jobId) || job;
    if (!refreshed) {
      notify('任务已触发');
      return;
    }

    const status = result.job?.last_status || '';
    const failed = status.startsWith('failed');
    if (failed) {
      notify(`任务执行失败：${result.run?.status || status}`);
      return;
    }

    if (isNewSessionDelivery(refreshed)) {
      notify(`任务「${refreshed.name || jobId}」执行完成，已新建会话`);
      // 切到对话页并刷新会话列表，让新会话出现在侧边栏
      setTabFn('chat');
      void import('./chat-controller').then(({ refreshSessions }) => refreshSessions());
    } else {
      notify(`任务「${refreshed.name || jobId}」执行完成，正在打开目标会话…`);
      await openJobConversation(refreshed);
    }
  } catch (err) {
    notify('执行失败：' + (err as Error).message);
  }
}

function applyTemplate(templateId: string): void {
  const tpl = CRON_TEMPLATES.find((item) => item.id === templateId);
  if (!tpl) return; /* removed by T22 patch */
  view.drawer = 'create';
  void loadDeliveryTargets();
  renderTasksTab();
  const form = document.getElementById('cron-create-form') as HTMLFormElement | null;
  if (!form) return; /* removed by T22 patch */
  const name = form.elements.namedItem('name') as HTMLInputElement | null;
  const query = form.elements.namedItem('query') as HTMLTextAreaElement | null;
  if (name) name.value = tpl.title;
  if (query) query.value = tpl.query;
  setScheduleFieldValue(tpl.schedule);
  bindScheduleForm();
  name?.focus();
  name?.select();
}

function bindCreateForm(): void {
  const form = document.getElementById('cron-create-form') as HTMLFormElement | null;
  const cancel = document.getElementById('cron-form-cancel');
  if (cancel) cancel.addEventListener('click', () => closeDrawer());
  if (!form) return; /* removed by T22 patch */
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (isCustomDatetimeOpen() && !syncCustomDatetimeToSchedule()) return;
    const fd = new FormData(form);
    const deliver = String(fd.get('deliver') || 'new_session').trim();
    const payload = {
      name: String(fd.get('name') || '').trim(),
      schedule: String(fd.get('schedule') || '').trim(),
      query: String(fd.get('query') || '').trim(),
      session_id: String(fd.get('session_id') || state.activeSessionId || '').trim(),
      deliver: deliver === 'new_session' ? '' : deliver,
    };
    const errBox = document.getElementById('cron-form-error');
    if (!payload.name || !payload.schedule || !payload.query) {
      if (errBox) {
        errBox.textContent = '请填写任务名称、设定时长和执行指令';
        errBox.hidden = false;
      }
      return; /* removed by T22 patch */
    }
    if (!payload.session_id) {
      if (errBox) {
        errBox.textContent = '请先在「对话」页选择或创建一个会话，再创建定时任务';
        errBox.hidden = false;
      }
      return;
    }
    try {
      const created = await backendApi.createCronJob(payload);
      view.drawer = null;
      notify('已创建：' + (created.name || payload.name));
      await loadCron();
    } catch (err) {
      if (errBox) {
        errBox.textContent = (err as Error).message;
        errBox.hidden = false;
      }
    }
  });
}

// ---------------------------------------------------------------------------
// 兼容旧 API
// ---------------------------------------------------------------------------

/** 聊天侧 task-board 渲染：这里只显示当前 session 关联的 cron 任务。 */
export function renderCronTaskBoard(): void {
  const panel = $('#task-board-panel');
  if (!panel) return; /* removed by T22 patch */
  const show = state.mode === 'team' && state.taskBoardOpen && state.activeTab === 'chat';
  panel.hidden = !show;
  if (!show) return; /* removed by T22 patch */
  const sid = state.activeSessionId || '';
  const mine = view.jobs.filter((j) => !sid || j.session_id === sid);
  panel.innerHTML = `
    <div class="task-board">
      <div class="task-board__head">
        <div class="task-board__title">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>
          </svg>
          <span>本会话定时任务</span>
        </div>
        <button type="button" class="task-board__close" id="task-board-close" aria-label="关闭">×</button>
      </div>
      <div class="task-board__list">
        ${mine.length === 0 ? '<div class="task-board__empty">该会话还没有定时任务。切到「任务」页可创建。</div>' : mine.map((j) => `
          <div class="task-board__item" data-job-id="${escapeHtml(j.id)}">
            <div class="task-board__item-name">${escapeHtml(j.name)}</div>
            <div class="task-board__item-meta">${escapeHtml(scheduleLabel(j))} · ${escapeHtml(statusLabel(j).text)}</div>
          </div>
        `).join('')}
      </div>
    </div>
  `;
  $('#task-board-close')?.addEventListener('click', () => {
    state.taskBoardOpen = false;
    renderCronTaskBoard();
  });
}

export async function refreshCronJobs(): Promise<void> {
  // 同步刷新任务页和 chat 侧 task-board 的数据源
  if (!state.backendConnected) {
    view.jobs = [];
    renderCronTaskBoard();
    return; /* removed by T22 patch */
  }
  try {
    const list = await backendApi.cronJobs();
    view.jobs = list.jobs || [];
  } catch {
    // 静默失败
  }
  renderCronTaskBoard();
}

export function activateCronPage(): void {
  void loadCron();
}

export function onAfterFinal(): void {
  void refreshCronJobs();
  if (view.drawer === 'detail' && view.selectedId) {
    void loadJobDetail(view.selectedId, { silent: true });
  }
  syncCronPoll();
}
