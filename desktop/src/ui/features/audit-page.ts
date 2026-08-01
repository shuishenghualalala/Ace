/**
 * 审计页：左侧 Tab + 右侧内容
 *
 * 两个 Tab：
 *   - 行为审计：7 天趋势 + KPI 卡片 + 7 列事件表 + 行内详情侧栏
 *   - 会话审计：会话卡片列表 + 会话详情 + 对话日志
 *
 * 当前展示本地演示数据，后续可接入通用审计数据源。
 */

import { $, escapeHtml, notify } from '../state';
import { bindPagination, paginate, renderPagination } from '../pagination';

type AuditTab = 'behavior' | 'session';
type RiskLevel = 'none' | 'low' | 'medium' | 'high' | 'critical';
type ActionResult = 'pass' | 'block' | 'review';

interface BehaviorEvent {
  id: string;
  type: string; // 行为检测
  category: string; // 文件读取 / 文件写入 / 网络请求 ...
  summary: string;
  hitRule: string; // 命中的规则
  risk: RiskLevel;
  result: ActionResult;
  timestamp: number; // unix seconds
  actor: string;
  target: string;
  meta: { ip?: string; location?: string; device?: string };
  /** 数据来源：'mock' = 演示数据，'live' = 来自后端审计日志。I-12 / S-20。 */
  source?: 'mock' | 'live';
}

interface SessionMessage {
  ts: number;
  role: 'user' | 'assistant';
  seq: number;
  content: string;
  sensitive?: boolean;
  sensitiveAction?: string;
}

interface SessionDetection {
  detection_type: string;
  source: string;
  action_taken: string;
  risk_level: number;
  content_preview: string;
  findings: Array<{ id?: string; name?: string; category?: string; count?: number; preview?: string }>;
  timestamp: number;
}

interface SessionAudit {
  id: string;
  name: string;
  agent: string;
  user: string;
  startTime: number;
  endTime: number;
  turns: number;
  tokens: number;
  risk: RiskLevel;
  key: string;
  messages: SessionMessage[];
  detections: SessionDetection[];
  /** 最近一条用户消息内容（用于卡片预览），由 mapSessionToAudit 从 API 提取 */
  lastUserMessage: string;
}

const TODAY = Math.floor(Date.now() / 1000); // mock 用今天时间，保持相对新鲜
const HOUR = 3600;

const RISK_LABELS: Record<RiskLevel, string> = {
  none: '正常',
  low: '低危',
  medium: '中危',
  high: '高危',
  critical: '严重',
};

const RESULT_LABELS: Record<ActionResult, string> = {
  pass: '已放行',
  block: '已拦截',
  review: '待复核',
};

const CATEGORIES = ['文件读取', '文件写入', '网络请求', '命令执行', '数据查询', '内容生成'];

let behaviorIdSeq = 0;

function mkBehavior(
  offsetHrs: number,
  category: string,
  summary: string,
  risk: RiskLevel,
  result: ActionResult,
  opts: Partial<BehaviorEvent> = {},
): BehaviorEvent {
  behaviorIdSeq += 1;
  return {
    id: opts.id ?? `bh_${behaviorIdSeq.toString(16).padStart(4, '0')}`,
    type: '行为检测',
    category,
    summary,
    hitRule: '--',
    risk,
    result,
    timestamp: TODAY - offsetHrs * HOUR,
    actor: 'Crew',
    target: 'unknown',
    meta: { ip: '192.0.2.42', location: '上海', device: 'macOS 15.4' },
    source: 'mock',
    ...opts,
  };
}

const BEHAVIOR_SEED: BehaviorEvent[] = [
  mkBehavior(0.2, '文件读取', '读取后检查', 'none', 'pass', { hitRule: 'file-read.after-check', meta: { ip: '192.0.2.42', location: '上海', device: 'macOS 15.4' } }),
  mkBehavior(0.2, '文件读取', '读取前检查', 'none', 'pass', { hitRule: 'file-read.pre-check' }),
  mkBehavior(1.2, '文件读取', '读取后检查', 'none', 'pass', { hitRule: 'file-read.after-check' }),
  mkBehavior(1.2, '文件读取', '读取前检查', 'none', 'pass', { hitRule: 'file-read.pre-check' }),
  mkBehavior(2.5, '文件读取', '读取后检查', 'none', 'pass', { hitRule: 'file-read.after-check' }),
  mkBehavior(2.5, '文件读取', '读取前检查', 'none', 'pass', { hitRule: 'file-read.pre-check' }),
  mkBehavior(3.5, '文件读取', '读取后检查', 'none', 'pass'),
  mkBehavior(3.5, '文件读取', '读取前检查', 'none', 'pass'),
  mkBehavior(5.0, '文件写入', '写入前检查', 'none', 'pass', { hitRule: 'file-write.pre-check', actor: '林岚' }),
  mkBehavior(5.5, '网络请求', '外发文件 DLP 检查', 'high', 'review', { hitRule: 'dlp.outbound-file', actor: '韩彻', summary: '外发邮件附带客户名单 CSV，命中 DLP' }),
  mkBehavior(7.2, '命令执行', '禁用命令拦截', 'critical', 'block', { hitRule: 'shell.deny.rm-rf', summary: '尝试执行 rm -rf /', actor: '外部' }),
  mkBehavior(8.0, '数据查询', 'PII 字段脱敏', 'medium', 'pass', { hitRule: 'pii.masking', summary: '查询用户表含身份证号，已脱敏', actor: 'Marcus Wei' }),
  mkBehavior(12, '内容生成', '敏感词扫描', 'medium', 'review', { hitRule: 'content.keyword-scan', summary: '回复含「工资」相关关键词', actor: '李冉' }),
  mkBehavior(18, '文件写入', '写入审计日志', 'none', 'pass', { hitRule: 'audit.append' }),
  mkBehavior(24, '权限校验', '权限降级复核', 'medium', 'review', { summary: 'Agent 申请 export_dataset 权限', hitRule: 'perm.escalation' }),
  mkBehavior(36, '网络请求', '公网域名解析', 'none', 'pass', { hitRule: 'dns.public' }),
  mkBehavior(48, '数据查询', '跨表关联', 'none', 'pass', { hitRule: 'sql.cross-table' }),
  mkBehavior(72, '文件读取', '配置文件读取', 'none', 'pass', { hitRule: 'config.read' }),
];

