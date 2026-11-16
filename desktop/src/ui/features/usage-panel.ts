/**
 * 系统页 · 使用统计面板（参考 CC Switch）
 *
 * 数据完全来自真实来源：
 *   - 后端 /api/usage → 真实 total_tokens（Hero 主数值）
 *   - 前端 usage-tracker → 本地持久化的每回合记录（所有明细/趋势/Provider/模型）
 *   - 真实 token 数通过 recordTurn() 在 chat 完成时记录（index.ts 内）
 *
 * 当用户还没发过对话时，面板显示空态；之后所有数字都来自本地记录，
 * 关闭重开应用也不会丢（localStorage 持久化）。
 */

// `state` 在模板字符串内被引用 26 次，typescript-eslint parser 偶尔漏检。
// eslint-disable-next-line @typescript-eslint/no-unused-vars
import { $, escapeHtml, notify, state } from '../state';
import { setRuntimeStyle } from '../components/runtime-style';
import {
  type UsageRecord,
  type UsageSummary,
  type TrendPoint,
  clearLocalRecords,
  deleteRecord,
  getLogs,
  getModelOptions,
  getModelStats,
  getProviderOptions,
  getProviderStats,
  getSummary,
  getTrend,
  subscribe as subscribeTracker,
  updateRecord,
} from './usage-tracker';
import {
  bindPagination,
  paginate,
  renderPagination,
} from '../pagination';

// ─────────────────────────── 工具函数 ───────────────────────────

function fmtInt(n: number): string {
  return n.toLocaleString('en-US');
}

/**
 * 中文友好的 token 数量显示：
 *   - ≥ 1 亿 → "X.XX 亿"
 *   - ≥ 1 万  → "X.X 万"
 *   - 否则   → 千分位
 */
function fmtTokensShort(n: number): string {
  if (n >= 100_000_000) return `${(n / 100_000_000).toFixed(2)} 亿`;
  if (n >= 10_000) return `${(n / 10_000).toFixed(1)} 万`;
  return fmtInt(Math.round(n));
}

