/**
 * 场景化推荐（welcome 首页）
 *
 * - 用统一的低饱和能力列表渲染经典场景，配「换一换」随机换一批。
 * - 点场景 → 展开细分玩法 chip；点细分玩法 → 通过 onPick 把 query 预填进输入框 + 记录绑定。
 * - 绑定（skill / 注入提示词）由后端按 sub_scenario id 反查，前端只负责传 id。
 * - API 不可用或返回空列表时展示内置 fallback，避免首页卡片被清空。
 */

import { backendApi, type Scenario, type SubScenario } from '../backend-client';
import { $, escapeHtml } from '../state';

const BATCH = 3;

const FALLBACK_SCENARIOS: Scenario[] = [
  {
    id: 'fallback-fin',
    title: '金融服务',
    description: '资讯检索、财报与竞品分析',
    items: [
      { id: 'fin-report', title: '竞品财报摘要', query: '帮我分析竞品最新财报的核心要点与风险提示' },
      { id: 'fin-news', title: '行业资讯速览', query: '检索今日金融行业重要资讯并给出摘要' },
    ],
  },
  {
    id: 'fallback-doc',
    title: '文档处理',
    description: 'Markdown / Word / PDF 互转与精排',
    items: [
      { id: 'doc-md-pdf', title: 'MD 转 PDF', query: '帮我把这份 Markdown 文档转成排版精美的 PDF' },
      { id: 'doc-ppt', title: '一键生成 PPT', query: '根据大纲生成演示文稿结构与每页要点' },
    ],
  },
  {
    id: 'fallback-mail',
    title: '邮箱办公',
    description: '邮件撰写、收发与摘要',
    items: [
      { id: 'mail-draft', title: '撰写回复', query: '帮我写一封专业、简洁的邮件回复' },
      { id: 'mail-summary', title: '邮件摘要', query: '总结这封邮件的核心诉求与待办事项' },
    ],
  },
];

// Notion 简约风：只保留单色线条图形，颜色交给 CSS（currentColor）。
const CARD_GLYPHS: string[] = [
  // 1. Skill 立方体
  '<path d="M32 14 L48 22 V42 L32 50 L16 42 V22 Z" /><path d="M16 22 L32 30 L48 22" /><path d="M32 30 V50" />',
  // 2. 邮件
  '<rect x="10" y="18" width="44" height="28" rx="4" /><path d="M12 22 L32 36 L52 22" />',
  // 3. 文件夹 / 整理
  '<path d="M10 20 H24 L28 24 H54 V48 H10 Z" /><path d="M22 33 L26 37 L34 29" />',
  // 4. 日历 / 日程
  '<rect x="10" y="16" width="44" height="34" rx="4" /><path d="M10 26 H54" /><path d="M22 12 V20 M42 12 V20" /><rect x="28" y="33" width="8" height="8" rx="1.5" />',
  // 5. 公文检索
  '<path d="M16 12 H38 L48 22 V52 H16 Z" /><path d="M38 12 V22 H48" /><circle cx="36" cy="40" r="5.5" /><path d="M40 44 L46 50" />',
];

function cardSvg(index: number): string {
  const glyph = CARD_GLYPHS[index % CARD_GLYPHS.length];
  return `<svg class="card-svg" viewBox="0 0 64 64" aria-hidden="true">
    <g class="card-glyph" fill="none" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">${glyph}</g>
  </svg>`;
}

let scenarios: Scenario[] = [...FALLBACK_SCENARIOS];
let activeId: string | null = null;
let onPick: ((sub: SubScenario, parent: Scenario) => void) | null = null;

function renderCards(): void {
  const host = $('#scenario-cards');
  if (!host) return;
  host.innerHTML = scenarios
    .map((s, i) => {
      const active = s.id === activeId ? ' scenario-card--active' : '';
      return `
      <button type="button" class="feature-card${active}" data-scenario-id="${escapeHtml(s.id)}">
        <div class="card-icon-area">${cardSvg(i)}</div>
        <div class="card-text-area">
          <div class="card-title">${escapeHtml(s.title)}</div>
          <div class="card-subtitle">${escapeHtml(s.description ?? '')}</div>
        </div>
        <svg class="feature-card__chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m8 5 5 5-5 5" /></svg>
      </button>`;
    })
    .join('');
}

function renderItems(): void {
  const host = $('#scenario-items') as HTMLElement | null;
  if (!host) return;
  const active = scenarios.find((s) => s.id === activeId);
  if (!active || active.items.length === 0) {
    host.hidden = true;
    host.innerHTML = '';
    return;
  }
  host.hidden = false;
  host.innerHTML = active.items
    .map(
      (item) =>
        `<button type="button" class="scenario-item" data-sub-id="${escapeHtml(item.id)}" title="${escapeHtml(item.query)}">${escapeHtml(item.title)}</button>`,
    )
    .join('');
}

function applyScenarios(list: Scenario[]): void {
  scenarios = list.length > 0 ? list : [...FALLBACK_SCENARIOS];
  if (activeId && !scenarios.some((s) => s.id === activeId)) {
    activeId = null;
  }
  renderCards();
  renderItems();
}

function load(): void {
  backendApi
    .scenarios(BATCH)
    .then((list) => applyScenarios(list))
    .catch(() => applyScenarios([]));
}

/**
 * 绑定场景推荐交互。onPick 在用户点击某细分玩法时触发。
 */
export function bindScenarioHub(pick: (sub: SubScenario, parent: Scenario) => void): void {
  onPick = pick;

  $('#scenario-refresh')?.addEventListener('click', load);

  $('#scenario-cards')?.addEventListener('click', (e) => {
    const card = (e.target as HTMLElement).closest('[data-scenario-id]') as HTMLElement | null;
    if (!card) return;
    const id = card.getAttribute('data-scenario-id');
    activeId = activeId === id ? null : id;
    renderCards();
    renderItems();
  });

  $('#scenario-items')?.addEventListener('click', (e) => {
    const btn = (e.target as HTMLElement).closest('[data-sub-id]') as HTMLElement | null;
    if (!btn) return;
    const subId = btn.getAttribute('data-sub-id');
    const parent = scenarios.find((s) => s.id === activeId);
    const item = parent?.items.find((it) => it.id === subId);
    if (parent && item && onPick) onPick(item, parent);
  });

  renderCards();
  load();
}

/** 网关（重新）连接后刷新一批推荐。 */
export function refreshScenarioHub(): void {
  load();
}
