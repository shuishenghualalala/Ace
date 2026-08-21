/**
 * 系统页：资源 + 服务 + 日志。
 *
 * 全部数据来自后端真实接口，不再使用 mock/估算：
 *   GET /api/system/metrics      → 运行时长 / CPU / 内存 / 磁盘 / 网络（psutil）
 *   GET /api/usage               → 累计 token / 会话数（SQL 聚合）
 *   GET /api/runtime/concurrency → 并发上限 / 活跃 / 排队（dispatcher.runtime_status）
 *   GET /api/sessions/status     → 各会话运行状态
 *   GET /api/platforms           → 平台插件运行状态（feishu）
 *   GET /api/system/logs         → 进程内环形缓冲日志（按级别/关键词筛选）
 */

import { backendApi, type LogEntry, type PlatformRow, type SystemMetrics } from '../backend-client';
import { state } from '../state';
import { bindPagination, paginate, renderPagination } from '../pagination';
import { createSystemPageView, type SystemPageView } from './system-page-view';
import { createStatus } from '../components/controls';
import { createIcon, type IconId } from '../components/icon';
import { setRuntimeStyle } from '../components/runtime-style';

// ── 工具函数 ────────────────────────────────────────
function fmtUptime(seconds: number): string {
  const totalMin = Math.floor(seconds / 60);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function fmtBytes(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)} GB`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} MB`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)} KB`;
  return `${n} B`;
}

function setBar(id: string, pct: number, valId: string, valText: string): void {
  const el = document.getElementById(id);
  if (el) setRuntimeStyle(el, 'width', `${pct}%`);
  const v = document.getElementById(valId);
  if (v) v.textContent = valText;
}

// ── 数据缓存 ────────────────────────────────────────
interface UsageData {
  total_tokens?: number;
  sessions?: number;
}
interface ConcurrencyData {
  max_active_runs: number;
  global_active: number;
  global_queued: number;
}
type SessionsStatus = Record<string, string>;

let lastUsage: UsageData = {};
let lastConcurrency: ConcurrencyData = { max_active_runs: 0, global_active: 0, global_queued: 0 };
let lastSessions: SessionsStatus = {};
let lastMetrics: SystemMetrics | null = null;
let lastFetchAt = 0;
let lastError: string | null = null;
let overviewRefreshTimer: number | null = null;
let systemPageView: SystemPageView | null = null;

function ensureSystemPageView(): SystemPageView | null {
  const root = document.getElementById('system-page-root');
  if (!root) return null;
  if (!systemPageView) {
    systemPageView = createSystemPageView({ onRefresh: () => void refreshBackendData() });
  }
  if (!root.contains(systemPageView.element)) root.replaceChildren(systemPageView.element);
  return systemPageView;
}

async function refreshBackendData(): Promise<void> {
  const view = ensureSystemPageView();
  if (!state.backendConnected) {
    lastError = null;
    view?.setStatus('offline', '后端未连接，连接恢复后自动刷新');
    renderOverviewValues();
    return;
  }
  view?.setStatus('loading', '');
  const [usage, conc, sessions, metrics] = await Promise.allSettled([
    backendApi.usage(),
    backendApi.runtimeConcurrency(),
    backendApi.sessionsStatus(),
    backendApi.systemMetrics(),
  ]);
  const failures: string[] = [];
  if (usage.status === 'fulfilled') lastUsage = usage.value;
  else failures.push(`使用统计：${String(usage.reason)}`);
  if (conc.status === 'fulfilled') lastConcurrency = conc.value;
  else failures.push(`并发状态：${String(conc.reason)}`);
  if (sessions.status === 'fulfilled') lastSessions = sessions.value;
  else failures.push(`会话状态：${String(sessions.reason)}`);
  if (metrics.status === 'fulfilled') lastMetrics = metrics.value;
  else failures.push(`资源指标：${String(metrics.reason)}`);
  lastError = failures.length ? failures.join('；') : null;
  lastFetchAt = Date.now();
  renderOverviewValues();
}

// ── 总览渲染 ────────────────────────────────────────
export function renderSystemOverview(): void {
  ensureSystemPageView();
  void refreshBackendData().then(() => renderOverviewValues());
}

function renderOverviewValues(): void {
  const conn = state.backendConnected;
  const view = ensureSystemPageView();

  // ── 4 KPI ──
  const uptimeEl = document.getElementById('sys-kpi-uptime');
  if (uptimeEl) uptimeEl.textContent = conn && lastMetrics ? fmtUptime(lastMetrics.uptime_s) : '—';

  const cpuEl = document.getElementById('sys-kpi-cpu');
  if (cpuEl) {
    cpuEl.textContent = conn ? `${lastConcurrency.global_active} / ${lastConcurrency.max_active_runs}` : '—';
  }

  const memEl = document.getElementById('sys-kpi-memory');
  if (memEl) memEl.textContent = conn ? fmtNum(Number(lastUsage.total_tokens) || 0) : '—';

  const tasksEl = document.getElementById('sys-kpi-tasks');
  if (tasksEl) {
    const sessCount = Number(lastUsage.sessions) || Object.keys(lastSessions).length;
    const running = Object.values(lastSessions).filter((s) => s === 'running' || s === 'queued').length;
    tasksEl.textContent = conn ? `${sessCount} / ${running}` : '—';
  }

  // ── 错误提示行 ──
  const hintEl = document.getElementById('sys-resources-error');
  if (hintEl) {
    if (!conn) {
      hintEl.textContent = '';
      hintEl.hidden = true;
    } else if (lastError) {
      hintEl.textContent = lastError;
      hintEl.hidden = false;
    } else if (lastMetrics?.psutil_unavailable) {
      hintEl.textContent = 'psutil 未安装，仅展示磁盘与运行时长（请 pip install psutil）。';
      hintEl.hidden = false;
    } else {
      hintEl.textContent = '';
      hintEl.hidden = true;
    }
  }

  // ── 资源条：未连接 / 拉取失败时也展示一个灰底占位，避免整块空白 ──
  if (!conn) {
    setBar('sys-bar-cpu', 0, 'sys-bar-cpu-val', '—');
    setBar('sys-bar-mem', 0, 'sys-bar-mem-val', '—');
    setBar('sys-bar-disk', 0, 'sys-bar-disk-val', '—');
    setBar('sys-bar-net', 0, 'sys-bar-net-val', '—');
  } else if (lastMetrics) {
    const cpu = lastMetrics.cpu_percent;
    if (cpu != null) {
      const cpuPct = Math.min(100, Math.round(Number(cpu) || 0));
      const idlePct = Math.max(0, 100 - cpuPct);
      setBar('sys-bar-cpu', cpuPct, 'sys-bar-cpu-val', `使用 ${cpuPct}% · 空闲 ${idlePct}%${lastMetrics.cpu_count ? ` · ${lastMetrics.cpu_count} 核` : ''}`);
    } else {
      setBar('sys-bar-cpu', 0, 'sys-bar-cpu-val', '— (psutil)');
    }

    const mem = lastMetrics.memory;
    if (mem) {
      const memPct = Math.round(mem.percent);
      setBar('sys-bar-mem', memPct, 'sys-bar-mem-val', `${mem.used_gb} / ${mem.total_gb} GB (${memPct}%) · 可用 ${mem.available_gb} GB`);
    } else {
      setBar('sys-bar-mem', 0, 'sys-bar-mem-val', '— (psutil)');
    }

    const disk = lastMetrics.disk;
    if (disk) {
      const dp = Math.round(disk.percent);
      setBar('sys-bar-disk', dp, 'sys-bar-disk-val', `${disk.used_gb} / ${disk.total_gb} GB (${dp}%) · 剩余 ${disk.free_gb} GB`);
    } else {
      setBar('sys-bar-disk', 0, 'sys-bar-disk-val', '—');
    }

    // 网络：psutil 给的是累计字节数，展示为「发送/接收」总量
    const net = lastMetrics.network;
    if (net) {
      // 没有速率概念，按累计取模 1GB 当作条形宽度参考
      const netPct = Math.min(95, Math.round(((net.bytes_sent + net.bytes_recv) % 1_000_000_000) / 1_000_000_000 * 100));
      setBar('sys-bar-net', netPct, 'sys-bar-net-val', `↑${fmtBytes(net.bytes_sent)} ↓${fmtBytes(net.bytes_recv)}`);
    } else {
      setBar('sys-bar-net', 0, 'sys-bar-net-val', '— (psutil)');
    }
  }

  // 进程 RSS（独立行）
  const procEl = document.getElementById('sys-process-meta');
  if (procEl) {
    if (lastMetrics?.process) {
      procEl.textContent = `Crew Gateway 进程 · PID ${lastMetrics.process.pid} · RSS ${lastMetrics.process.rss_mb} MB`;
      procEl.hidden = false;
    } else {
      procEl.textContent = '';
      procEl.hidden = true;
    }
  }

  // 上次刷新时间
  const stampEl = document.getElementById('sys-refresh-stamp');
  if (stampEl) {
    if (lastFetchAt) {
      const dt = new Date(lastFetchAt);
      stampEl.textContent = `更新于 ${dt.toLocaleTimeString('zh-CN', { hour12: false })}`;
    } else if (conn) {
      stampEl.textContent = '加载中…';
    } else {
      stampEl.textContent = '暂无数据';
    }
  }
  if (!conn) view?.setStatus('offline', '后端未连接，连接恢复后自动刷新');
  else if (lastError) view?.setStatus(lastFetchAt ? 'partial' : 'error', lastError);
  else if (lastFetchAt) {
    const updated = new Date(lastFetchAt).toLocaleTimeString('zh-CN', { hour12: false });
    view?.setStatus('ready', `系统状态已更新 · ${updated}`);
  }

  // ── 活跃服务表（来自 /api/platforms） ──
  void renderServicesTable();
}

// ── 服务表：从 /api/platforms 渲染真实平台状态 ────────
async function renderServicesTable(): Promise<void> {
  const container = document.getElementById('sys-services-table');
  const hint = document.getElementById('sys-services-hint');
  if (!container) return;
  if (!state.backendConnected) {
    container.textContent = '暂无服务状态';
    if (hint) hint.textContent = '—';
    return;
  }
  let platforms: PlatformRow[] = [];
  try {
    platforms = await backendApi.platforms();
  } catch {
    platforms = [];
  }
  let onlineCount = 0;
  container.replaceChildren();
  for (const p of platforms) {
    const status = p.error ? 'error' : (p.live_connected || (p.detail as { connected?: boolean } | undefined)?.connected) ? 'online' : 'offline';
    if (status === 'online') onlineCount += 1;
    const row = document.createElement('div');
    const name = document.createElement('span');
    row.className = 'system-page__service';
    name.className = 'system-page__service-name';
    name.textContent = p.label || p.name;
    row.append(
      name,
      createStatus({
        label: status === 'online' ? '在线' : status === 'error' ? '异常' : '已停止',
        tone: status === 'online' ? 'success' : status === 'error' ? 'warning' : 'neutral',
      }),
    );
    container.append(row);
  }
  if (platforms.length === 0) {
    container.textContent = '暂无服务状态';
    if (hint) hint.textContent = '—';
    return;
  }
  if (hint) hint.textContent = `${onlineCount} 在线`;
}

// ── 日志页：真实日志 + 级别/关键词筛选 ────────────────
let logLevel = '';
let logKeyword = '';
let logPage = 1;
let logPageSize = 50;
let logRefreshTimer: number | null = null;
let logAutoRefreshTimer: number | null = null;

function createLogState(
  stateName: 'empty' | 'offline' | 'error',
  iconId: IconId,
  titleText: string,
  descriptionText: string,
): HTMLElement {
  const stateElement = document.createElement('div');
  const icon = document.createElement('span');
  const title = document.createElement('strong');
  const description = document.createElement('p');
  stateElement.className = 'system-logs-empty';
  stateElement.dataset.state = stateName;
  icon.className = 'system-logs-empty__icon';
  icon.append(createIcon(iconId, { size: 24 }));
  title.className = 'system-logs-empty__title';
  title.textContent = titleText;
  description.className = 'system-logs-empty__desc';
  description.textContent = descriptionText;
  stateElement.append(icon, title, description);
  return stateElement;
}

export async function renderSystemLogs(): Promise<void> {
  const list = document.getElementById('sys-logs-list');
  const countEl = document.getElementById('sys-logs-count');
  const pagerHost = document.getElementById('sys-logs-pager');
  if (!list) return;
  if (!state.backendConnected) {
    list.replaceChildren(createLogState(
      'offline',
      'icon-warning',
      '暂无日志',
      '连接恢复后可查看实时日志。',
    ));
    if (countEl) countEl.textContent = '0';
    pagerHost?.replaceChildren();
    return;
  }
  try {
    const data = await backendApi.systemLogs({
      level: logLevel || undefined,
      q: logKeyword || undefined,
      limit: 500,
    });
    const items = data.items;
    if (countEl) countEl.textContent = String(items.length);
    if (items.length === 0) {
      list.replaceChildren(createLogState(
        'empty',
        'icon-file',
        '暂无日志',
        '没有匹配当前筛选条件的日志，或服务尚未产生日志。',
      ));
      pagerHost?.replaceChildren();
      return;
    }
    const pageItems = paginate(items, logPage, logPageSize);
    list.replaceChildren(...pageItems.map(createLogLine));
    if (pagerHost) {
      pagerHost.innerHTML = renderPagination(
        { page: logPage, pageSize: logPageSize, total: items.length },
        { id: 'sys-logs', pageSizeChoices: [20, 50, 100, 200] },
      );
      bindPagination('sys-logs', {
        onPageChange: (page) => {
          logPage = page;
          void renderSystemLogs();
        },
        onPageSizeChange: (size) => {
          logPageSize = size;
          logPage = 1;
          void renderSystemLogs();
        },
      });
    }
  } catch {
    list.replaceChildren(createLogState(
      'error',
      'icon-error',
      '日志拉取失败',
      '请稍后重试。',
    ));
    pagerHost?.replaceChildren();
  }
}

function createLogLine(entry: LogEntry): HTMLElement {
  const time = new Date(entry.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });
  const levelClass = levelToClass(entry.level);
  const row = document.createElement('div');
  const timeElement = document.createElement('span');
  const level = document.createElement('span');
  const logger = document.createElement('span');
  const message = document.createElement('span');
  row.className = `system-log-line system-log-line--${levelClass}`;
  timeElement.className = 'system-log-line__time';
  timeElement.textContent = time;
  level.className = 'system-log-line__level';
  level.textContent = entry.level;
  logger.className = 'system-log-line__logger';
  logger.textContent = entry.name;
  message.className = 'system-log-line__msg';
  message.textContent = entry.message;
  row.append(timeElement, level, logger, message);
  return row;
}

function levelToClass(level: string): string {
  const lv = level.toUpperCase();
  if (lv === 'ERROR' || lv === 'CRITICAL') return 'error';
  if (lv === 'WARNING') return 'warn';
  if (lv === 'DEBUG') return 'debug';
  return 'info';
}

function bindLogsControls(): void {
  const levelSel = document.getElementById('sys-logs-level') as HTMLSelectElement | null;
  const kwInput = document.getElementById('sys-logs-keyword') as HTMLInputElement | null;
  const refreshBtn = document.getElementById('sys-logs-refresh');
  const autoRefreshChk = document.getElementById('sys-logs-auto-refresh') as HTMLInputElement | null;
  levelSel?.addEventListener('change', () => {
    logLevel = levelSel.value;
    logPage = 1;
    void renderSystemLogs();
  });
  kwInput?.addEventListener('input', () => {
    logKeyword = kwInput.value.trim();
    logPage = 1;
    // 防抖：输入停 400ms 再查
    if (logRefreshTimer) window.clearTimeout(logRefreshTimer);
    logRefreshTimer = window.setTimeout(() => void renderSystemLogs(), 400);
  });
  refreshBtn?.addEventListener('click', () => void renderSystemLogs());

  // 自动刷新：3s 拉一次（仅在勾选时）
  const applyAuto = (): void => {
    if (logAutoRefreshTimer != null) {
      window.clearInterval(logAutoRefreshTimer);
      logAutoRefreshTimer = null;
    }
    if (autoRefreshChk?.checked) {
      logAutoRefreshTimer = window.setInterval(() => {
        const logsPane = document.getElementById('settings-pane-sys-logs');
        if (logsPane && !logsPane.hidden) {
          void renderSystemLogs();
        }
      }, 3000);
    }
  };
  autoRefreshChk?.addEventListener('change', applyAuto);
  applyAuto();
}

// ── 绑定 ────────────────────────────────────────────

/**
 * D10: dispose the system-tab refresh interval + the refresh-button listener.
 * Returned by bindSystemTab so the app can clear the 8s interval on teardown
 * (it previously leaked for the lifetime of the renderer).
 */
export function disposeSystemTab(): void {
  if (overviewRefreshTimer != null) {
    window.clearInterval(overviewRefreshTimer);
    overviewRefreshTimer = null;
  }
  if (logRefreshTimer != null) {
    window.clearTimeout(logRefreshTimer);
    logRefreshTimer = null;
  }
  if (logAutoRefreshTimer != null) {
    window.clearInterval(logAutoRefreshTimer);
    logAutoRefreshTimer = null;
  }
}

export function bindSystemTab(): () => void {
  bindLogsControls();
  // 每 8s 刷新总览指标（避免重复创建：setInterval 多次调用 → 多次刷新）
  if (overviewRefreshTimer != null) window.clearInterval(overviewRefreshTimer);
  overviewRefreshTimer = window.setInterval(() => {
    void refreshBackendData();
  }, 8000);

  // D10: return a disposer so the caller can tear the interval + listener down.
  return () => {
    disposeSystemTab();
  };
}
