/** Developer-only trace workbench: bounded list, waterfall, detail and export. */

import { backendApi, type ObservationExportJob } from '../backend-client';
import type {
  ObservationEvent,
  ObservationFilter,
  ObservationPayload,
  ObservationSpan,
  ObservationTrace,
  ObservationTraceBundle,
} from '../../shared/observability';
import { isObservationDevLaunch } from '../../shared/observability';
import { CrewBridge } from '../api/transport';
import type { PageContribution, PageLifecycleContext } from './page-registry';

const PAGE_ID = 'tracing';
const AUTO_REFRESH_BASE_MS = 1_000;
const AUTO_REFRESH_MAX_MS = 10_000;
const TRACE_PAGE_SIZE = 50;
const MAX_TRACE_ITEMS = 500;
const LOG_PAGE_SIZE = 50;

export async function resolveTracingDevLaunch(): Promise<boolean> {
  const getLaunchMode = CrewBridge()?.getLaunchMode;
  if (typeof getLaunchMode !== 'function') return false;
  try {
    return Boolean((await getLaunchMode()).isDevLaunch);
  } catch {
    return false;
  }
}

type DetailTab = 'overview' | 'payloads' | 'events';
type ViewMode = 'traces' | 'logs';

let activeController: AbortController | null = null;
let refreshTimer: number | null = null;
let refreshDelayMs = AUTO_REFRESH_BASE_MS;
let refreshInFlight = false;
let selectedTraceId = '';
let selectedSpanId = '';
let selectedEventSeq = -1;
let activeFilter: ObservationFilter = {};
let nextTraceCursor: string | null = null;
let nextLogCursor: number | null = null;
let traceHasMore = false;
let logHasMore = false;
let traceItems: ObservationTrace[] = [];
let selectedBundle: ObservationTraceBundle | null = null;
let detailTab: DetailTab = 'overview';
let viewMode: ViewMode = 'traces';
let paused = false;
let latestIngestSeq = -1;
let activeExportId = '';
let logItems: ObservationEvent[] = [];
let selectedLog: ObservationEvent | null = null;
let exportFormat: 'json' | 'jsonl' | 'csv' = 'jsonl';
let exportIncludePayloads = false;
let activeExportJob: ObservationExportJob | null = null;
let lastRefreshFailed = false;

function rootElement(): HTMLElement | null { return document.getElementById('tracing-page-root'); }
function currentSignal(): AbortSignal | undefined { return activeController?.signal; }
function aborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
    || error instanceof Error && error.name === 'AbortError';
}

function fmtTime(timestampUs: number | null | undefined): string {
  if (!timestampUs) return '—';
  return new Date(timestampUs / 1_000).toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtDuration(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value < 1 ? `${value.toFixed(2)} ms` : `${value.toFixed(1)} ms`;
}

function recentHourFilter(): ObservationFilter {
  return { start_after_us: Date.now() * 1_000 - 3_600_000_000 };
}

function exportStatusLabel(status: ObservationExportJob['status'], reason = ''): string {
  const labels: Record<ObservationExportJob['status'], string> = {
    running: '生成中',
    completed: '导出完成',
    partial: `部分完成${reason ? `：${reason}` : ''}`,
    cancelled: '已取消',
    failed: '导出失败',
    interrupted: '进程中断，可重新导出',
    expired: '文件已过期，请重新导出',
  };
  return labels[status] || status;
}

function setStatus(message: string, tone: 'ready' | 'loading' | 'error' | 'offline' = 'ready'): void {
  const element = rootElement()?.querySelector<HTMLElement>('[data-tracing-status]');
  if (!element) return;
  element.dataset.state = tone;
  element.textContent = message;
}

function makeButton(label: string, action: string, className = 'tracing-button'): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.dataset.tracingAction = action;
  button.textContent = label;
  return button;
}