function fmtClock(d: Date): string {
  const pad = (v: number) => String(v).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours()).padStart(2, '0')}:${pad(d.getMinutes()).padStart(2, '0')}`;
}

function escapeAttr(s: string): string {
  return s.replace(/"/g, '&quot;');
}

type TimePreset = 'today' | '7d' | '30d';
const TIME_PRESETS: { id: TimePreset; label: string; hours: number }[] = [
  { id: 'today', label: '当天', hours: 24 },
  { id: '7d', label: '7 天', hours: 24 * 7 },
  { id: '30d', label: '30 天', hours: 24 * 30 },
];

const REFRESH_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: '关闭' },
  { value: 5000, label: '5s' },
  { value: 10000, label: '10s' },
  { value: 30000, label: '30s' },
  { value: 60000, label: '60s' },
];

// ─────────────────────────── 顶部 KPI ───────────────────────────

function renderHero(summary: UsageSummary): string {
  const empty = summary.totalRequests === 0;
  const mainSrc = (() => {
    if (empty && summary.backendReported) {
      return '<span class="usage-hero__src-tag" title="后端未产生本地记录时显示">· 后端</span>';
    }
    if (empty) {
      return '<span class="usage-hero__src-tag usage-hero__src-tag--warn" title="后端未连接，本地无记录">— 无数据</span>';
    }
    if (summary.hasProviderData) {
      return '<span class="usage-hero__src-tag usage-hero__src-tag--real" title="来自 Provider API 返回的真实 token 计数">· 真实</span>';
    }
    return '<span class="usage-hero__src-tag" title="每回合 (input + output + cache) 累加；与后端 /api/usage 的会话当前上下文总和是不同口径">· 本地累加</span>';
  })();
  const backendRef = summary.backendReported && summary.backendTotalTokens > 0
    ? `<div class="usage-hero__ref" title="后端 /api/usage：所有会话当前上下文大小累加。另一口径，仅作参考。">后端累计 ${fmtInt(summary.backendTotalTokens)} · 与主数值不同口径</div>`
    : '';
  return `
    <div class="usage-hero">
      <div class="usage-hero__main">
        <div class="usage-hero__icon">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 9 14 8 22 21 10 14 10 15 2 13 2"/></svg>
        </div>
        <div class="usage-hero__copy">
          <div class="usage-hero__label">
            真实消耗 Tokens
            ${mainSrc}
          </div>
          <div class="usage-hero__value-row">
            <span class="usage-hero__value">${empty ? '—' : fmtInt(summary.totalTokens)}</span>
            ${empty ? '' : `<span class="usage-hero__approx">≈ ${fmtTokensShort(summary.totalTokens)}</span>`}
          </div>
          ${backendRef}
        </div>
      </div>
      <div class="usage-hero__metrics">
        <div class="usage-hero__metric">
          <span class="usage-hero__metric-label">总请求数</span>
          <span class="usage-hero__metric-value">
            <svg class="mw-icon" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><use href="#process-thinking"></use></svg>
            ${empty ? '—' : fmtInt(summary.totalRequests)}
          </span>
        </div>
        <div class="usage-hero__divider"></div>
        <div class="usage-hero__metric">
          <span class="usage-hero__metric-label">成功率</span>
          <span class="usage-hero__metric-value">${empty ? '—' : `${(summary.successRate * 100).toFixed(1)}%`}</span>
        </div>
      </div>
    </div>

    <div class="usage-stats">
      <div class="usage-stat">
        <div class="usage-stat__head">
            <svg class="mw-icon" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><use href="#process-read"></use></svg>
          <span title="本回合 LLM 实际接收的完整上下文（含 system prompt / 历史消息 / 工具结果 / 本轮输入），每次调用都会重发整个上下文">上下文输入</span>
        </div>
        <div class="usage-stat__value">${empty ? '—' : fmtTokensShort(summary.totalInput)}</div>
      </div>
      <div class="usage-stat">
        <div class="usage-stat__head">
            <svg class="mw-icon" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><use href="#process-write"></use></svg>
          <span>Output（估）</span>
        </div>
        <div class="usage-stat__value">${empty ? '—' : fmtTokensShort(summary.totalOutput)}</div>
      </div>
      <div class="usage-stat">
        <div class="usage-stat__head">
            <svg class="mw-icon" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><use href="#icon-folder"></use></svg>
          <span>缓存创建</span>
        </div>
        <div class="usage-stat__value">${summary.totalCacheCreate}</div>
      </div>
      <div class="usage-stat">
        <div class="usage-stat__head">
            <svg class="mw-icon" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><use href="#icon-check"></use></svg>
          <span>缓存命中</span>
        </div>
        <div class="usage-stat__value">${summary.totalCacheRead}</div>
      </div>
      <div class="usage-stat usage-stat--wide">
        <div class="usage-stat__row">
          <span class="usage-stat__head"><span>缓存命中率</span></span>
          <span class="usage-stat__rate">${(summary.hitRate * 100).toFixed(1)}%</span>
        </div>
        <div class="usage-stat__bar"><div class="usage-stat__bar-fill" data-usage-width="${(summary.hitRate * 100).toFixed(1)}"></div></div>
      </div>
    </div>
  `;
}

// ─────────────────────────── 趋势折线图（纯 SVG） ───────────────────────────

function trendSvg(trend: TrendPoint[], hours: number): string {
  if (trend.length === 0 || trend.every((p) => p.input + p.output + p.cacheRead === 0)) {
    return `
      <div class="usage-trend__empty">
        <div class="usage-trend__empty-icon">~</div>
        <div class="usage-trend__empty-title">暂无趋势数据</div>
        <div class="usage-trend__empty-desc">完成几次对话后，这里会显示 ${hours} 小时内的 token 走势。</div>
      </div>
    `;
  }
  const W = 920;
  const H = 280;
  const padL = 56, padR = 24, padT = 16, padB = 32;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const maxTokens = Math.max(1, ...trend.map((p) => p.cacheRead + p.input + p.output)) * 1.05;

  const xAt = (i: number) => padL + (i / Math.max(1, trend.length - 1)) * innerW;
  const yTok = (v: number) => padT + innerH - (v / maxTokens) * innerH;

  // 网格 + Y 轴刻度
  const grid: string[] = [];
  const yLabels: string[] = [];
  for (let k = 0; k <= 4; k++) {
    const y = padT + (innerH * k) / 4;
    grid.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="var(--mw-chart-grid)" stroke-dasharray="3 3"/>`);
    const vTok = (maxTokens * (4 - k)) / 4;
    yLabels.push(`<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--mw-chart-label)">${fmtTokensShort(vTok)}</text>`);
  }

  // X 轴：根据 hours 决定刻度密度（24h：每 4h；7d：每天；30d：每 5 天）
  const xLabels: string[] = [];
  const step = hours <= 24 ? 4 : hours <= 24 * 7 ? 24 : 24 * 5;
  for (let h = 0; h <= hours; h += step) {
    const i = Math.min(trend.length - 1, h);
    const d = new Date(trend[i].ts * 1000);
    const x = padL + (i / Math.max(1, trend.length - 1)) * innerW;
    const label = `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:00`;
    xLabels.push(`<text x="${x.toFixed(1)}" y="${H - padB + 18}" text-anchor="middle" font-size="10" fill="var(--mw-chart-label)">${label}</text>`);
  }

  function pathOf(get: (p: TrendPoint) => number, yFn: (v: number) => number): string {
    if (trend.length === 0) return '';
    const pts = trend.map((p, i) => `${xAt(i).toFixed(1)},${yFn(get(p)).toFixed(1)}`);
    let d = `M ${pts[0]}`;
    for (let i = 1; i < pts.length; i++) {
      const [x1, y1] = pts[i - 1].split(',').map(Number);
      const [x2, y2] = pts[i].split(',').map(Number);
      const cx = (x1 + x2) / 2;
      d += ` Q ${cx.toFixed(1)} ${y1.toFixed(1)} ${cx.toFixed(1)} ${((y1 + y2) / 2).toFixed(1)} T ${x2.toFixed(1)} ${y2.toFixed(1)}`;
    }
    return d;
  }

  function areaOf(get: (p: TrendPoint) => number, yFn: (v: number) => number): string {
    const top = pathOf(get, yFn);
    if (!top) return '';
    const base = ` L ${xAt(trend.length - 1).toFixed(1)} ${(padT + innerH).toFixed(1)} L ${padL} ${(padT + innerH).toFixed(1)} Z`;
    return top + base;
  }

  const series: { id: string; label: string; color: string; get: (p: TrendPoint) => number; yFn: (v: number) => number }[] = [
    { id: 'cacheRead', label: '缓存命中', color: 'var(--mw-chart-3)', get: (p) => p.cacheRead, yFn: yTok },
    { id: 'cacheWrite', label: '缓存创建', color: 'var(--mw-chart-4)', get: (p) => p.cacheWrite, yFn: yTok },
    { id: 'input', label: '输入', color: 'var(--mw-chart-1)', get: (p) => p.input, yFn: yTok },
    { id: 'output', label: '输出', color: 'var(--mw-chart-2)', get: (p) => p.output, yFn: yTok },
  ];

  const paths: string[] = [];
  const lines: string[] = [];
  for (const s of series) {
    const a = areaOf(s.get, s.yFn);
    if (a) paths.push(`<path d="${a}" fill="${s.color}" fill-opacity="0.08" stroke="none"/>`);
    const p = pathOf(s.get, s.yFn);
    if (p) lines.push(`<path d="${p}" fill="none" stroke="${s.color}" stroke-width="2"/>`);
  }

  const hitBands = trend.map((_, i) => {
    const x = xAt(i);
    return `<rect class="usage-trend__hit" data-idx="${i}" x="${(x - 8).toFixed(1)}" y="${padT}" width="16" height="${innerH}" fill="transparent"/>`;
  });

  return `
    <div class="usage-trend__canvas">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="usage-trend__svg" xmlns="http://www.w3.org/2000/svg">
        ${grid.join('')}${yLabels.join('')}${paths.join('')}${lines.join('')}${hitBands.join('')}${xLabels.join('')}
        <line class="usage-trend__crosshair" x1="0" y1="${padT}" x2="0" y2="${padT + innerH}" stroke="var(--mw-action-primary)" stroke-width="1" opacity="0"/>
      </svg>
      <div class="usage-trend__tooltip" hidden></div>
    </div>`;
}