/** 扩充 mock 体量，便于验证分页交互 */
const BEHAVIORS: BehaviorEvent[] = [
  ...BEHAVIOR_SEED,
  ...Array.from({ length: 48 }, (_, i) =>
    mkBehavior(
      80 + i * 0.5,
      CATEGORIES[i % CATEGORIES.length],
      `批量审计事件 #${i + 1}`,
      i % 11 === 0 ? 'medium' : 'none',
      i % 17 === 0 ? 'review' : 'pass',
      { hitRule: `mock.rule.${i + 1}`, actor: i % 3 === 0 ? '外部' : 'Crew' },
    ),
  ),
];

const SESSION_SEED: SessionAudit[] = [
  {
    id: 'sess_92a3f1',
    name: 'Crew',
    agent: 'Crew',
    user: 'Crew',
    startTime: TODAY - 18 * HOUR,
    endTime: TODAY - 18 * HOUR,
    turns: 1,
    tokens: 12,
    risk: 'none',
    key: 'a05e3280-c4c2-49ac-9e48-6237c9bcbb1b',
    messages: [
      { ts: TODAY - 18 * HOUR, role: 'assistant', seq: 0, content: 'HEARTBEAT_OK' },
    ],
    detections: [],
    lastUserMessage: '',
  },
  {
    id: 'sess_77b2ce',
    name: 'Crew',
    agent: 'Crew',
    user: 'Crew',
    startTime: TODAY - 42 * HOUR,
    endTime: TODAY - 42 * HOUR,
    turns: 1,
    tokens: 8,
    risk: 'none',
    key: 'b8d3a771-22e1-4f01-9b22-7a4f1ed02bbd',
    messages: [
      { ts: TODAY - 42 * HOUR, role: 'assistant', seq: 0, content: 'HEARTBEAT_OK' },
    ],
    detections: [],
    lastUserMessage: '',
  },
  {
    id: 'sess_55e1d2',
    name: 'Crew',
    agent: 'Crew',
    user: 'Crew',
    startTime: TODAY - 3 * 24 * HOUR,
    endTime: TODAY - 3 * 24 * HOUR,
    turns: 1,
    tokens: 14,
    risk: 'none',
    key: 'f0e1a8a3-c9d2-4c12-9a55-21c4bb3c2bb1',
    messages: [
      { ts: TODAY - 3 * 24 * HOUR, role: 'assistant', seq: 0, content: 'HEARTBEAT_OK' },
    ],
    detections: [],
    lastUserMessage: '',
  },
  {
    id: 'sess_44c0fa',
    name: 'Crew',
    agent: 'Crew',
    user: 'Crew',
    startTime: TODAY - 4 * 24 * HOUR,
    endTime: TODAY - 4 * 24 * HOUR,
    turns: 1,
    tokens: 9,
    risk: 'none',
    key: '92d9c0e1-7f31-4d28-aa23-91bbf1e3a4c7',
    messages: [
      { ts: TODAY - 4 * 24 * HOUR, role: 'assistant', seq: 0, content: 'HEARTBEAT_OK' },
    ],
    detections: [],
    lastUserMessage: '',
  },
  {
    id: 'sess_chat_demo',
    name: 'Crew',
    agent: 'Crew',
    user: 'Crew',
    startTime: TODAY - 8 * 24 * HOUR,
    endTime: TODAY - 8 * 24 * HOUR + 15 * 60,
    turns: 2,
    tokens: 59,
    risk: 'none',
    key: 'ca19e6b2-d227-4640-99d2-e91e1c42aa72',
    messages: [
      { ts: TODAY - 8 * 24 * HOUR, role: 'user', seq: 0, content: 'hi' },
      { ts: TODAY - 8 * 24 * HOUR + 16, role: 'assistant', seq: 0, content: 'Hey! 👋 How\'s it going? What can I help you with today?' },
      { ts: TODAY - 8 * 24 * HOUR + 15 * 60, role: 'user', seq: 0, content: 'hi' },
    ],
    detections: [],
    lastUserMessage: 'hi',
  },
];

const SESSIONS: SessionAudit[] = [
  ...SESSION_SEED,
  ...Array.from({ length: 22 }, (_, i) => ({
    id: `sess_gen_${i}`,
    name: 'Crew',
    agent: 'Crew',
    user: i % 4 === 0 ? '林岚' : 'Crew',
    startTime: TODAY - (10 + i) * 24 * HOUR,
    endTime: TODAY - (10 + i) * 24 * HOUR + 600,
    turns: 1 + (i % 5),
    tokens: 10 + i * 3,
    risk: (i % 9 === 0 ? 'medium' : 'none') as RiskLevel,
    key: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
    messages: [{ ts: TODAY - (10 + i) * 24 * HOUR, role: 'assistant' as const, seq: 0, content: `HEARTBEAT_OK #${i + 1}` }],
    detections: [],
    lastUserMessage: '',
  })),
];

/** 预留实时数据状态；未接入数据源时使用 mock。 */
const liveBehaviors: BehaviorEvent[] | null = null;
const liveSessions: SessionAudit[] | null = null;
const liveTrend: DayBucket[] | null = null;
const liveKPIs: { total: number; pass: number; block: number; review: number } | null = null;