function buildPage(): HTMLElement {
  const page = document.createElement('section');
  page.className = 'tracing-page';
  page.dataset.tracingPage = '';

  const header = document.createElement('header');
  header.className = 'tracing-page__header';
  const titleWrap = document.createElement('div');
  const title = document.createElement('h1');
  title.className = 'tracing-page__title';
  title.textContent = '追踪工作台';
  const subtitle = document.createElement('p');
  subtitle.className = 'tracing-page__subtitle';
  subtitle.textContent = '查看请求链、关键边界与脱敏后的输入输出';
  titleWrap.append(title, subtitle);
  const headerActions = document.createElement('div');
  headerActions.className = 'tracing-page__actions';
  headerActions.append(
    makeButton(viewMode === 'traces' ? '系统日志' : '调用追踪', 'toggle-view'),
    makeButton(paused ? '继续刷新' : '暂停刷新', 'toggle-pause'),
    makeButton('刷新', 'refresh'),
    makeButton('导出筛选结果', 'export', 'tracing-button tracing-button--primary'),
  );
  header.append(titleWrap, headerActions);

  const status = document.createElement('div');
  status.className = 'tracing-page__status';
  status.dataset.tracingStatus = '';
  status.textContent = '加载中…';

  const filters = document.createElement('form');
  filters.className = 'tracing-filters';
  filters.dataset.tracingFilters = '';
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = '搜索摘要、模块、请求 ID…';
  search.dataset.tracingFilter = 'q';
  const statusSelect = document.createElement('select');
  statusSelect.dataset.tracingFilter = 'status';
  for (const [value, label] of [['', '全部状态'], ['running', '运行中'], ['succeeded', '成功'], ['failed', '失败'], ['cancelled', '已取消']]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    statusSelect.append(option);
  }
  const moduleInput = document.createElement('input');
  moduleInput.placeholder = '模块';
  moduleInput.dataset.tracingFilter = 'module';
  const componentInput = document.createElement('input');
  componentInput.placeholder = '组件';
  componentInput.dataset.tracingFilter = 'component';
  const levelSelect = document.createElement('select');
  levelSelect.dataset.tracingFilter = 'level';
  for (const [value, label] of [['', '全部日志级别'], ['ERROR', '错误'], ['WARNING', '警告'], ['INFO', '信息'], ['DEBUG', '调试']]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    levelSelect.append(option);
  }
  filters.append(search, statusSelect, moduleInput, componentInput, levelSelect, makeButton('应用筛选', 'filter'));

  const shortcuts = document.createElement('div');
  shortcuts.className = 'tracing-shortcuts';
  for (const [key, label] of [['agent-loop', 'Agent Loop'], ['wiki', 'Wiki'], ['compact', 'Compact']]) {
    const button = makeButton(label, 'shortcut', 'tracing-button tracing-button--quiet');
    button.dataset.tracingShortcut = key;
    shortcuts.append(button);
  }

  const body = document.createElement('div');
  body.className = 'tracing-page__body';
  const list = document.createElement('div');
  list.className = 'tracing-list';
  list.dataset.tracingList = '';
  const detail = document.createElement('div');
  detail.className = 'tracing-detail';
  detail.dataset.tracingDetail = '';
  body.append(list, detail);
  const exportDialog = document.createElement('div');
  exportDialog.className = 'tracing-export-dialog';
  exportDialog.hidden = true;
  exportDialog.dataset.exportDialog = '';
  exportDialog.setAttribute('role', 'dialog');
  exportDialog.setAttribute('aria-modal', 'true');
  const dialogCard = document.createElement('div');
  dialogCard.className = 'tracing-export-dialog__card';
  const dialogTitle = document.createElement('h2');
  dialogTitle.textContent = '导出追踪数据';
  const scope = document.createElement('p');
  scope.dataset.exportScope = '';
  scope.textContent = '当前筛选范围，生成时统计';
  const formatLabel = document.createElement('label');
  formatLabel.textContent = '格式';
  const formatSelect = document.createElement('select');
  formatSelect.dataset.exportFormat = '';
  for (const [value, label] of [['jsonl', 'JSONL（推荐）'], ['json', 'JSON'], ['csv', 'CSV（仅摘要列）']] as const) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    formatSelect.append(option);
  }
  formatLabel.append(formatSelect);
  const payloadLabel = document.createElement('label');
  const payloadToggle = document.createElement('input');
  payloadToggle.type = 'checkbox';
  payloadToggle.dataset.exportPayloads = '';
  payloadLabel.append(payloadToggle, document.createTextNode('包含已脱敏输入 / 输出'));
  const dialogActions = document.createElement('div');
  dialogActions.className = 'tracing-export-dialog__actions';
  dialogActions.append(makeButton('取消', 'export-close'), makeButton('生成导出', 'export-submit', 'tracing-button tracing-button--primary'));
  dialogCard.append(dialogTitle, scope, formatLabel, payloadLabel, dialogActions);
  exportDialog.append(dialogCard);
  page.append(header, status, filters, shortcuts, body, exportDialog);
  return page;
}

function readFilterForm(): ObservationFilter {
  const result: ObservationFilter = {};
  rootElement()?.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[data-tracing-filter]').forEach((field) => {
    const rawKey = field.dataset.tracingFilter || '';
    if (rawKey === 'level') return;
    const key = rawKey as keyof ObservationFilter;
    const value = field.value.trim();
    if (value) result[key] = value as never;
  });
  return result;
}

function setFilterForm(filter: ObservationFilter): void {
  rootElement()?.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[data-tracing-filter]').forEach((field) => {
    const rawKey = field.dataset.tracingFilter || '';
    const key = rawKey as keyof ObservationFilter;
    field.value = rawKey === 'level' ? '' : String(filter[key] ?? '');
  });
}

function readLogLevel(): string {
  return rootElement()?.querySelector<HTMLSelectElement>('[data-tracing-filter="level"]')?.value || '';
}

function logQuery(afterSeq?: number): Parameters<typeof backendApi.tracingLogs>[0] {
  const query: Parameters<typeof backendApi.tracingLogs>[0] = {
    limit: LOG_PAGE_SIZE,
  };
  const level = readLogLevel();
  if (level) query.level = level;
  if (activeFilter.q) query.q = String(activeFilter.q);
  if (activeFilter.module) query.module = String(activeFilter.module);
  if (activeFilter.component) query.component = String(activeFilter.component);
  if (activeFilter.operation) query.operation = String(activeFilter.operation);
  if (activeFilter.source) query.source = String(activeFilter.source);
  if (activeFilter.start_after_us !== undefined) query.start_after_us = activeFilter.start_after_us;
  if (activeFilter.start_before_us !== undefined) query.start_before_us = activeFilter.start_before_us;
  if (afterSeq !== undefined) query.after_seq = afterSeq;
  return query;
}