// ─────────────────────────── 子视图 ───────────────────────────

function renderFilters(state: UsagePageState, providers: string[], models: string[]): string {
  const providerOptions = ['<option value="0">全部来源</option>']
    .concat(providers.map((p, i) => `<option value="${i + 1}"${state.providerIdx === i + 1 ? ' selected' : ''}>${escapeHtml(p)}</option>`))
    .join('');
  const modelOptions = ['<option value="0">全部模型</option>']
    .concat(models.map((m, i) => `<option value="${i + 1}"${state.modelIdx === i + 1 ? ' selected' : ''}>${escapeHtml(m)}</option>`))
    .join('');
  return `
    <div class="usage-filters">
      <div class="usage-filter-field">
        <label class="usage-filter-label" for="usage-provider-filter">来源</label>
        <div class="usage-select usage-select--wide">
          <select id="usage-provider-filter" aria-label="筛选来源">${providerOptions}</select>
        </div>
      </div>
      <div class="usage-filter-field">
        <label class="usage-filter-label" for="usage-model-filter">模型</label>
        <div class="usage-select usage-select--wide">
          <select id="usage-model-filter" aria-label="筛选模型">${modelOptions}</select>
        </div>
      </div>
      <div class="usage-filters__spacer"></div>
      <div class="usage-filters__right">
        <div class="usage-select">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
          <select id="usage-refresh" aria-label="刷新间隔">
            ${REFRESH_OPTIONS.map((o) => `<option value="${o.value}"${state.refreshMs === o.value ? ' selected' : ''}>${o.label}</option>`).join('')}
          </select>
        </div>
        <div class="usage-select">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
          <select id="usage-time-preset" aria-label="时间范围">
            ${TIME_PRESETS.map((t) => `<option value="${t.id}"${state.timePreset === t.id ? ' selected' : ''}>${t.label}</option>`).join('')}
          </select>
        </div>
        <button class="usage-filter-btn" id="usage-clear" type="button" title="清空本地统计">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
          清空
        </button>
      </div>
    </div>
  `;
}

function renderLogRow(l: UsageRecord): string {
  const modelCell = l.requestModel && l.requestModel !== l.model
    ? `<span class="usage-log__model">${escapeHtml(l.requestModel)}<span class="usage-log__model-arrow">→</span><span class="usage-log__model-bill">${escapeHtml(l.model)}</span></span>`
    : `<span class="usage-log__model">${escapeHtml(l.model)}</span>`;
  const cacheCell = (l.cacheReadTokens > 0 || l.cacheWriteTokens > 0)
    ? `<div class="usage-log__cache">R${fmtTokensShort(l.cacheReadTokens)}${l.cacheWriteTokens > 0 ? ` · W${fmtTokensShort(l.cacheWriteTokens)}` : ''}</div>`
    : '';
  const ok = l.status >= 200 && l.status < 300;
  const editedBadge = l.edited ? '<span class="usage-log__edited-badge" title="本条 token / cache 已由用户手动修正">已编辑</span>' : '';
  const providerBadge = l.fromProvider
    ? '<span class="usage-log__provider-badge" title="本条 token 数字来自 API 真实返回值">真实</span>'
    : '<span class="usage-log__provider-badge usage-log__provider-badge--est" title="本条 token 数字为字符估算">估算</span>';
  return `
    <tr class="usage-log__row" data-log-id="${escapeAttr(l.id)}" tabindex="0" role="button" title="点击编辑 token / cache 数字">
      <td class="usage-log__cell">${fmtClock(new Date(l.ts * 1000))}</td>
      <td class="usage-log__cell">${escapeHtml(l.provider)}</td>
      <td class="usage-log__cell">${modelCell}${editedBadge}${providerBadge}</td>
      <td class="usage-log__cell">
        <div>${fmtInt(l.inputTokens)}</div>
        ${cacheCell}
      </td>
      <td class="usage-log__cell">${fmtInt(l.outputTokens)}</td>
      <td class="usage-log__cell">
        <div>${(l.durationMs / 1000).toFixed(1)}s</div>
        ${l.firstTokenMs != null ? `<div class="usage-log__ft">/ ${(l.firstTokenMs / 1000).toFixed(1)}s</div>` : ''}
      </td>
      <td class="usage-log__cell">
        <span class="usage-status-pill ${ok ? 'is-ok' : 'is-fail'}">${l.status}</span>
      </td>
      <td class="usage-log__cell usage-log__source" title="${escapeAttr(l.sessionId)}">${escapeHtml(l.source)}</td>
    </tr>
  `;
}