function effectiveBehaviors(): BehaviorEvent[] {
  return liveBehaviors ?? BEHAVIORS;
}

function effectiveSessions(): SessionAudit[] {
  return liveSessions ?? SESSIONS;
}

let activeTab: AuditTab = 'behavior';
let behaviorFilter: 'all' | ActionResult = 'all';
let searchQ = '';
let selectedBehaviorId: string | null = BEHAVIOR_SEED[0].id;
let selectedSessionId: string = SESSION_SEED[0].id;
let behaviorPage = 1;
let behaviorPageSize = 10;
let sessionPage = 1;
let sessionPageSize = 8;

// ─────────────── 工具函数 ───────────────

function fmtAbsolute(unix: number): string {
  const d = new Date(unix * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${String(d.getFullYear()).slice(2)}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtTimeAgo(unix: number): string {
  const now = Math.floor(Date.now() / 1000);
  const diff = now - unix;
  if (diff < 0) return '刚刚';
  if (diff < 60) return `${diff} 秒前`;
  if (diff < HOUR) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 24 * HOUR) return `${Math.floor(diff / HOUR)} 小时前`;
  const d = Math.floor(diff / (24 * HOUR));
  return `${d} 天前`;
}

// ─────────────── 7 天趋势 mock ───────────────

interface DayBucket {
  dayLabel: string; // 周一
  pass: number;
  block: number;
}

function buildTrend(): DayBucket[] {
  const labels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const today = new Date(TODAY * 1000);
  const start = new Date(today);
  start.setHours(0, 0, 0, 0);
  // 找到今天是周几（0=周日），回退到本周一
  const dow = (start.getDay() + 6) % 7; // 0=周一
  start.setDate(start.getDate() - dow);

  const pass = [120, 98, 142, 156, 134, 88, 76];
  const block = [2, 1, 4, 6, 3, 1, 0];
  const buckets: DayBucket[] = [];
  for (let i = 0; i < 7; i += 1) {
    buckets.push({
      dayLabel: labels[i],
      pass: pass[i] ?? 0,
      block: block[i] ?? 0,
    });
  }
  return buckets;
}

// ─────────────── 行为审计 ───────────────

function visibleBehaviors(): BehaviorEvent[] {
  return effectiveBehaviors().filter((b) => {
    if (behaviorFilter !== 'all' && b.result !== behaviorFilter) return false;
    if (searchQ) {
      const q = searchQ.toLowerCase();
      const hay = `${b.type} ${b.category} ${b.summary} ${b.hitRule} ${b.actor}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function behaviorKPIs(): { total: number; pass: number; block: number; review: number } {
  if (liveKPIs) return liveKPIs;
  return effectiveBehaviors().reduce(
    (acc, b) => {
      acc.total += 1;
      if (b.result === 'pass') acc.pass += 1;
      if (b.result === 'block') acc.block += 1;
      if (b.result === 'review') acc.review += 1;
      return acc;
    },
    { total: 0, pass: 0, block: 0, review: 0 },
  );
}

/** 行为审计表各列可拖拽宽度上下限（px）。 */
const AUDIT_COL_BOUNDS: Record<number, { min: number; max: number }> = {
  0: { min: 72, max: 116 },
  1: { min: 140, max: 300 },
  2: { min: 96, max: 188 },
  3: { min: 76, max: 108 },
  4: { min: 96, max: 140 },
  5: { min: 148, max: 196 },
};

function trendChart(buckets: DayBucket[]): string {
  if (!buckets.length) {
    return '<div class="audit-trend-empty">暂无 7 天趋势数据</div>';
  }
  const W = 640;
  const H = 220;
  const padL = 36;
  const padR = 12;
  const padT = 12;
  const padB = 26;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const max = Math.max(...buckets.map((b) => b.pass + b.block), 1);
  const yTicks = 3;
  const parts: string[] = [];

  parts.push(
    `<defs>
      <linearGradient id="audit-chart-bg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="rgba(71, 125, 247, 0.08)"/>
        <stop offset="100%" stop-color="rgba(71, 125, 247, 0)"/>
      </linearGradient>
      <linearGradient id="audit-bar-pass" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#8bb0ff"/>
        <stop offset="55%" stop-color="#5b8def"/>
        <stop offset="100%" stop-color="#477df7"/>
      </linearGradient>
      <linearGradient id="audit-bar-block" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#fb7185"/>
        <stop offset="100%" stop-color="#ef4444"/>
      </linearGradient>
      <filter id="audit-bar-glow" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#477df7" flood-opacity="0.18"/>
      </filter>
    </defs>`,
    `<rect class="audit-trend-svg__bg" x="${padL}" y="${padT}" width="${innerW}" height="${innerH}" rx="12"/>`,
    `<line class="audit-trend-svg__axis" x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + innerH}"/>`,
    `<line class="audit-trend-svg__axis" x1="${padL}" y1="${padT + innerH}" x2="${W - padR}" y2="${padT + innerH}"/>`,
  );

  for (let k = 0; k <= yTicks; k += 1) {
    const y = padT + (innerH * k) / yTicks;
    const val = Math.round((max * (yTicks - k)) / yTicks);
    if (k > 0) {
      parts.push(`<line class="audit-trend-svg__grid" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"/>`);
    }
    parts.push(
      `<text class="audit-trend-svg__ylabel" x="${padL - 6}" y="${y + 3.5}" text-anchor="end">${val}</text>`,
    );
  }

  const slot = innerW / buckets.length;
  const barW = Math.min(36, slot * 0.48);
  const barR = Math.min(8, barW / 2);
  buckets.forEach((b, i) => {
    const total = b.pass + b.block;
    const x = padL + slot * i + (slot - barW) / 2;
    const totalH = (total / max) * innerH;
    const blockH = b.block > 0 ? Math.max((b.block / max) * innerH, 4) : 0;
    const passH = Math.max(0, totalH - blockH);
    const baseY = padT + innerH;
    const cx = (x + barW / 2).toFixed(1);
    const group = [
      `<g class="audit-trend-svg__group" data-day="${escapeHtml(b.dayLabel)}" data-pass="${b.pass}" data-block="${b.block}" data-total="${total}">`,
      `<rect class="audit-trend-svg__hit" x="${x.toFixed(1)}" y="${padT}" width="${barW.toFixed(1)}" height="${innerH}" rx="6"/>`,
    ];
    if (totalH > 1) {
      group.push(
        `<rect class="audit-trend-svg__bar-shadow" x="${(x + 2).toFixed(1)}" y="${(baseY - totalH + 2).toFixed(1)}" width="${barW.toFixed(1)}" height="${totalH.toFixed(1)}" rx="${barR}" opacity="0.12"/>`,
      );
    }
    if (blockH > 0.5) {
      group.push(
        `<rect class="audit-trend-svg__bar audit-trend-svg__bar--block" x="${x.toFixed(1)}" y="${(baseY - totalH).toFixed(1)}" width="${barW.toFixed(1)}" height="${blockH.toFixed(1)}" rx="${Math.min(barR, blockH / 2)}"/>`,
      );
    }
    if (passH > 0.5) {
      const passY = baseY - totalH + blockH;
      group.push(
        `<rect class="audit-trend-svg__bar audit-trend-svg__bar--pass" x="${x.toFixed(1)}" y="${passY.toFixed(1)}" width="${barW.toFixed(1)}" height="${passH.toFixed(1)}" rx="${Math.min(barR, passH / 2)}" filter="url(#audit-bar-glow)"/>`,
        `<rect class="audit-trend-svg__bar-cap" x="${x.toFixed(1)}" y="${passY.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.min(4, passH).toFixed(1)}" rx="${Math.min(barR, passH / 2)}" opacity="0.35"/>`,
      );
    }
    group.push(
      `<text class="audit-trend-svg__total" x="${cx}" y="${(baseY - totalH - 7).toFixed(1)}" text-anchor="middle">${total}</text>`,
      `<text class="audit-trend-svg__xlabel" x="${cx}" y="${(baseY + 18).toFixed(1)}" text-anchor="middle">${b.dayLabel}</text>`,
      '</g>',
    );
    parts.push(group.join(''));
  });

  return `<svg viewBox="0 0 ${W} ${H}" class="audit-trend-svg" preserveAspectRatio="none" role="img" aria-label="7天事件趋势柱状图">${parts.join('')}</svg>`;
}

function categoryTone(category: string): string {
  const c = category.toLowerCase();
  if (c.includes('file') || c.includes('read') || c.includes('write') || c.includes('edit')) return 'file';
  if (c.includes('http') || c.includes('fetch') || c.includes('curl') || c.includes('browser') || c.includes('navigate')) return 'net';
  if (c.includes('bash') || c.includes('shell') || c.includes('execute') || c.includes('terminal')) return 'cmd';
  if (c.includes('search') || c.includes('grep') || c.includes('find') || c.includes('list')) return 'data';
  if (c.includes('文件')) return 'file';
  if (c.includes('网络')) return 'net';
  if (c.includes('命令')) return 'cmd';
  if (c.includes('数据')) return 'data';
  return 'default';
}

function behaviorRow(b: BehaviorEvent): string {
  const isSelected = b.id === selectedBehaviorId;
  const mockBadge = b.source === 'mock'
    ? '<span class="audit-pill audit-pill--mock" title="演示数据">Mock</span>'
    : '';
  const tone = categoryTone(b.category);
  return `
    <tr class="audit-row${isSelected ? ' is-selected' : ''}" data-behavior-id="${b.id}">
      <td><span class="audit-category audit-category--${tone}" title="${escapeHtml(b.category)}" style="display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(b.category)}</span></td>
      <td><span class="audit-cell-clip audit-cell-clip--summary" title="${escapeHtml(b.summary)}">${escapeHtml(b.summary)}</span></td>
      <td><span class="audit-cell-clip audit-cell-clip--mono" title="${escapeHtml(b.hitRule)}">${escapeHtml(b.hitRule)}</span></td>
      <td><span class="audit-pill audit-pill--${b.risk}">${RISK_LABELS[b.risk]}</span></td>
      <td><div class="audit-cell-inline"><span class="audit-pill audit-pill--${b.result}">${RESULT_LABELS[b.result]}</span>${mockBadge}</div></td>
      <td class="audit-row__time" style="white-space:nowrap">${fmtAbsolute(b.timestamp)}</td>
    </tr>
  `;
}

function behaviorDetail(): string {
  const b = effectiveBehaviors().find((x) => x.id === selectedBehaviorId);
  if (!b) {
    return `<aside class="audit-detail-pane"><div class="audit-detail-pane__empty">点击左侧表格中的事件查看详情</div></aside>`;
  }
  return `
    <aside class="audit-detail-pane">
      <header class="audit-detail-pane__head">
        <div class="audit-detail-pane__id">${escapeHtml(b.id)}</div>
        <h3 class="audit-detail-pane__title">${escapeHtml(b.summary)}</h3>
        <div class="audit-detail-pane__chips">
          <span class="audit-pill audit-pill--${b.risk}">${RISK_LABELS[b.risk]}风险</span>
          <span class="audit-pill audit-pill--${b.result}">${RESULT_LABELS[b.result]}</span>
        </div>
      </header>
      <dl class="audit-detail-pane__rows">
        <div><dt>类型</dt><dd>${escapeHtml(b.type)}</dd></div>
        <div><dt>类型</dt><dd>${escapeHtml(b.category)}</dd></div>
        <div><dt>命中规则</dt><dd>${escapeHtml(b.hitRule)}</dd></div>
        <div><dt>时间</dt><dd>${fmtAbsolute(b.timestamp)}</dd></div>
        <div><dt>执行者</dt><dd>${escapeHtml(b.actor)}</dd></div>
        <div><dt>设备</dt><dd>${escapeHtml(b.meta.device ?? '--')}</dd></div>
        <div><dt>IP / 地点</dt><dd>${escapeHtml(b.meta.ip ?? '--')} · ${escapeHtml(b.meta.location ?? '--')}</dd></div>
      </dl>
      <footer class="audit-detail-pane__foot"></footer>
    </aside>
  `;
}

function renderBehavior(): string {
  const kpis = behaviorKPIs();
  const buckets = liveTrend ?? buildTrend();
  const events = visibleBehaviors();
  const pageItems = paginate(events, behaviorPage, behaviorPageSize);
  if (pageItems.length && !pageItems.some((b) => b.id === selectedBehaviorId)) {
    selectedBehaviorId = pageItems[0].id;
  }

  const rows = pageItems.length
    ? pageItems.map(behaviorRow).join('')
    : `<tr><td colspan="6" class="audit-table__empty">${visibleBehaviors().length === 0 ? '暂无审计事件' : '没有匹配的事件'}</td></tr>`;

  const pager = renderPagination(
    { page: behaviorPage, pageSize: behaviorPageSize, total: events.length },
    { id: 'audit-behavior' },
  );

  return `
    <section class="audit-pane audit-pane--behavior">
      <div class="audit-overview-grid">
        <div class="audit-trend-wrap audit-trend-wrap--v2">
          <div class="audit-trend-head">
            <h3 class="audit-trend-head__title">7 天事件趋势</h3>
            <div class="audit-trend-head__legend">
              <span><span class="audit-trend-head__dot audit-trend-head__dot--pass"></span>已放行</span>
              <span><span class="audit-trend-head__dot audit-trend-head__dot--block"></span>已拦截</span>
            </div>
          </div>
          <div class="audit-trend-v2 audit-trend-v2--chart">
            ${trendChart(buckets)}
            <div class="audit-trend-svg__tooltip" hidden></div>
          </div>
        </div>

        <div class="audit-kpis">
          <div class="audit-kpi">
            <div class="audit-kpi__num is-blue">${kpis.total}</div>
            <div class="audit-kpi__label">事件总数</div>
          </div>
          <div class="audit-kpi">
            <div class="audit-kpi__num is-blue">${kpis.pass}</div>
            <div class="audit-kpi__label">已放行</div>
          </div>
          <div class="audit-kpi">
            <div class="audit-kpi__num is-red">${kpis.block + kpis.review}</div>
            <div class="audit-kpi__label">拦截 / 复核</div>
          </div>
        </div>
      </div>

      <div class="audit-toolbar">
        <div class="audit-toolbar__filters">
          <span class="audit-toolbar__label">处置结果</span>
          <select class="audit-select" id="audit-behavior-result">
            <option value="all"${behaviorFilter === 'all' ? ' selected' : ''}>全部</option>
            <option value="pass"${behaviorFilter === 'pass' ? ' selected' : ''}>已放行</option>
            <option value="review"${behaviorFilter === 'review' ? ' selected' : ''}>待复核</option>
            <option value="block"${behaviorFilter === 'block' ? ' selected' : ''}>已拦截</option>
          </select>
          <input class="audit-input" id="audit-behavior-search" type="text" placeholder="搜索事件、规则、执行者…" value="${escapeHtml(searchQ)}" />
        </div>
      </div>

      <div class="audit-layout">
        <div class="audit-table-wrap">
          <table class="audit-table audit-table--behavior">
            <colgroup>
              <col data-col="0" style="width:105px" />
              <col data-col="1" style="width:24%" />
              <col data-col="2" style="width:18%" />
              <col data-col="3" style="width:68px" />
              <col data-col="4" style="width:80px" />
              <col data-col="5" style="width:120px" />
            </colgroup>
            <thead>
              <tr>
                <th class="audit-th--resizable">类型<span class="audit-col-grip" data-col="0" aria-hidden="true"></span></th>
                <th class="audit-th--resizable">摘要<span class="audit-col-grip" data-col="1" aria-hidden="true"></span></th>
                <th class="audit-th--resizable">命中规则<span class="audit-col-grip" data-col="2" aria-hidden="true"></span></th>
                <th class="audit-th--resizable">风险等级<span class="audit-col-grip" data-col="3" aria-hidden="true"></span></th>
                <th class="audit-th--resizable">处置结果<span class="audit-col-grip" data-col="4" aria-hidden="true"></span></th>
                <th class="audit-th--resizable">时间<span class="audit-col-grip" data-col="5" aria-hidden="true"></span></th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
          ${pager}
        </div>
        ${behaviorDetail()}
      </div>
    </section>
  `;
}

// ─────────────── 会话审计 ───────────────

function selectedSession(): SessionAudit | undefined {
  return effectiveSessions().find((s) => s.id === selectedSessionId);
}

function fmtTimeRange(s: SessionAudit): string {
  const a = fmtAbsolute(s.startTime);
  const sameDay = new Date(s.startTime * 1000).toDateString() === new Date(s.endTime * 1000).toDateString();
  if (sameDay) {
    const d = new Date(s.endTime * 1000);
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${a} - ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  return `${a} - ${fmtAbsolute(s.endTime)}`;
}

function renderSessionCard(s: SessionAudit): string {
  const isSelected = s.id === selectedSessionId;
  const riskCls = s.risk !== 'none' ? ` audit-session-card--${s.risk}` : '';
  return `
    <button class="audit-session-card${isSelected ? ' is-selected' : ''}${riskCls}" type="button" data-session-id="${s.id}">
      <div class="audit-session-card__head">
        <span class="audit-session-card__name">${escapeHtml(s.name)}</span>
        <span class="audit-pill audit-pill--${s.risk}">${RISK_LABELS[s.risk]}</span>
      </div>
      <div class="audit-session-card__meta">${s.lastUserMessage ? escapeHtml(s.lastUserMessage) : ''}</div>
      <div class="audit-session-card__time">${fmtTimeAgo(s.startTime)}</div>
    </button>
  `;
}

const SEVERITY_LABELS: Record<string, string> = {
  sensitive_word: '敏感词检测',
  injection: '注入检测',
  behavior: '行为检测',
};

const ACTION_LABELS: Record<string, string> = {
  block: '已拦截',
  sensitive_block: '已拦截',
  redact: '已脱敏',
  warn: '告警',
  injection_block: '注入拦截',
};

const RISK_LEVEL_MAP: Record<number, { label: string; cls: string }> = {
  0: { label: '正常', cls: 'none' },
  1: { label: '低危', cls: 'low' },
  2: { label: '中危', cls: 'medium' },
  3: { label: '高危', cls: 'high' },
  4: { label: '严重', cls: 'critical' },
};

function findDetectionForMessage(s: SessionAudit, m: SessionMessage): SessionDetection | null {
  if (!s.detections?.length) return null;
  const threshold = 5;
  return s.detections.find((d) => Math.abs(d.timestamp - m.ts) < threshold) ?? s.detections[0] ?? null;
}

function renderDetectionBox(d: SessionDetection): string {
  const rl = RISK_LEVEL_MAP[d.risk_level] ?? RISK_LEVEL_MAP[0];
  const typeLabel = SEVERITY_LABELS[d.detection_type] ?? d.detection_type;
  const actionLabel = ACTION_LABELS[d.action_taken] ?? d.action_taken;
  const findings = d.findings ?? [];

  const findingsHtml = findings.length > 0
    ? findings.map((f) => `
        <div class="audit-det__finding">
          <span class="audit-det__finding-name">${escapeHtml(f.name || f.id || '--')}</span>
          <span class="audit-det__finding-cat">${escapeHtml(f.category || '--')}</span>
          ${f.preview ? `<span class="audit-det__finding-match">匹配: ${escapeHtml(f.preview)}</span>` : ''}
        </div>
      `).join('')
    : '';

  return `
    <div class="audit-det audit-det--${rl.cls}">
      <div class="audit-det__head">
        <span style="font-size:13px">⚠</span>
        <span class="audit-pill audit-pill--${rl.cls}" style="font-size:11px">${rl.label}</span>
        <span class="audit-pill audit-pill--${d.action_taken === 'block' || d.action_taken === 'sensitive_block' ? 'critical' : 'high'}" style="font-size:11px">${actionLabel}</span>
        <span class="audit-det__type">${typeLabel}</span>
      </div>
      ${findingsHtml ? `<div class="audit-det__findings">${findingsHtml}</div>` : ''}
    </div>
  `;
}

function renderSessionDetail(): string {
  const s = selectedSession();
  if (!s) return `<aside class="audit-session-detail"><div class="audit-session-detail__empty">选择左侧会话查看详情</div></aside>`;
  const messages = s.messages
    .map(
      (m) => {
        const isUserRisk = m.sensitive && m.role === 'user';
        const det = isUserRisk ? findDetectionForMessage(s, m) : null;
        const detHtml = det ? renderDetectionBox(det) : '';
        return `
      <div class="audit-msg audit-msg--${m.role}${isUserRisk ? ' audit-msg--risk' : ''}">
        <div class="audit-msg__head">
          <span>${fmtAbsolute(m.ts)}</span>
          <span class="audit-msg__sep">·</span>
          <span>${m.role === 'user' ? 'user' : m.role}</span>
          <span class="audit-msg__sep">·</span>
          <span>#${m.seq}</span>
          ${isUserRisk ? `<span class="audit-pill audit-pill--${m.sensitiveAction === 'block' || m.sensitiveAction === 'sensitive_block' ? 'critical' : 'high'}" style="margin-left:auto;font-size:11px">${m.sensitiveAction === 'block' || m.sensitiveAction === 'sensitive_block' ? '已拦截' : m.sensitiveAction === 'redact' ? '已脱敏' : '告警'}</span>` : ''}
        </div>
        <div class="audit-msg__body">${escapeHtml(m.content)}</div>
        ${detHtml}
      </div>
    `;
      },
    )
    .join('');
  const detailRiskCls = s.risk !== 'none' ? ` audit-session-detail--${s.risk}` : '';
  return `
    <aside class="audit-session-detail${detailRiskCls}">
      <header class="audit-session-detail__head">
        <div>
          <h3 class="audit-session-detail__title">${escapeHtml(s.name)}</h3>
          <div class="audit-session-detail__time">${fmtTimeRange(s)}</div>
        </div>
        <span class="audit-pill audit-pill--${s.risk}">${RISK_LABELS[s.risk]}</span>
      </header>

      <div class="audit-session-detail__stats">
        <div class="audit-session-stat">
          <div class="audit-session-stat__num is-blue">${s.turns}</div>
          <div class="audit-session-stat__label">对话轮数</div>
        </div>
        <div class="audit-session-stat">
          <div class="audit-session-stat__num is-blue">${s.tokens}</div>
          <div class="audit-session-stat__label">Token 数</div>
        </div>
        <div class="audit-session-stat">
          <div class="audit-session-stat__num is-${s.risk === 'none' ? 'green' : s.risk === 'high' || s.risk === 'critical' ? 'red' : s.risk === 'low' ? 'blue' : 'amber'}">${RISK_LABELS[s.risk]}</div>
          <div class="audit-session-stat__label">风险等级</div>
        </div>
      </div>

      <dl class="audit-session-detail__rows">
        <div><dt>Key</dt><dd><code>${escapeHtml(s.key)}</code></dd></div>
        <div><dt>Agent</dt><dd>${escapeHtml(s.agent)}</dd></div>
        <div><dt>User</dt><dd>${escapeHtml(s.user)}</dd></div>
      </dl>

      <div class="audit-session-detail__msgs">
        ${messages || '<div class="audit-session-detail__empty">无对话消息</div>'}
      </div>
    </aside>
  `;
}

function renderSession(): string {
  const all = effectiveSessions();
  const pageItems = paginate(all, sessionPage, sessionPageSize);
  if (pageItems.length && !pageItems.some((s) => s.id === selectedSessionId)) {
    selectedSessionId = pageItems[0].id;
  }
  const cards = pageItems.map(renderSessionCard).join('');
  const pager = renderPagination(
    { page: sessionPage, pageSize: sessionPageSize, total: all.length },
    { id: 'audit-session', pageSizeChoices: [8, 12, 24] },
  );
  return `
    <section class="audit-pane audit-pane--session">
      <div class="audit-session-grid">
        <div class="audit-session-list">
          <div class="audit-session-list__head">会话列表 · ${all.length}</div>
          <div class="audit-session-list__body">${cards}</div>
          ${pager}
        </div>
        ${renderSessionDetail()}
      </div>
    </section>
  `;
}

// ─────────────── 入口渲染 ───────────────

function render(): void {
  const root = $('#audit-page-root');
  if (!root) return;

  root.innerHTML = `
    <div class="audit-shell-v2 page-shell page-shell--audit">
      <header class="page-header page-header--hub audit-topbar">
        <div class="page-header__copy audit-topbar__copy">
          <h1 class="page-header__title audit-topbar__title">安全审计</h1>
          <p class="page-header__desc audit-topbar__desc">${activeTab === 'behavior' ? 'AI 的后台操作全记录：工具调用、数据访问及权限变更，让每一步都透明可见。' : '会话安全与对话日志：随时回溯聊天内容，保障沟通安全。'}</p>
        </div>
        <div class="page-header__actions audit-topbar__actions">
          <button type="button" class="hub-refresh-btn" data-audit-action="refresh" title="刷新">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg>
            刷新
          </button>
        </div>
      </header>
      <nav class="audit-subnav" aria-label="审计视图">
          <button type="button" class="skill-subnav__item${activeTab === 'behavior' ? ' is-active' : ''}" data-audit-tab="behavior">
            行为审计
            <span class="skill-subnav__count">${effectiveBehaviors().length}</span>
          </button>
          <button type="button" class="skill-subnav__item${activeTab === 'session' ? ' is-active' : ''}" data-audit-tab="session">
            会话审计
            <span class="skill-subnav__count">${effectiveSessions().length}</span>
          </button>
        </nav>
      <main class="audit-content">
        ${activeTab === 'behavior' ? renderBehavior() : renderSession()}
      </main>
    </div>
  `;
  bindEvents();
}

function bindEvents(): void {
  document.querySelectorAll<HTMLButtonElement>('[data-audit-tab]').forEach((b) => {
    b.addEventListener('click', () => {
      const t = b.getAttribute('data-audit-tab') as AuditTab;
      if (t && (t === 'behavior' || t === 'session') && t !== activeTab) {
        activeTab = t;
        behaviorPage = 1;
        sessionPage = 1;
        render();
      }
    });
  });

  // 通用按钮动作：data-audit-action
  document.querySelectorAll<HTMLButtonElement>('[data-audit-action]').forEach((b) => {
    b.addEventListener('click', () => {
      const act = b.getAttribute('data-audit-action');
      if (act === 'refresh') {
        notify('已刷新审计数据（mock）');
        render();
      } else if (act === 'trace') {
        notify('完整链路视图（即将上线）');
      } else if (act === 'config-rules') {
        notify('审计规则配置（即将上线）');
      }
    });
  });

  // 兼容历史：data-refresh 旧按钮（行为 / 会话页内仍可能存在）
  document.querySelectorAll<HTMLButtonElement>('[data-refresh]').forEach((b) => {
    b.addEventListener('click', () => notify('已刷新（mock）'));
  });

  const resultSel = document.getElementById('audit-behavior-result') as HTMLSelectElement | null;
  resultSel?.addEventListener('change', () => {
    behaviorFilter = (resultSel.value as 'all' | ActionResult) || 'all';
    updateBehaviorList();
  });

  const search = document.getElementById('audit-behavior-search') as HTMLInputElement | null;
  search?.addEventListener('input', () => {
    searchQ = search.value;
    updateBehaviorList();
  });

  document.querySelectorAll<HTMLElement>('.audit-table--behavior .audit-row[data-behavior-id]').forEach((row) => {
    row.addEventListener('click', () => {
      selectedBehaviorId = row.getAttribute('data-behavior-id');
      updateBehaviorSelection();
    });
  });

  document.querySelectorAll<HTMLButtonElement>('[data-session-id]').forEach((c) => {
    c.addEventListener('click', () => {
      const sessions = effectiveSessions();
      selectedSessionId = c.getAttribute('data-session-id') || (sessions[0]?.id ?? '');
      updateSessionSelection();
    });
  });

  bindPagination('audit-behavior', {
    onPageChange: (page) => {
      behaviorPage = page;
      render();
    },
    onPageSizeChange: (size) => {
      behaviorPageSize = size;
      behaviorPage = 1;
      render();
    },
  });

  bindPagination('audit-session', {
    onPageChange: (page) => {
      sessionPage = page;
      render();
    },
    onPageSizeChange: (size) => {
      sessionPageSize = size;
      sessionPage = 1;
      render();
    },
  });

  bindAuditColumnResize();
  bindAuditTrendHover();
}

/** 表头拖拽调整列宽（行为审计表），通过 colgroup 同步列宽并限制上下限。 */
function bindAuditColumnResize(): void {
  const table = document.querySelector('#audit-tab .audit-table') as HTMLTableElement | null;
  if (!table) return;

  table.querySelectorAll<HTMLElement>('.audit-col-grip').forEach((grip) => {
    grip.addEventListener('mousedown', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const colIdx = Number(grip.getAttribute('data-col') ?? '0');
      const th = grip.closest('th') as HTMLTableCellElement | null;
      const col = table.querySelector<HTMLTableColElement>(`colgroup col[data-col="${colIdx}"]`);
      if (!th || !col) return;
      const bounds = AUDIT_COL_BOUNDS[colIdx] ?? { min: 72, max: 240 };
      const startX = e.clientX;
      const startW = th.offsetWidth;
      grip.classList.add('is-dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMove = (ev: MouseEvent) => {
        const w = Math.min(bounds.max, Math.max(bounds.min, startW + ev.clientX - startX));
        col.style.width = `${w}px`;
      };
      const onUp = () => {
        grip.classList.remove('is-dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    });
  });
}

/** 审计趋势图柱体悬停提示。 */
function bindAuditTrendHover(): void {
  const wrap = document.querySelector('#audit-tab .audit-trend-v2--chart');
  const svg = wrap?.querySelector('svg.audit-trend-svg');
  const tip = wrap?.querySelector('.audit-trend-svg__tooltip') as HTMLElement | null;
  if (!wrap || !svg || !tip) return;

  const hide = () => {
    tip.hidden = true;
    svg.querySelectorAll('.audit-trend-svg__group.is-hover').forEach((g) => g.classList.remove('is-hover'));
  };

  svg.querySelectorAll<SVGGElement>('.audit-trend-svg__group').forEach((group) => {
    group.addEventListener('mouseenter', () => {
      const day = group.getAttribute('data-day') ?? '';
      const pass = group.getAttribute('data-pass') ?? '0';
      const block = group.getAttribute('data-block') ?? '0';
      const total = group.getAttribute('data-total') ?? '0';
      group.classList.add('is-hover');
      const dayLabel = document.createElement('strong');
      dayLabel.textContent = day;
      const totalLabel = document.createElement('span');
      totalLabel.textContent = `合计 ${total}`;
      const passLabel = document.createElement('span');
      passLabel.textContent = `已放行 ${pass}`;
      const blockLabel = document.createElement('span');
      blockLabel.textContent = `已拦截 ${block}`;
      tip.replaceChildren(dayLabel, totalLabel, passLabel, blockLabel);
      tip.hidden = false;
    });
    group.addEventListener('mousemove', (ev) => {
      const rect = wrap.getBoundingClientRect();
      tip.style.left = `${ev.clientX - rect.left + 12}px`;
      tip.style.top = `${ev.clientY - rect.top - 8}px`;
    });
    group.addEventListener('mouseleave', hide);
  });

  wrap.addEventListener('mouseleave', hide);
}

function updateBehaviorList(): void {
  behaviorPage = 1;
  render();
}

function updateBehaviorSelection(): void {
  document.querySelectorAll<HTMLElement>('.audit-row').forEach((el) => {
    el.classList.toggle('is-selected', el.getAttribute('data-behavior-id') === selectedBehaviorId);
  });
  const pane = document.querySelector('.audit-layout');
  if (!pane) return;
  const old = pane.querySelector('.audit-detail-pane');
  if (old) {
    const tmp = document.createElement('div');
    tmp.innerHTML = behaviorDetail();
    const next = tmp.firstElementChild;
    if (next) old.replaceWith(next);
  }
}

async function updateSessionSelection(): Promise<void> {
  document.querySelectorAll<HTMLButtonElement>('.audit-session-card').forEach((c) => {
    c.classList.toggle('is-selected', c.getAttribute('data-session-id') === selectedSessionId);
  });
  const grid = document.querySelector('.audit-session-grid');
  if (!grid) return;
  const old = grid.querySelector('.audit-session-detail');
  if (old) {
    const tmp = document.createElement('div');
    tmp.innerHTML = renderSessionDetail();
    const next = tmp.firstElementChild;
    if (next) old.replaceWith(next);
  }
}

export function renderAuditPage(): void {
  render();
}

export function bindAuditTab(onTab: () => void): void {
  document.querySelector('[data-tab="audit"]')?.addEventListener('click', () => {
    onTab();
    render();
    render();
  });
}

let _refreshDebounceTimer: ReturnType<typeof setTimeout> | null = null;
const REFRESH_DEBOUNCE_MS = 1000;

/**
 * 由 WebSocket 事件驱动调用（chat-controller 收到 audit_updated 时触发）。
 * 仅在审计页可见时才刷新，且 1 秒内多次调用只触发一次。
 */
export function onAuditDataChanged(): void {
  if (!$('#audit-page-root')) return;
  if (_refreshDebounceTimer) clearTimeout(_refreshDebounceTimer);
  _refreshDebounceTimer = setTimeout(() => {
    _refreshDebounceTimer = null;
    render();
  }, REFRESH_DEBOUNCE_MS);
}

export async function initAuditPage(): Promise<void> {
  render();
}