function renderTraceList(): void {
  const list = rootElement()?.querySelector<HTMLElement>('[data-tracing-list]');
  if (!list) return;
  if (!traceItems.length) {
    list.replaceChildren(emptyState('暂无追踪', '没有匹配当前筛选条件的 trace，或后端尚未采集数据。'));
    return;
  }
  const rows = traceItems.map((trace) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `tracing-trace-row${trace.trace_id === selectedTraceId ? ' is-selected' : ''}`;
    button.dataset.traceId = trace.trace_id;
    const main = document.createElement('span');
    main.className = 'tracing-trace-row__main';
    const name = document.createElement('strong');
    name.textContent = trace.summary || trace.request_id || trace.trace_id.slice(0, 12);
    const meta = document.createElement('span');
    meta.textContent = `${fmtTime(trace.started_at_us)} · ${trace.source || 'backend'} · ${trace.request_id || '无 request'}`;
    main.append(name, meta);
    const side = document.createElement('span');
    side.className = 'tracing-trace-row__side';
    const status = document.createElement('span');
    status.className = `tracing-status tracing-status--${trace.status}`;
    status.textContent = trace.status;
    const duration = document.createElement('span');
    duration.textContent = fmtDuration(trace.server_total_ms);
    side.append(status, duration);
    button.append(main, side);
    return button;
  });
  if (traceHasMore) {
    rows.push(makeButton('加载更早的 trace', 'load-more-traces', 'tracing-button tracing-button--quiet'));
  }
  list.replaceChildren(...rows);
}

function renderLogList(): void {
  const list = rootElement()?.querySelector<HTMLElement>('[data-tracing-list]');
  const detail = rootElement()?.querySelector<HTMLElement>('[data-tracing-detail]');
  if (!list || !detail) return;
  if (!logItems.length) {
    list.replaceChildren(emptyState('暂无系统日志', '没有匹配当前筛选条件的日志。'));
    detail.replaceChildren(emptyState('系统日志', '开发追踪日志与系统日志共用同一时间线。'));
    return;
  }
  const rows = logItems.map((entry) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'tracing-log-row';
    row.dataset.eventId = entry.event_id;
    const title = document.createElement('strong');
    title.textContent = `${entry.level || 'INFO'} · ${entry.name}`;
    const meta = document.createElement('span');
    meta.textContent = `${fmtTime(entry.occurred_at_us)} · ${entry.module || 'system'}${entry.component ? `/${entry.component}` : ''} · ${entry.operation || '—'} · ${entry.source || 'system'}`;
    const message = document.createElement('span');
    message.textContent = entry.message || '—';
    row.append(title, meta, message);
    return row;
  });
  if (logHasMore) rows.push(makeButton('加载更早的日志', 'load-more-logs', 'tracing-button tracing-button--quiet'));
  list.replaceChildren(...rows);
  renderLogDetail(selectedLog);
}

function renderLogDetail(entry: ObservationEvent | null): void {
  const detail = rootElement()?.querySelector<HTMLElement>('[data-tracing-detail]');
  if (!detail) return;
  if (!entry) {
    detail.replaceChildren(emptyState('选择日志', '日志正文使用安全文本节点呈现。'));
    return;
  }
  const card = document.createElement('section');
  card.className = 'tracing-card tracing-log-detail';
  const title = document.createElement('h2');
  title.textContent = `${entry.level || 'INFO'} · ${entry.name}`;
  const meta = document.createElement('p');
  meta.textContent = `${fmtTime(entry.occurred_at_us)} · ${entry.module || 'system'} / ${entry.component || '—'} · ${entry.operation || '—'} · ${entry.source || 'system'}`;
  const message = document.createElement('pre');
  message.className = 'tracing-log-detail__message';
  message.textContent = entry.message || '—';
  const structured = document.createElement('pre');
  structured.className = 'tracing-log-detail__structured';
  structured.textContent = JSON.stringify({
    event_id: entry.event_id,
    trace_id: entry.trace_id || null,
    span_id: entry.span_id || null,
    status: entry.status,
    attributes: entry.attributes || {},
  }, null, 2);
  const actions = document.createElement('div');
  actions.className = 'tracing-log-detail__actions';
  if (entry.trace_id) {
    const jump = makeButton('查看调用链', 'jump-trace', 'tracing-button tracing-button--quiet');
    jump.dataset.traceId = entry.trace_id;
    actions.append(jump);
  } else {
    const noTrace = document.createElement('span');
    noTrace.textContent = '无关联请求';
    actions.append(noTrace);
  }
  card.append(title, meta, message, structured, actions);
  detail.replaceChildren(card);
}

function emptyState(titleText: string, description: string): HTMLElement {
  const element = document.createElement('div');
  element.className = 'tracing-empty';
  const title = document.createElement('strong');
  title.textContent = titleText;
  const copy = document.createElement('p');
  copy.textContent = description;
  element.append(title, copy);
  return element;
}

function renderWaterfall(spans: ObservationSpan[], trace: ObservationTrace): HTMLElement {
  const section = document.createElement('section');
  section.className = 'tracing-card tracing-waterfall';
  const heading = document.createElement('h2');
  heading.textContent = '调用瀑布';
  section.append(heading);
  if (!spans.length) {
    section.append(emptyState('暂无 span', '该 trace 只包含事件或尚未完成写入。'));
    return section;
  }
  const origin = Number(trace.started_at_us || spans[0].started_at_us);
  const total = Math.max(1, Number(trace.ended_at_us || spans[spans.length - 1].ended_at_us || origin) - origin);
  const rows = document.createElement('div');
  rows.className = 'tracing-waterfall__rows';
  for (const spanItem of spans) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = `tracing-waterfall__row${spanItem.span_id === selectedSpanId ? ' is-selected' : ''}`;
    row.dataset.spanId = spanItem.span_id;
    const label = document.createElement('span');
    label.className = 'tracing-waterfall__label';
    label.textContent = `${spanItem.module || 'core'} · ${spanItem.name}`;
    const track = document.createElement('span');
    track.className = 'tracing-waterfall__track';
    const bar = document.createElement('span');
    bar.className = `tracing-waterfall__bar tracing-waterfall__bar--${spanItem.status}`;
    const start = Math.max(0, Number(spanItem.started_at_us) - origin);
    const duration = Math.max(1, Number(spanItem.duration_ms || 0) * 1_000);
    bar.style.left = `${Math.min(99, start / total * 100)}%`;
    bar.style.width = `${Math.max(0.8, Math.min(100, duration / total * 100))}%`;
    bar.title = `${spanItem.name} · ${fmtDuration(spanItem.duration_ms)}`;
    track.append(bar);
    const durationText = document.createElement('span');
    durationText.className = 'tracing-waterfall__duration';
    durationText.textContent = fmtDuration(spanItem.duration_ms);
    row.append(label, track, durationText);
    rows.append(row);
  }
  section.append(rows);
  return section;
}