function renderLogsTable(state: UsagePageState, providers: string[], models: string[]): string {
  const filtered = getLogs().filter((l) => {
    if (state.providerIdx > 0 && l.provider !== providers[state.providerIdx - 1]) return false;
    if (state.modelIdx > 0 && l.model !== models[state.modelIdx - 1]) return false;
    if (state.statusFilter === 'ok' && !(l.status >= 200 && l.status < 300)) return false;
    if (state.statusFilter === 'err' && l.status >= 200 && l.status < 300) return false;
    if (state.statusFilter === '429' && l.status !== 429) return false;
    if (state.statusFilter === '5xx' && l.status < 500) return false;
    return true;
  });
  const sorted = filtered.slice().sort((a, b) => b.ts - a.ts);
  if (sorted.length === 0) {
    return `
      <div class="usage-log">
        <div class="usage-log__filters">
          <button class="usage-log__filter${state.statusFilter === '' ? ' is-active' : ''}" data-status="" type="button">全部</button>
          <button class="usage-log__filter${state.statusFilter === 'ok' ? ' is-active' : ''}" data-status="ok" type="button">200</button>
          <button class="usage-log__filter${state.statusFilter === '429' ? ' is-active' : ''}" data-status="429" type="button">429</button>
          <button class="usage-log__filter${state.statusFilter === '5xx' ? ' is-active' : ''}" data-status="5xx" type="button">5xx</button>
          <button class="usage-log__filter${state.statusFilter === 'err' ? ' is-active' : ''}" data-status="err" type="button">异常</button>
          <span class="usage-log__count">共 0 条</span>
        </div>
        <div class="usage-log__table-wrap">
          <div class="usage-log__empty-block">
            <div class="usage-log__empty-icon">~</div>
            <div class="usage-log__empty-title">暂无请求日志</div>
            <div class="usage-log__empty-desc">${getLogs().length === 0 ? '完成几次对话后，这里会自动出现每回合的请求明细。' : '当前筛选条件下没有匹配记录。'}</div>
          </div>
        </div>
      </div>
    `;
  }
  const pageItems = paginate(sorted, state.logsPage, state.logsPageSize);
  const pager = renderPagination(
    { page: state.logsPage, pageSize: state.logsPageSize, total: sorted.length },
    { id: 'usage-logs', pageSizeChoices: [10, 20, 50, 100] },
  );
  return `
    <div class="usage-log">
      <div class="usage-log__filters">
        <button class="usage-log__filter${state.statusFilter === '' ? ' is-active' : ''}" data-status="" type="button">全部</button>
        <button class="usage-log__filter${state.statusFilter === 'ok' ? ' is-active' : ''}" data-status="ok" type="button">200</button>
        <button class="usage-log__filter${state.statusFilter === '429' ? ' is-active' : ''}" data-status="429" type="button">429</button>
        <button class="usage-log__filter${state.statusFilter === '5xx' ? ' is-active' : ''}" data-status="5xx" type="button">5xx</button>
        <button class="usage-log__filter${state.statusFilter === 'err' ? ' is-active' : ''}" data-status="err" type="button">异常</button>
        <span class="usage-log__count">共 ${sorted.length} 条</span>
      </div>
      <div class="usage-log__table-wrap">
        <table class="usage-log__table usage-log__table--requests">
          <colgroup>
            <col class="usage-log__col--time">
            <col class="usage-log__col--provider">
            <col class="usage-log__col--model">
            <col class="usage-log__col--tokens">
            <col class="usage-log__col--tokens">
            <col class="usage-log__col--latency">
            <col class="usage-log__col--status">
            <col class="usage-log__col--session">
          </colgroup>
          <thead>
            <tr>
              <th>时间</th>
              <th>来源</th>
              <th>计费模型</th>
              <th>输入（估）</th>
              <th>输出（估）</th>
              <th>用时 / 首字</th>
              <th>状态</th>
              <th>会话</th>
            </tr>
          </thead>
          <tbody>${pageItems.map(renderLogRow).join('')}</tbody>
        </table>
      </div>
      ${pager}
    </div>
  `;
}