function payloadIds(bundle: ObservationTraceBundle, spanId = ''): string[] {
  const ids = new Set<string>();
  for (const entry of bundle.events) {
    if (spanId && entry.span_id !== spanId) continue;
    const attributes = entry.attributes || {};
    for (const key of ['payload_id', 'request_payload_id', 'response_payload_id', 'input_payload_id', 'output_payload_id']) {
      const value = attributes[key];
      if (typeof value === 'string' && value) ids.add(value);
    }
    const values = attributes.payload_ids;
    if (Array.isArray(values)) values.forEach((item) => { if (typeof item === 'string' && item) ids.add(item); });
  }
  for (const span of bundle.spans) {
    if (spanId && span.span_id !== spanId) continue;
    const attributes = span.attributes || {};
    for (const key of ['payload_id', 'request_payload_id', 'response_payload_id', 'input_payload_id', 'output_payload_id']) {
      const value = attributes[key];
      if (typeof value === 'string' && value) ids.add(value);
    }
    const values = attributes.payload_ids;
    if (Array.isArray(values)) values.forEach((item) => { if (typeof item === 'string' && item) ids.add(item); });
  }
  return [...ids];
}

function renderEvents(bundle: ObservationTraceBundle): HTMLElement {
  const card = document.createElement('section');
  card.className = 'tracing-card tracing-events';
  const heading = document.createElement('h2');
  heading.textContent = `事件 (${bundle.events.length})`;
  card.append(heading);
  if (!bundle.events.length) {
    card.append(emptyState('暂无事件', '该 trace 尚未写入事件。'));
    return card;
  }
  const eventList = document.createElement('div');
  eventList.className = 'tracing-events__list';
  for (const entry of bundle.events) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'tracing-event-row';
    row.dataset.spanId = entry.span_id;
    const label = document.createElement('strong');
    label.textContent = entry.name;
    const copy = document.createElement('span');
    copy.textContent = `${fmtTime(entry.occurred_at_us)} · ${entry.message || entry.operation || ''}`;
    row.append(label, copy);
    eventList.append(row);
  }
  card.append(eventList);
  return card;
}

function renderPayloads(bundle: ObservationTraceBundle): HTMLElement {
  const card = document.createElement('section');
  card.className = 'tracing-card tracing-payloads';
  const heading = document.createElement('h2');
  heading.textContent = '输入 / 输出';
  card.append(heading);
  const ids = payloadIds(bundle, selectedSpanId);
  if (!ids.length) {
    card.append(emptyState('暂无 payload 引用', '内容按需加载；当前 span 没有可展示的载荷引用。'));
    return card;
  }
  const payloadList = document.createElement('div');
  payloadList.className = 'tracing-payloads__list';
  for (const id of ids) {
    const row = document.createElement('div');
    row.className = 'tracing-payload-row';
    const label = document.createElement('span');
    label.textContent = id;
    const load = makeButton('按需查看', 'payload');
    load.dataset.payloadId = id;
    row.append(label, load);
    payloadList.append(row);
  }
  card.append(payloadList);
  return card;
}

function renderDetail(bundle: ObservationTraceBundle | null): void {
  const detail = rootElement()?.querySelector<HTMLElement>('[data-tracing-detail]');
  if (!detail) return;
  if (!bundle) {
    detail.replaceChildren(emptyState('选择一个 trace', '从左侧列表选择请求以查看瀑布、事件与脱敏 payload。'));
    return;
  }
  const trace = bundle.trace;
  const header = document.createElement('header');
  header.className = 'tracing-detail__header';
  const title = document.createElement('h2');
  title.textContent = trace.summary || trace.trace_id;
  const meta = document.createElement('p');
  meta.textContent = `${trace.trace_id} · ${trace.status} · ${fmtDuration(trace.server_total_ms)}`;
  const headerActions = document.createElement('div');
  headerActions.className = 'tracing-detail__actions';
  headerActions.append(makeButton('导出此 trace', 'export-trace', 'tracing-button tracing-button--quiet'));
  header.append(title, meta);
  header.append(headerActions);
  const tabs = document.createElement('div');
  tabs.className = 'tracing-detail__tabs';
  for (const [tab, label] of [['overview', '概览'], ['payloads', '输入 / 输出'], ['events', '事件']] as const) {
    const button = makeButton(label, 'detail-tab', `tracing-button${detailTab === tab ? ' is-selected' : ''}`);
    button.dataset.detailTab = tab;
    tabs.append(button);
  }
  const content = document.createElement('div');
  content.className = 'tracing-detail__content';
  if (detailTab === 'overview') {
    content.append(renderWaterfall(bundle.spans, trace), renderEvents(bundle));
  } else if (detailTab === 'payloads') {
    content.append(renderWaterfall(bundle.spans, trace), renderPayloads(bundle));
  } else {
    content.append(renderWaterfall(bundle.spans, trace), renderEvents(bundle));
  }
  detail.replaceChildren(header, tabs, content);
}

async function loadPayload(payloadId: string, button: HTMLButtonElement): Promise<void> {
  button.disabled = true;
  try {
    const payload: ObservationPayload = await backendApi.tracingPayload(payloadId, currentSignal());
    const parent = button.parentElement;
    if (!parent) return;
    const pre = document.createElement('pre');
    pre.className = 'tracing-payload-row__content';
    pre.textContent = JSON.stringify(payload, null, 2);
    parent.append(pre);
  } catch (error) {
    if (!aborted(error)) button.textContent = `加载失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    button.disabled = false;
  }
}

function mergeTracePage(items: ObservationTrace[], replaceFirstPage: boolean): void {
  if (replaceFirstPage) {
    const byId = new Map(traceItems.map((item) => [item.trace_id, item]));
    for (const item of items) byId.set(item.trace_id, item);
    traceItems = [...byId.values()]
      .sort((a, b) => Number(b.started_at_us) - Number(a.started_at_us))
      .slice(0, MAX_TRACE_ITEMS);
  } else {
    const known = new Set(traceItems.map((item) => item.trace_id));
    traceItems = [...traceItems, ...items.filter((item) => !known.has(item.trace_id))]
      .slice(0, MAX_TRACE_ITEMS);
  }
}

async function refreshSelectedTrace(): Promise<void> {
  if (!selectedTraceId || !selectedBundle) return;
  const traceId = selectedTraceId;
  const [trace, spans] = await Promise.all([
    backendApi.tracingTrace(traceId, currentSignal()),
    backendApi.tracingSpans(traceId, 200, currentSignal()),
  ]);
  const afterSeq = selectedEventSeq >= 0 ? selectedEventSeq : undefined;
  const events = await backendApi.tracingEvents(traceId, 200, afterSeq, currentSignal());
  const mergedEvents = afterSeq === undefined
    ? events.items
    : [...selectedBundle.events, ...events.items.filter((item) => item.ingest_seq > selectedEventSeq)];
  selectedBundle = { ...trace, spans: spans.items, events: mergedEvents };
  selectedEventSeq = mergedEvents.reduce((max, item) => Math.max(max, Number(item.ingest_seq || -1)), selectedEventSeq);
  renderDetail(selectedBundle);
}

async function loadTraces(force = false): Promise<void> {
  setStatus('加载中…', 'loading');
  try {
    const status = await backendApi.tracingStatus(currentSignal());
    lastRefreshFailed = false;
    const nextSeq = Number(status.latest_ingest_seq ?? -1);
    if (!force && nextSeq >= 0 && nextSeq === latestIngestSeq) {
      await refreshSelectedTrace();
      setStatus(`${traceItems.length} 条 trace · 无新增`, 'ready');
      return;
    }
    latestIngestSeq = nextSeq;
    const page = await backendApi.tracingTraces(activeFilter, TRACE_PAGE_SIZE, null, currentSignal());
    mergeTracePage(page.items, true);
    nextTraceCursor = page.next_cursor || null;
    traceHasMore = Boolean(page.has_more && nextTraceCursor);
    renderTraceList();
    setStatus(`${traceItems.length}${traceHasMore ? '+' : ''} 条 trace`, 'ready');
    if ((!selectedTraceId || !traceItems.some((item) => item.trace_id === selectedTraceId)) && traceItems[0]) {
      await selectTrace(traceItems[0].trace_id);
    } else {
      await refreshSelectedTrace();
    }
  } catch (error) {
    if (aborted(error)) return;
    lastRefreshFailed = true;
    renderTraceList();
    setStatus(error instanceof Error ? error.message : '追踪查询失败', 'error');
  }
}

async function loadMoreTraces(): Promise<void> {
  if (!nextTraceCursor || !traceHasMore) return;
  try {
    const page = await backendApi.tracingTraces(activeFilter, TRACE_PAGE_SIZE, nextTraceCursor, currentSignal());
    mergeTracePage(page.items, false);
    nextTraceCursor = page.next_cursor || null;
    traceHasMore = Boolean(page.has_more && nextTraceCursor);
    renderTraceList();
    setStatus(`${traceItems.length}${traceHasMore ? '+' : ''} 条 trace`, 'ready');
  } catch (error) {
    if (!aborted(error)) setStatus(error instanceof Error ? error.message : '加载更多 trace 失败', 'error');
  }
}

async function loadLogs(force = false): Promise<void> {
  try {
    const status = await backendApi.tracingStatus(currentSignal());
    lastRefreshFailed = false;
    const nextSeq = Number(status.latest_ingest_seq ?? -1);
    if (!force && nextSeq >= 0 && nextSeq === latestIngestSeq) {
      setStatus(`${logItems.length} 条日志 · 无新增`, 'ready');
      return;
    }
    latestIngestSeq = nextSeq;
    const data = await backendApi.tracingLogs(logQuery(), currentSignal());
    logItems = data.items;
    nextLogCursor = data.next_after_seq ?? null;
    logHasMore = Boolean(data.has_more && nextLogCursor !== null);
    selectedLog = selectedLog && logItems.some((item) => item.event_id === selectedLog?.event_id) ? selectedLog : null;
    renderLogList();
    setStatus(`${logItems.length}${logHasMore ? '+' : ''} 条系统日志`, 'ready');
  } catch (error) {
    if (!aborted(error)) {
      lastRefreshFailed = true;
      setStatus(error instanceof Error ? error.message : '系统日志查询失败', 'error');
    }
  }
}

async function loadMoreLogs(): Promise<void> {
  if (nextLogCursor === null || !logHasMore) return;
  try {
    const data = await backendApi.tracingLogs(logQuery(nextLogCursor), currentSignal());
    const known = new Set(logItems.map((item) => item.event_id));
    logItems = [...logItems, ...data.items.filter((item) => !known.has(item.event_id))].slice(0, MAX_TRACE_ITEMS);
    nextLogCursor = data.next_after_seq ?? null;
    logHasMore = Boolean(data.has_more && nextLogCursor !== null);
    renderLogList();
  } catch (error) {
    if (!aborted(error)) setStatus(error instanceof Error ? error.message : '加载更多日志失败', 'error');
  }
}

async function selectTrace(traceId: string): Promise<void> {
  selectedTraceId = traceId;
  selectedSpanId = '';
  selectedEventSeq = -1;
  detailTab = 'overview';
  renderTraceList();
  try {
    selectedBundle = await backendApi.tracingTrace(traceId, currentSignal());
    selectedSpanId = selectedBundle.spans[0]?.span_id || '';
    selectedEventSeq = selectedBundle.events.reduce((max, item) => Math.max(max, Number(item.ingest_seq || -1)), -1);
    renderDetail(selectedBundle);
  } catch (error) {
    if (aborted(error)) return;
    selectedBundle = null;
    renderDetail(null);
    setStatus(error instanceof Error ? error.message : 'trace 详情加载失败', 'error');
  }
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(new DOMException('The operation was aborted.', 'AbortError'));
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      window.clearTimeout(timer);
      reject(new DOMException('The operation was aborted.', 'AbortError'));
    }, { once: true });
  });
}

function showExportDialog(singleTraceId = ''): void {
  const dialog = rootElement()?.querySelector<HTMLElement>('[data-export-dialog]');
  if (!dialog) return;
  const format = dialog.querySelector<HTMLSelectElement>('[data-export-format]');
  const payloads = dialog.querySelector<HTMLInputElement>('[data-export-payloads]');
  exportFormat = singleTraceId ? 'json' : (format?.value as typeof exportFormat) || 'jsonl';
  exportIncludePayloads = Boolean(payloads?.checked);
  if (format) format.value = exportFormat;
  if (payloads) {
    payloads.checked = exportIncludePayloads;
    payloads.disabled = exportFormat === 'csv';
  }
  const scope = dialog.querySelector<HTMLElement>('[data-export-scope]');
  if (scope) scope.textContent = singleTraceId
    ? `单条 trace：${singleTraceId}`
    : `当前筛选范围（${viewMode === 'logs' ? '系统日志' : '交互追踪'}），生成时统计`;
  dialog.dataset.singleTraceId = singleTraceId;
  dialog.hidden = false;
  (format || payloads)?.focus();
}

function closeExportDialog(): void {
  const dialog = rootElement()?.querySelector<HTMLElement>('[data-export-dialog]');
  if (dialog) dialog.hidden = true;
}

function renderExportTask(): void {
  const status = rootElement()?.querySelector<HTMLElement>('[data-tracing-status]');
  if (!status || !activeExportJob) return;
  const label = exportStatusLabel(activeExportJob.status, activeExportJob.partial_reason);
  status.textContent = `${label} · ${activeExportJob.count} 条`;
  status.dataset.state = activeExportJob.status === 'failed' ? 'error' : activeExportJob.status === 'partial' ? 'error' : 'ready';
  if (activeExportJob.status === 'running') {
    const cancel = makeButton('取消导出', 'cancel-export', 'tracing-button tracing-button--quiet');
    status.append(' ', cancel);
  } else if (activeExportJob.status === 'completed' || activeExportJob.status === 'partial') {
    const save = makeButton('保存文件', 'save-export', 'tracing-button tracing-button--quiet');
    save.dataset.exportId = activeExportJob.export_id;
    status.append(' ', save);
  }
}

async function createExport(singleTraceId = ''): Promise<void> {
  try {
    const filter = singleTraceId ? { trace_id: singleTraceId } : activeFilter;
    // The single-trace endpoint has the same durable export schema and is
    // intentionally not downloaded through renderer memory.
    const job = await backendApi.tracingCreateExport({
      format: exportFormat,
      filter,
      include_payloads: exportIncludePayloads && exportFormat !== 'csv',
    }, currentSignal());
    activeExportId = job.export_id;
    activeExportJob = job;
    closeExportDialog();
    renderExportTask();
    await pollExport(job, currentSignal());
  } catch (error) {
    if (!aborted(error)) setStatus(error instanceof Error ? error.message : '导出失败', 'error');
  }
}

async function pollExport(initial: ObservationExportJob, signal?: AbortSignal): Promise<void> {
  let job = initial;
  for (let attempt = 0; attempt < 240 && job.status === 'running'; attempt += 1) {
    await delay(250, signal);
    job = await backendApi.tracingExportStatus(job.export_id, signal);
    activeExportJob = job;
    renderExportTask();
  }
  activeExportJob = job;
  renderExportTask();
  if (!['completed', 'partial'].includes(job.status)) {
    setStatus(job.error || exportStatusLabel(job.status, job.partial_reason), ['failed', 'partial'].includes(job.status) ? 'error' : 'ready');
    return;
  }
  const saveExport = CrewBridge()?.saveTracingExport;
  if (typeof saveExport !== 'function') {
    setStatus('当前环境不支持安全保存追踪导出', 'error');
    return;
  }
  const result = await saveExport(job.export_id);
  if (result.canceled) {
    setStatus('已取消保存，导出任务仍保留至过期', 'ready');
    return;
  }
  setStatus(`${job.status === 'partial' ? '部分完成' : '导出完成'} · ${job.count} 条`, job.status === 'partial' ? 'error' : 'ready');
}

export function mergeTracingShortcutFilter(active: ObservationFilter, key: string): ObservationFilter {
  const moduleFilter = key === 'wiki' ? 'wiki' : 'agent';
  const componentFilter = key === 'agent-loop' ? 'loop' : key === 'compact' ? 'compact' : undefined;
  const nextFilter = { ...active };
  delete nextFilter.module;
  delete nextFilter.component;
  delete nextFilter.operation;
  nextFilter.module = moduleFilter;
  if (componentFilter) nextFilter.component = componentFilter;
  // Compact has several stable operations; module/component is the
  // intentional shortcut so all four variants remain visible.
  return nextFilter;
}

function applyShortcut(key: string): void {
  activeFilter = mergeTracingShortcutFilter(activeFilter, key);
  setFilterForm(activeFilter);
  resetQueryState();
  renderDetail(null);
  void (viewMode === 'logs' ? loadLogs(true) : loadTraces(true));
}

function resetQueryState(): void {
  nextTraceCursor = null;
  nextLogCursor = null;
  traceHasMore = false;
  logHasMore = false;
  selectedTraceId = '';
  selectedSpanId = '';
  selectedEventSeq = -1;
  selectedBundle = null;
  selectedLog = null;
  latestIngestSeq = -1;
  traceItems = [];
  logItems = [];
}

function scheduleRefresh(delayMs = refreshDelayMs): void {
  if (refreshTimer != null) window.clearTimeout(refreshTimer);
  refreshTimer = null;
  if (paused || !activeController) return;
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null;
    if (document.getElementById('tracing-tab')?.classList.contains('active')) {
      void runRefreshCycle();
    } else {
      scheduleRefresh();
    }
  }, delayMs);
}

async function runRefreshCycle(): Promise<void> {
  if (refreshInFlight || !activeController || paused) return;
  refreshInFlight = true;
  try {
    lastRefreshFailed = false;
    if (viewMode === 'logs') await loadLogs();
    else await loadTraces();
  } catch {
    lastRefreshFailed = true;
  } finally {
    refreshInFlight = false;
    refreshDelayMs = lastRefreshFailed
      ? Math.min(AUTO_REFRESH_MAX_MS, Math.max(AUTO_REFRESH_BASE_MS, refreshDelayMs * 2))
      : AUTO_REFRESH_BASE_MS;
    scheduleRefresh();
  }
}

function bindPage(): void {
  const root = rootElement();
  if (!root) return;
  activeController?.abort();
  activeController = new AbortController();
  const { signal } = activeController;
  root.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const eventRow = target?.closest<HTMLElement>('[data-event-id]');
    if (eventRow?.dataset.eventId) {
      selectedLog = logItems.find((item) => item.event_id === eventRow.dataset.eventId) || null;
      renderLogDetail(selectedLog);
      return;
    }
    const jumpTrace = target?.closest<HTMLButtonElement>('[data-tracing-action="jump-trace"]');
    if (jumpTrace?.dataset.traceId) {
      viewMode = 'traces';
      renderPageView();
      void selectTrace(jumpTrace.dataset.traceId);
      return;
    }
    const traceRow = target?.closest<HTMLElement>('[data-trace-id]');
    if (traceRow?.dataset.traceId) {
      void selectTrace(traceRow.dataset.traceId);
      return;
    }
    const spanRow = target?.closest<HTMLElement>('[data-span-id]');
    if (spanRow?.dataset.spanId && selectedBundle) {
      selectedSpanId = spanRow.dataset.spanId;
      if (spanRow.classList.contains('tracing-event-row')) detailTab = 'events';
      renderDetail(selectedBundle);
      return;
    }
    const payloadButton = target?.closest<HTMLButtonElement>('[data-tracing-action="payload"]');
    if (payloadButton?.dataset.payloadId) {
      void loadPayload(payloadButton.dataset.payloadId, payloadButton);
      return;
    }
    const tabButton = target?.closest<HTMLButtonElement>('[data-tracing-action="detail-tab"]');
    if (tabButton?.dataset.detailTab && selectedBundle) {
      detailTab = tabButton.dataset.detailTab as DetailTab;
      renderDetail(selectedBundle);
      return;
    }
    const shortcut = target?.closest<HTMLButtonElement>('[data-tracing-shortcut]')?.dataset.tracingShortcut;
    if (shortcut) {
      applyShortcut(shortcut);
      return;
    }
    const action = target?.closest<HTMLButtonElement>('[data-tracing-action]')?.dataset.tracingAction;
    if (action === 'load-more-traces') {
      void loadMoreTraces();
    } else if (action === 'load-more-logs') {
      void loadMoreLogs();
    } else if (action === 'refresh') {
      latestIngestSeq = -1;
      void (viewMode === 'logs' ? loadLogs(true) : loadTraces(true));
    } else if (action === 'toggle-view') {
      viewMode = viewMode === 'traces' ? 'logs' : 'traces';
      renderPageView();
      void (viewMode === 'logs' ? loadLogs(true) : loadTraces(true));
    } else if (action === 'toggle-pause') {
      paused = !paused;
      renderPageView();
      setStatus(paused ? '显示已暂停，后台仍继续采集' : '已恢复增量刷新', 'ready');
      if (!paused) {
        refreshDelayMs = AUTO_REFRESH_BASE_MS;
        scheduleRefresh();
      }
    } else if (action === 'filter') {
      event.preventDefault();
      activeFilter = readFilterForm();
      resetQueryState();
      renderDetail(null);
      void (viewMode === 'logs' ? loadLogs(true) : loadTraces(true));
    } else if (action === 'export') {
      showExportDialog();
    } else if (action === 'export-trace') {
      if (selectedTraceId) showExportDialog(selectedTraceId);
    } else if (action === 'export-close') {
      closeExportDialog();
    } else if (action === 'export-submit') {
      const dialog = root.querySelector<HTMLElement>('[data-export-dialog]');
      const format = dialog?.querySelector<HTMLSelectElement>('[data-export-format]')?.value;
      exportFormat = format === 'json' || format === 'csv' || format === 'jsonl' ? format : 'jsonl';
      exportIncludePayloads = Boolean(dialog?.querySelector<HTMLInputElement>('[data-export-payloads]')?.checked);
      void createExport(dialog?.dataset.singleTraceId || '');
    } else if (action === 'cancel-export') {
      const exportId = activeExportJob?.export_id || activeExportId;
      if (exportId) {
        void backendApi.tracingCancelExport(exportId, currentSignal()).then(() => {
          activeExportJob = activeExportJob ? { ...activeExportJob, status: 'cancelled', partial: true, partial_reason: 'cancelled' } : null;
          activeExportId = '';
          renderExportTask();
        }).catch((error) => {
          if (!aborted(error)) setStatus(error instanceof Error ? error.message : '取消导出失败', 'error');
        });
      }
    } else if (action === 'save-export') {
      const exportId = target?.closest<HTMLButtonElement>('[data-export-id]')?.dataset.exportId;
      const save = CrewBridge()?.saveTracingExport;
      if (exportId && typeof save === 'function') void save(exportId).then((result) => {
        if (result.canceled) setStatus('已取消保存，导出任务仍保留至过期', 'ready');
      }).catch((error) => setStatus(error instanceof Error ? error.message : '保存导出失败', 'error'));
    }
  }, { signal });
  root.addEventListener('submit', (event) => {
    event.preventDefault();
    activeFilter = readFilterForm();
    resetQueryState();
    renderDetail(null);
    void (viewMode === 'logs' ? loadLogs(true) : loadTraces(true));
  }, { signal });
  root.addEventListener('change', (event) => {
    const target = event.target instanceof HTMLSelectElement ? event.target : null;
    if (target?.dataset.exportFormat) {
      const payloads = root.querySelector<HTMLInputElement>('[data-export-payloads]');
      if (payloads) payloads.disabled = target.value === 'csv';
    }
  }, { signal });
  root.addEventListener('keydown', (event) => {
    const row = event.target instanceof HTMLButtonElement && event.target.dataset.traceId
      ? event.target
      : null;
    if (!row || !['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    const rows = [...root.querySelectorAll<HTMLButtonElement>('[data-trace-id]')];
    const index = rows.indexOf(row);
    const next = rows[index + (event.key === 'ArrowDown' ? 1 : -1)];
    if (next) {
      event.preventDefault();
      next.focus();
    }
  }, { signal });
}

function renderPageView(): void {
  const page = rootElement();
  if (!page) return;
  const toggle = page.querySelector<HTMLButtonElement>('[data-tracing-action="toggle-view"]');
  if (toggle) toggle.textContent = viewMode === 'traces' ? '系统日志' : '调用追踪';
  const pause = page.querySelector<HTMLButtonElement>('[data-tracing-action="toggle-pause"]');
  if (pause) pause.textContent = paused ? '继续刷新' : '暂停刷新';
  page.classList.toggle('is-log-view', viewMode === 'logs');
  if (viewMode === 'logs') renderLogList();
  else {
    renderTraceList();
    renderDetail(selectedBundle);
  }
}

async function activate(context?: PageLifecycleContext): Promise<void> {
  const root = rootElement();
  if (!root) return;
  root.replaceChildren(buildPage());
  bindPage();
  if (context?.signal) {
    if (context.signal.aborted) return;
    context.signal.addEventListener('abort', () => activeController?.abort(), { once: true });
  }
  resetQueryState();
  activeFilter = recentHourFilter();
  setFilterForm(activeFilter);
  refreshDelayMs = AUTO_REFRESH_BASE_MS;
  lastRefreshFailed = false;
  viewMode = 'traces';
  renderDetail(null);
  try {
    const capabilities = await backendApi.tracingCapabilities(currentSignal());
    if (!capabilities.available) {
      setStatus('后端未开放追踪能力', 'offline');
      renderTraceList();
      return;
    }
  } catch (error) {
    if (!aborted(error)) setStatus(error instanceof Error ? error.message : '追踪能力查询失败', 'error');
    return;
  }
  await loadTraces(true);
  scheduleRefresh();
}

function deactivate(): void {
  activeController?.abort();
  activeController = null;
  if (refreshTimer != null) window.clearTimeout(refreshTimer);
  refreshTimer = null;
  // Leaving the page aborts in-flight queries only.  A durable export job is
  // intentionally left on the Gateway until it expires; DELETE is reserved
  // for the explicit "取消导出" action.
  activeExportId = '';
  selectedTraceId = '';
  selectedSpanId = '';
  selectedEventSeq = -1;
  selectedBundle = null;
  traceItems = [];
  logItems = [];
  selectedLog = null;
  activeExportJob = null;
  rootElement()?.replaceChildren();
}

export function createTracingPageContribution(): PageContribution {
  return {
    id: PAGE_ID,
    isAvailable: () => isObservationDevLaunch(),
    activate,
    deactivate,
  };
}

export function disposeTracingPage(): void { deactivate(); }