function renderProviderTable(state: UsagePageState, providers: string[]): string {
  const all = getProviderStats().filter((s) => state.providerIdx === 0 || s.provider === providers[state.providerIdx - 1]);
  if (all.length === 0) {
    return `<div class="usage-log__table-wrap"><div class="usage-log__empty-block"><div class="usage-log__empty-icon">~</div><div class="usage-log__empty-title">暂无 Provider 记录</div></div></div>`;
  }
  const pageItems = paginate(all, state.providerPage, state.providerPageSize);
  const pager = renderPagination(
    { page: state.providerPage, pageSize: state.providerPageSize, total: all.length },
    { id: 'usage-provider', pageSizeChoices: [10, 20, 50] },
  );
  return `
    <div class="usage-log__table-wrap">
      <table class="usage-log__table">
        <thead>
          <tr>
            <th>来源</th>
            <th>请求数</th>
            <th>输入（估）</th>
            <th>输出（估）</th>
            <th>缓存命中</th>
            <th>成功率</th>
          </tr>
        </thead>
        <tbody>
          ${pageItems.map((p) => `
            <tr>
              <td>${escapeHtml(p.provider)}</td>
              <td>${fmtInt(p.requests)}</td>
              <td>${fmtTokensShort(p.input)}</td>
              <td>${fmtTokensShort(p.output)}</td>
              <td>${fmtTokensShort(p.cacheRead)}</td>
              <td><span class="usage-status-pill ${p.successRate >= 98 ? 'is-ok' : p.successRate >= 95 ? 'is-warn' : 'is-fail'}">${p.successRate.toFixed(1)}%</span></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
    ${pager}
  `;
}

function renderModelTable(state: UsagePageState, models: string[]): string {
  const all = getModelStats().filter((s) => state.modelIdx === 0 || s.model === models[state.modelIdx - 1]);
  if (all.length === 0) {
    return `<div class="usage-log__table-wrap"><div class="usage-log__empty-block"><div class="usage-log__empty-icon">~</div><div class="usage-log__empty-title">暂无模型记录</div></div></div>`;
  }
  const pageItems = paginate(all, state.modelPage, state.modelPageSize);
  const pager = renderPagination(
    { page: state.modelPage, pageSize: state.modelPageSize, total: all.length },
    { id: 'usage-model', pageSizeChoices: [10, 20, 50] },
  );
  return `
    <div class="usage-log__table-wrap">
      <table class="usage-log__table">
        <thead>
          <tr>
            <th>模型</th>
            <th>请求数</th>
            <th>输入（估）</th>
            <th>输出（估）</th>
            <th>缓存命中</th>
            <th>平均用时</th>
            <th>成功率</th>
          </tr>
        </thead>
        <tbody>
          ${pageItems.map((m) => `
            <tr>
              <td>${escapeHtml(m.model)}</td>
              <td>${fmtInt(m.requests)}</td>
              <td>${fmtTokensShort(m.input)}</td>
              <td>${fmtTokensShort(m.output)}</td>
              <td>${fmtTokensShort(m.cacheRead)}</td>
              <td>${(m.avgDurationMs / 1000).toFixed(1)}s</td>
              <td><span class="usage-status-pill ${m.successRate >= 98 ? 'is-ok' : m.successRate >= 95 ? 'is-warn' : 'is-fail'}">${m.successRate.toFixed(1)}%</span></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
    ${pager}
  `;
}

function renderTabContent(state: UsagePageState, providers: string[], models: string[]): string {
  switch (state.activeTab) {
    case 'logs':     return renderLogsTable(state, providers, models);
    case 'provider': return renderProviderTable(state, providers);
    case 'model':    return renderModelTable(state, models);
  }
}

function renderTabs(state: UsagePageState): string {
  const tabs = [
    { id: 'logs', label: '请求日志' },
    { id: 'provider', label: 'Provider 统计' },
    { id: 'model', label: '模型统计' },
  ];
  return `
    <div class="usage-tabs">
      ${tabs.map((t) => `
        <button class="usage-tab${state.activeTab === t.id ? ' is-active' : ''}" data-usage-tab="${t.id}" type="button">${t.label}</button>
      `).join('')}
    </div>
  `;
}

// ─────────────────────────── 主渲染 ───────────────────────────

interface UsagePageState {
  providerIdx: number;
  modelIdx: number;
  refreshMs: number;
  timePreset: TimePreset;
  statusFilter: '' | 'ok' | 'err' | '429' | '5xx';
  activeTab: 'logs' | 'provider' | 'model';
  logsPage: number;
  logsPageSize: number;
  providerPage: number;
  providerPageSize: number;
  modelPage: number;
  modelPageSize: number;
}

const pageState: UsagePageState = {
  providerIdx: 0,
  modelIdx: 0,
  refreshMs: 0,
  timePreset: 'today',
  statusFilter: '',
  activeTab: 'logs',
  logsPage: 1,
  logsPageSize: 20,
  providerPage: 1,
  providerPageSize: 10,
  modelPage: 1,
  modelPageSize: 10,
};

let refreshTimer: number | null = null;
let unsubscribeTracker: (() => void) | null = null;
let lastTrendSnapshot: TrendPoint[] = [];

function timePresetHours(): number {
  return TIME_PRESETS.find((t) => t.id === pageState.timePreset)?.hours ?? 24;
}

async function render(): Promise<void> {
  const root = $('#usage-page-root');
  if (!root) return;
  const summary = await getSummary();
  const providers = getProviderOptions();
  const models = getModelOptions();
  // 校正 provider/modelIdx 越界（记录被清空后）
  if (pageState.providerIdx > providers.length) pageState.providerIdx = 0;
  if (pageState.modelIdx > models.length) pageState.modelIdx = 0;

  const hours = timePresetHours();
  const trend = getTrend(hours);
  lastTrendSnapshot = trend;
  root.innerHTML = `
    ${renderHero(summary)}

    <section class="usage-trend">
      <header class="usage-trend__head">
        <h3 class="usage-trend__title">使用趋势</h3>
        <span class="usage-trend__range">${TIME_PRESETS.find((t) => t.id === pageState.timePreset)?.label ?? '当天'}</span>
        <div class="usage-trend__legend">
          <span><span class="usage-trend__dot usage-trend__dot--cache-w"></span>缓存创建</span>
          <span><span class="usage-trend__dot usage-trend__dot--cache-r"></span>缓存命中</span>
          <span><span class="usage-trend__dot usage-trend__dot--input"></span>输入</span>
          <span><span class="usage-trend__dot usage-trend__dot--output"></span>输出</span>
        </div>
      </header>
      <div class="usage-trend__chart">${trendSvg(trend, hours)}</div>
    </section>

    ${renderFilters(pageState, providers, models)}

    ${renderTabs(pageState)}

    <div class="usage-tab-content">${renderTabContent(pageState, providers, models)}</div>
  `;
  applyUsageRuntimeStyles(root);
  bindEvents();
}

function applyUsageRuntimeStyles(root: HTMLElement): void {
  root.querySelectorAll<HTMLElement>('[data-usage-width]').forEach((fill) => {
    setRuntimeStyle(fill, 'width', `${fill.dataset.usageWidth ?? '0'}%`);
  });
}

function bindEvents(): void {
  document.getElementById('usage-provider-filter')?.addEventListener('change', (e) => {
    const select = e.target as HTMLSelectElement;
    pageState.providerIdx = Number(select.value) || 0;
    pageState.modelIdx = 0;
    pageState.logsPage = 1;
    pageState.providerPage = 1;
    pageState.modelPage = 1;
    void render();
  });
  document.getElementById('usage-model-filter')?.addEventListener('change', (e) => {
    const select = e.target as HTMLSelectElement;
    pageState.modelIdx = Number(select.value) || 0;
    pageState.logsPage = 1;
    pageState.modelPage = 1;
    rerenderTabOnly();
  });
  const refreshSel = document.getElementById('usage-refresh') as HTMLSelectElement | null;
  refreshSel?.addEventListener('change', () => {
    pageState.refreshMs = Number(refreshSel.value);
    notify(`刷新间隔：${pageState.refreshMs > 0 ? pageState.refreshMs / 1000 + 's' : '关闭'}`);
    installRefreshTimer();
  });
  const timeSel = document.getElementById('usage-time-preset') as HTMLSelectElement | null;
  timeSel?.addEventListener('change', () => {
    pageState.timePreset = (timeSel.value as TimePreset) ?? 'today';
    void render(); // 趋势图随时间范围重建
  });
  document.getElementById('usage-clear')?.addEventListener('click', () => {
    if (!window.confirm('清空本地使用统计？该操作不影响后端数据，仅删除本机缓存的请求记录。')) return;
    clearLocalRecords();
    pageState.providerIdx = 0;
    pageState.modelIdx = 0;
    pageState.statusFilter = '';
    pageState.logsPage = 1;
    pageState.providerPage = 1;
    pageState.modelPage = 1;
    notify('已清空本地使用统计');
    void render();
  });
  document.querySelectorAll<HTMLButtonElement>('[data-usage-tab]').forEach((b) => {
    b.addEventListener('click', () => {
      const t = b.getAttribute('data-usage-tab') as UsagePageState['activeTab'] | null;
      if (!t) return;
      pageState.activeTab = t;
      rerenderTabOnly();
      document.querySelectorAll<HTMLElement>('.usage-tab').forEach((el) => {
        el.classList.toggle('is-active', el.getAttribute('data-usage-tab') === t);
      });
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-status]').forEach((b) => {
    b.addEventListener('click', () => {
      const v = (b.getAttribute('data-status') ?? '') as UsagePageState['statusFilter'];
      pageState.statusFilter = v;
      pageState.logsPage = 1;
      rerenderTabOnly();
      document.querySelectorAll<HTMLElement>('[data-status]').forEach((el) => {
        el.classList.toggle('is-active', el.getAttribute('data-status') === v);
      });
    });
  });
  bindLogRowClicks();
  bindUsagePagers();
  bindTrendHover();
}

/** 使用趋势图悬停显示该时间点各序列数值。 */
function bindTrendHover(): void {
  const canvas = document.querySelector('.usage-trend__canvas');
  if (!canvas) return;
  const svg = canvas.querySelector('svg');
  const tip = canvas.querySelector('.usage-trend__tooltip') as HTMLElement | null;
  const cross = svg?.querySelector('.usage-trend__crosshair') as SVGLineElement | null;
  if (!svg || !tip) return;

  const W = 920;
  const padL = 56;
  const padR = 24;
  const innerW = W - padL - padR;
  const trend = lastTrendSnapshot;
  if (!trend.length) return;

  const hide = () => {
    tip.hidden = true;
    if (cross) cross.setAttribute('opacity', '0');
  };

  const showAt = (idx: number, clientX: number, clientY: number) => {
    const p = trend[idx];
    if (!p) return;
    const d = new Date(p.ts * 1000);
    const label = `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    tip.innerHTML = `
      <div class="usage-trend__tip-time">${label}</div>
      <div class="usage-trend__tip-row"><span>输入</span><strong>${fmtTokensShort(p.input)}</strong></div>
      <div class="usage-trend__tip-row"><span>输出</span><strong>${fmtTokensShort(p.output)}</strong></div>
      <div class="usage-trend__tip-row"><span>缓存命中</span><strong>${fmtTokensShort(p.cacheRead)}</strong></div>
      <div class="usage-trend__tip-row"><span>缓存创建</span><strong>${fmtTokensShort(p.cacheWrite)}</strong></div>
    `;
    tip.hidden = false;
    const rect = canvas.getBoundingClientRect();
    const x = Math.min(Math.max(clientX - rect.left + 12, 8), rect.width - tip.offsetWidth - 8);
    const y = Math.min(Math.max(clientY - rect.top - 12, 8), rect.height - tip.offsetHeight - 8);
    setRuntimeStyle(tip, 'left', `${x}px`);
    setRuntimeStyle(tip, 'top', `${y}px`);
    if (cross) {
      const xSvg = padL + (idx / Math.max(1, trend.length - 1)) * innerW;
      cross.setAttribute('x1', String(xSvg));
      cross.setAttribute('x2', String(xSvg));
      cross.setAttribute('opacity', '0.55');
    }
  };

  canvas.addEventListener('mousemove', (e) => {
    const ev = e as MouseEvent;
    const rect = svg.getBoundingClientRect();
    const relX = ((ev.clientX - rect.left) / rect.width) * W;
    if (relX < padL || relX > W - padR) {
      hide();
      return;
    }
    const ratio = (relX - padL) / innerW;
    const idx = Math.min(trend.length - 1, Math.max(0, Math.round(ratio * (trend.length - 1))));
    showAt(idx, ev.clientX, ev.clientY);
  });
  canvas.addEventListener('mouseleave', hide);
}

function bindUsagePagers(): void {
  bindPagination('usage-logs', {
    onPageChange: (page) => {
      pageState.logsPage = page;
      rerenderTabOnly();
    },
    onPageSizeChange: (size) => {
      pageState.logsPageSize = size;
      pageState.logsPage = 1;
      rerenderTabOnly();
    },
  });
  bindPagination('usage-provider', {
    onPageChange: (page) => {
      pageState.providerPage = page;
      rerenderTabOnly();
    },
    onPageSizeChange: (size) => {
      pageState.providerPageSize = size;
      pageState.providerPage = 1;
      rerenderTabOnly();
    },
  });
  bindPagination('usage-model', {
    onPageChange: (page) => {
      pageState.modelPage = page;
      rerenderTabOnly();
    },
    onPageSizeChange: (size) => {
      pageState.modelPageSize = size;
      pageState.modelPage = 1;
      rerenderTabOnly();
    },
  });
}

// ─────────────────────────── 编辑对话框 ───────────────────────────

const EDIT_MODELS = [
  'minimax-latest', 'claude-sonnet-4-6', 'claude-haiku-4-5', 'claude-opus-4-7',
  'gpt-4o-mini', 'gpt-4o', 'qwen-long',
];

function modelOptionsHtml(current: string): string {
  const set = new Set<string>(EDIT_MODELS);
  for (const m of getModelOptions()) set.add(m);
  return Array.from(set).map((m) =>
    `<option value="${escapeAttr(m)}"${m === current ? ' selected' : ''}>${escapeHtml(m)}</option>`,
  ).join('');
}

function openEditDialog(recordId: string): void {
  const r = getLogs().find((x) => x.id === recordId);
  if (!r) return;

  closeEditDialog();

  const overlay = document.createElement('div');
  overlay.className = 'usage-edit-overlay';
  overlay.id = 'usage-edit-overlay';
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeEditDialog();
  });
  overlay.innerHTML = `
    <div class="usage-edit-dialog" role="dialog" aria-modal="true" aria-labelledby="usage-edit-title">
      <header class="usage-edit__head">
        <div>
          <h3 class="usage-edit__title" id="usage-edit-title">编辑请求记录</h3>
          <div class="usage-edit__subtitle">${fmtClock(new Date(r.ts * 1000))} · ${escapeHtml(r.source)} · ${escapeHtml(r.sessionId)}${r.fromProvider ? ' · <span class="usage-edit__provider-tag" title="本条 token 数字来自 Provider API 真实返回值">来自 Provider</span>' : ' · <span class="usage-edit__provider-tag usage-edit__provider-tag--est" title="本条 token 数字为字符估算">字符估算</span>'}</div>
        </div>
        <button class="usage-edit__close" id="usage-edit-close" type="button" aria-label="关闭">×</button>
      </header>

      <div class="usage-edit__body">
        <div class="usage-edit__row">
          <label class="usage-edit__label" for="usage-edit-model">计费模型</label>
          <select class="usage-edit__select" id="usage-edit-model">${modelOptionsHtml(r.model)}</select>
        </div>

        <div class="usage-edit__grid">
          <div class="usage-edit__field">
            <label class="usage-edit__label" for="usage-edit-input">
              <span class="usage-edit__hint" title="本回合 LLM 接收的完整上下文：系统提示词 + 之前所有 user/assistant/tool 消息 + 本回合输入。每次调用都会重发整个上下文。">输入 Tokens（含上下文）</span>
              <span class="usage-edit__chars" id="usage-edit-input-hint">${fmtInt(r.inputChars)} 字</span>
            </label>
            <input class="usage-edit__input" id="usage-edit-input" type="number" min="0" step="1" value="${r.inputTokens}" />
          </div>

          <div class="usage-edit__field">
            <label class="usage-edit__label" for="usage-edit-output">
              <span class="usage-edit__hint" title="本回合助手回复的字符数（含 thinking + 工具调用 args/results）。">输出 Tokens</span>
              <span class="usage-edit__chars" id="usage-edit-output-hint">${fmtInt(r.outputChars)} 字</span>
            </label>
            <input class="usage-edit__input" id="usage-edit-output" type="number" min="0" step="1" value="${r.outputTokens}" />
          </div>

          <div class="usage-edit__field">
            <label class="usage-edit__label" for="usage-edit-cache-read">
              <span class="usage-edit__hint" title="Anthropic 模型 prompt cache 命中数。OpenAI 协议通常不区分，记 0。">缓存命中 Tokens</span>
              <button class="usage-edit__suggest" id="usage-edit-cache-read-suggest" type="button" title="按 30% 命中率补值">建议</button>
            </label>
            <input class="usage-edit__input" id="usage-edit-cache-read" type="number" min="0" step="1" value="${r.cacheReadTokens}" />
          </div>

          <div class="usage-edit__field">
            <label class="usage-edit__label" for="usage-edit-cache-write">
              <span class="usage-edit__hint" title="Anthropic 模型缓存创建 token 数。一般仅大上下文首次写入时非 0。">缓存创建 Tokens</span>
            </label>
            <input class="usage-edit__input" id="usage-edit-cache-write" type="number" min="0" step="1" value="${r.cacheWriteTokens}" />
          </div>
        </div>

        <div class="usage-edit__summary">
          <div class="usage-edit__summary-row">
            <span>用时</span><strong>${(r.durationMs / 1000).toFixed(1)}s</strong>
          </div>
          <div class="usage-edit__summary-row">
            <span>首字延迟</span><strong>${r.firstTokenMs != null ? `${(r.firstTokenMs / 1000).toFixed(1)}s` : '—'}</strong>
          </div>
          <div class="usage-edit__summary-row">
            <span>状态</span><strong>${r.status}</strong>
          </div>
        </div>
      </div>

      <footer class="usage-edit__foot">
        <button class="usage-edit__btn usage-edit__btn--danger" id="usage-edit-delete" type="button">删除</button>
        <div class="usage-edit__foot-spacer"></div>
        <button class="usage-edit__btn" id="usage-edit-cancel" type="button">取消</button>
        <button class="usage-edit__btn usage-edit__btn--primary" id="usage-edit-save" type="button">保存</button>
      </footer>
    </div>
  `;
  document.body.appendChild(overlay);

  document.getElementById('usage-edit-close')?.addEventListener('click', closeEditDialog);
  document.getElementById('usage-edit-cancel')?.addEventListener('click', closeEditDialog);
  document.addEventListener('keydown', editDialogEscHandler);

  document.getElementById('usage-edit-cache-read-suggest')?.addEventListener('click', () => {
    const inTok = Number((document.getElementById('usage-edit-input') as HTMLInputElement | null)?.value) || 0;
    const cr = document.getElementById('usage-edit-cache-read') as HTMLInputElement | null;
    if (cr) cr.value = String(Math.round(inTok * 0.3));
  });

  document.getElementById('usage-edit-save')?.addEventListener('click', () => {
    const model = (document.getElementById('usage-edit-model') as HTMLSelectElement | null)?.value ?? r.model;
    const inTok = Number((document.getElementById('usage-edit-input') as HTMLInputElement | null)?.value) || 0;
    const outTok = Number((document.getElementById('usage-edit-output') as HTMLInputElement | null)?.value) || 0;
    const cr = Number((document.getElementById('usage-edit-cache-read') as HTMLInputElement | null)?.value) || 0;
    const cw = Number((document.getElementById('usage-edit-cache-write') as HTMLInputElement | null)?.value) || 0;
    updateRecord(r.id, {
      inputTokens: Math.max(0, Math.floor(inTok)),
      outputTokens: Math.max(0, Math.floor(outTok)),
      cacheReadTokens: Math.max(0, Math.floor(cr)),
      cacheWriteTokens: Math.max(0, Math.floor(cw)),
      model,
    });
    notify('已保存到本地使用统计');
    closeEditDialog();
    void render();
  });

  document.getElementById('usage-edit-delete')?.addEventListener('click', () => {
    if (!window.confirm('删除这条请求记录？该操作仅影响本机统计，不影响后端数据。')) return;
    deleteRecord(r.id);
    notify('已删除记录');
    closeEditDialog();
    void render();
  });
}

function editDialogEscHandler(e: KeyboardEvent): void {
  if (e.key === 'Escape') closeEditDialog();
}

function closeEditDialog(): void {
  document.getElementById('usage-edit-overlay')?.remove();
  document.removeEventListener('keydown', editDialogEscHandler);
}

function bindLogRowClicks(): void {
  document.querySelectorAll<HTMLElement>('[data-log-id]').forEach((row) => {
    row.addEventListener('click', () => {
      const id = row.getAttribute('data-log-id');
      if (id) openEditDialog(id);
    });
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        const id = row.getAttribute('data-log-id');
        if (id) openEditDialog(id);
      }
    });
  });
}

function rerenderTabOnly(): void {
  const providers = getProviderOptions();
  const models = getModelOptions();
  const host = document.querySelector('.usage-tab-content');
  if (host) {
    host.innerHTML = renderTabContent(pageState, providers, models);
    applyUsageRuntimeStyles(host as HTMLElement);
  }
  // 重新绑定 status 按钮
  document.querySelectorAll<HTMLButtonElement>('[data-status]').forEach((b) => {
    b.addEventListener('click', () => {
      const v = (b.getAttribute('data-status') ?? '') as UsagePageState['statusFilter'];
      pageState.statusFilter = v;
      pageState.logsPage = 1;
      rerenderTabOnly();
      document.querySelectorAll<HTMLElement>('[data-status]').forEach((el) => {
        el.classList.toggle('is-active', el.getAttribute('data-status') === v);
      });
    });
  });
  // 重新绑定日志行点击（rerender 会替换 innerHTML，原 listener 丢失）
  bindLogRowClicks();
  // 分页条也会被 innerHTML 替换，需要重新绑定
  bindUsagePagers();
}

function installRefreshTimer(): void {
  if (refreshTimer != null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (pageState.refreshMs > 0) {
    refreshTimer = window.setInterval(() => { void render(); }, pageState.refreshMs);
  }
}

export function renderUsagePage(): void {
  void render();
  installRefreshTimer();
}

export function bindUsageTab(): void {
  document.querySelector('[data-settings-pane="sys-usage"]')?.addEventListener('click', () => {
    void render();
  });
}

function isUsagePaneVisible(): boolean {
  const pane = document.getElementById('settings-pane-sys-usage');
  return Boolean(pane && !pane.hidden);
}

export async function initUsagePage(): Promise<void> {
  // 监听 tracker —— 新回合完成后自动刷新（无论面板是否显示）
  if (!unsubscribeTracker) {
    unsubscribeTracker = subscribeTracker(() => {
      // 仅在面板可见时刷新，避免后台空转
      const root = $('#usage-page-root');
      if (root && isUsagePaneVisible()) {
        void render();
      }
    });
  }
  void render();
}
