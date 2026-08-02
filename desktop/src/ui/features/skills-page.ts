/**
 * 技能页 v3
 *
 * - 一级 Tab：技能 | 插件；技能下二级：已安装 | 可安装
 * - 分类筛选条 + 搜索同行 + 竖向卡片网格（无顶部三列摘要卡）
 * - 技能描述来自后端 SKILL.md frontmatter（非前端手写）
 * - 技能卡详情弹窗，支持安装/卸载
 */

import DOMPurify from 'dompurify';

import { backendApi, type PluginItem, type Skill, type SkillStore } from '../backend-client';
import { $, $$, escapeHtml, notify } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import { invalidateSkills } from './skill-store';

type PageTab = 'skills' | 'plugins';

/** 技能 Tab 下的二级视图：已安装 vs 仓库自带的可安装技能。 */
type SkillSubview = 'installed' | 'available';

type SkillStatus = 'builtin' | 'installed' | 'available';

interface SkillViewItem {
  slug: string;
  name: string;
  description: string;
  category: string;
  source: Skill['source'] | 'optional' | 'local';
  status: SkillStatus;
  canInstall: boolean;
  canUninstall: boolean;
  tone: string;
  badges: ('featured' | 'new' | 'builtin' | 'local')[];
  aliases?: string[];
}

const SKILL_TONES = ['blue', 'violet', 'cyan', 'amber', 'green', 'rose', 'indigo', 'orange'] as const;

let store: SkillStore | null = null;
let plugins: PluginItem[] = [];
/** 本地筛选防抖：避免每个字符都整页重绘导致输入卡顿。 */
let localSearchTimer: number | null = null;
/** 最近一次渲染的 skill 卡片快照。 */
let lastSkillItems: SkillViewItem[] = [];
/** 安装/卸载进行中标志：防重复点击。 */
let installing = false;
let togglingPlugin: string | null = null;
let pageTab: PageTab = 'skills';
let skillSubview: SkillSubview = 'installed';
let searchQ = '';
let category = '全部';
let modalSkill: SkillViewItem | null = null;
let modalPlugin: PluginItem | null = null;
/** 弹窗滚动记忆：打开弹窗时记录列表 scrollTop，关闭弹窗全量重建后恢复，避免滚动条跳回顶部。 */
let modalScrollMemory: number | null = null;
let escListenerAttached = false;
/** 技能分类栏是否处于展开状态（分类过多时折叠为单行）。 */
let categoryRailExpanded = false;
/** 自进化配置 */
let evolutionConfig = { auto_trigger: false, auto_full_cycle: false, visible: false };

const SKILL_CATEGORY_MEMORY_KEY = 'crew.skill.category-by-slug';

/** 安装后 API 若暂未带回 category，用安装前的分类记忆兜底（slug → 分类名）。 */
const skillCategoryMemory = loadSkillCategoryMemory();

function loadSkillCategoryMemory(): Map<string, string> {
  try {
    const raw = localStorage.getItem(SKILL_CATEGORY_MEMORY_KEY);
    if (!raw) return new Map();
    const parsed = JSON.parse(raw) as Record<string, string>;
    return new Map(Object.entries(parsed));
  } catch {
    return new Map();
  }
}

function persistSkillCategoryMemory(): void {
  try {
    localStorage.setItem(
      SKILL_CATEGORY_MEMORY_KEY,
      JSON.stringify(Object.fromEntries(skillCategoryMemory)),
    );
  } catch {
    /* quota / private mode */
  }
}

function rememberSkillCategory(slug: string, cat: string | undefined): void {
  const normalized = (cat || '通用办公').trim() || '通用办公';
  skillCategoryMemory.set(slug, normalized);
  persistSkillCategoryMemory();
}

function resolveSkillCategory(slug: string, apiCategory: string | undefined): string {
  const fromApi = apiCategory?.trim();
  if (fromApi) {
    rememberSkillCategory(slug, fromApi);
    return fromApi;
  }
  return skillCategoryMemory.get(slug) || '通用办公';
}

/** 当前分类筛选项下是否还有技能；无则回退到「全部」。 */
function reconcileCategorySelection(skillItems: SkillViewItem[]): void {
  if (category === '全部') return;
  const pool = itemsForSubview(skillItems);
  const count = pool.filter((s) => {
    const cat = s.category?.trim() || '通用办公';
    return cat === category;
  }).length;
  if (count === 0) category = '全部';
}

function toneFor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i += 1) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return SKILL_TONES[h % SKILL_TONES.length];
}

/** Skill 徽章首字：中文名优先取首个汉字，其他名称取首个字母并大写。 */
export function skillInitial(name: string): string {
  const normalized = name.trim();
  const han = normalized.match(/\p{Script=Han}/u)?.[0];
  if (han) return han;
  const letter = normalized.match(/\p{L}/u)?.[0];
  return letter ? letter.toLocaleUpperCase() : 'S';
}

function skillBadge(name: string, className = ''): string {
  return `<span class="skill-letter-avatar${className ? ` ${className}` : ''}" aria-hidden="true">
    <svg class="skill-letter-avatar__book" viewBox="0 0 240 240" focusable="false">
      <use href="./book.svg#book-icon"></use>
    </svg>
    <span class="skill-letter-avatar__text">${escapeHtml(skillInitial(name))}</span>
  </span>`;
}

function pluginBadge(name: string, className = ''): string {
  return `<span class="plugin-letter-avatar${className ? ` ${className}` : ''}" aria-hidden="true">
    <svg class="plugin-letter-avatar__puzzle" viewBox="0 0 240 240" focusable="false">
      <use href="./plugin.svg#plugin-icon"></use>
    </svg>
    <span class="plugin-letter-avatar__text">${escapeHtml(skillInitial(name))}</span>
  </span>`;
}

function buildSkillItems(data: SkillStore): SkillViewItem[] {
  const items: SkillViewItem[] = [];
  for (const s of data.installed ?? []) {
    const isBuiltin = s.source === 'builtin';
    items.push({
      slug: s.slug,
      name: s.display_name || s.name,
      description: s.description_zh || s.description,
      category: resolveSkillCategory(s.slug, s.category),
      source: s.source,
      status: isBuiltin ? 'builtin' : 'installed',
      canInstall: false,
      canUninstall: !isBuiltin,
      tone: toneFor(s.slug),
      badges: isBuiltin ? ['builtin', 'featured'] : [],
      aliases: s.aliases ?? [],
    });
  }
  for (const o of data.optional ?? []) {
    rememberSkillCategory(o.slug, o.category);
    items.push({
      slug: o.slug,
      name: o.display_name || o.name,
      description: o.description_zh || o.description,
      category: o.category || '通用办公',
      source: 'optional',
      status: 'available',
      canInstall: true,
      canUninstall: false,
      tone: toneFor(o.slug),
      badges: ['new'],
      aliases: o.aliases ?? [],
    });
  }
  for (const o of data.local ?? []) {
    rememberSkillCategory(o.slug, o.category);
    items.push({
      slug: o.slug,
      name: o.display_name || o.name,
      description: o.description_zh || o.description,
      category: o.category || '通用办公',
      source: 'local',
      status: 'available',
      canInstall: true,
      canUninstall: false,
      tone: toneFor(o.slug),
      badges: ['local'],
      aliases: o.aliases ?? [],
    });
  }
  return items;
}

/** 按二级视图切分技能池：已安装含内置与用户目录；可安装来自 optional-skills。 */
function itemsForSubview(items: SkillViewItem[]): SkillViewItem[] {
  if (skillSubview === 'installed') {
    return items.filter((s) => s.status === 'builtin' || s.status === 'installed');
  }
  return items.filter((s) => s.source === 'optional' || s.source === 'local');
}

function filterSkills(items: SkillViewItem[]): SkillViewItem[] {
  const pool = itemsForSubview(items);
  return pool.filter((s) => {
    if (category !== '全部') {
      if (s.category !== category) return false;
    }
    if (!searchQ) return true;
    const q = searchQ.toLowerCase();
    const aliasText = (s.aliases ?? []).join(' ');
    return `${s.name} ${s.description} ${s.category} ${s.slug} ${aliasText}`.toLowerCase().includes(q);
  });
}

function filterPlugins(items: PluginItem[]): PluginItem[] {
  if (!searchQ) return items;
  const q = searchQ.toLowerCase();
  return items.filter(
    (p) =>
      `${p.label} ${p.name} ${p.description} ${p.kind}`.toLowerCase().includes(q),
  );
}

function pluginEnabled(plugin: PluginItem): boolean {
  return plugin.effective_enabled ?? plugin.enabled;
}

function statusLabel(s: SkillViewItem): string {
  if (s.status === 'builtin') return '内置';
  if (s.status === 'installed') return '已安装';
  return '可安装';
}

function skillCard(s: SkillViewItem): string {
  // 分类标签按 category 取色（而非卡片 slug tone），保证相同分类颜色统一。
  const catTone = toneFor(s.category);
  const badgeHtml = s.badges
    .map((b) => {
      if (b === 'featured') return '<span class="skill-card-v3__badge skill-card-v3__badge--featured">推荐</span>';
      if (b === 'new') return '<span class="skill-card-v3__badge skill-card-v3__badge--new">新</span>';
      if (b === 'builtin') return '<span class="skill-card-v3__badge skill-card-v3__badge--builtin">内置</span>';
      if (b === 'local') return '<span class="skill-card-v3__badge skill-card-v3__badge--local">本地</span>';
      return '';
    })
    .join('');
  return `
    <article class="skill-card-v3 skill-card-v3--${s.tone}" data-skill-slug="${escapeHtml(s.slug)}">
      <div class="skill-card-v3__top">
        <div class="skill-card-v3__icon-wrap">${skillBadge(s.name)}</div>
        ${
          s.canInstall
            ? `<button type="button" class="skill-card-v3__action" data-install="${escapeHtml(s.slug)}" aria-label="安装 ${escapeHtml(s.name)}">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
                <span>安装</span>
              </button>`
            : s.canUninstall
              ? `<button type="button" class="skill-card-v3__action skill-card-v3__action--muted" data-uninstall="${escapeHtml(s.slug)}" aria-label="卸载 ${escapeHtml(s.name)}">
                  <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/></svg>
                  <span>卸载</span>
                </button>`
              : `<span class="skill-card-v3__action skill-card-v3__action--locked" title="内置技能">
                  <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
                  <span>内置</span>
                </span>`
        }
      </div>
      <div class="skill-card-v3__name-row">
        <div class="skill-card-v3__name">${escapeHtml(s.name)}</div>
        ${badgeHtml ? `<div class="skill-card-v3__badges">${badgeHtml}</div>` : ''}
      </div>
      <p class="skill-card-v3__desc">${escapeHtml(s.description)}</p>
      <div class="skill-card-v3__footer">
        <span class="skill-card-v3__tag skill-card-v3__tag--${catTone}">${escapeHtml(s.category)}</span>
        <span class="skill-card-v3__meta">${escapeHtml(statusLabel(s))}</span>
      </div>
    </article>
  `;
}

function pluginCard(p: PluginItem): string {
  const tone = toneFor(p.name);
  const enabled = pluginEnabled(p);
  return `
    <article class="skill-card-v3 skill-card-v3--plugin skill-card-v3--${tone}" data-plugin-name="${escapeHtml(p.name)}">
      <div class="skill-card-v3__top">
        <div class="skill-card-v3__icon-wrap skill-card-v3__icon-wrap--plugin">
          ${pluginBadge(p.label || p.name)}
        </div>
        <span class="skill-card-v3__plugin-state${enabled ? ' is-on' : ''}">${enabled ? '已启用' : '未启用'}</span>
      </div>
      <div class="skill-card-v3__name-row">
        <div class="skill-card-v3__name">${escapeHtml(p.label || p.name)}</div>
        <span class="skill-card-v3__badge skill-card-v3__badge--plugin">插件</span>
      </div>
      <p class="skill-card-v3__desc">${escapeHtml(p.description || 'Crew 运行时插件')}</p>
      <div class="skill-card-v3__footer">
        <span class="skill-card-v3__tag">${escapeHtml(p.kind || 'standalone')}</span>
        <span class="skill-card-v3__meta">v${escapeHtml(p.version || '0')}</span>
      </div>
    </article>
  `;
}

function renderSkillModal(): string {
  if (!modalSkill) return '';
  const s = modalSkill;
  const usageHint =
    s.status === 'builtin'
      ? '内置技能随 Agent 启动加载，对话中发送 /' + s.slug + ' 即可激活。'
      : s.status === 'installed'
        ? '已安装到用户目录，发送 /' + s.slug + ' 激活；可在下方卸载。'
        : '安装后复制到用户 skills 目录，即可通过 /' + s.slug + ' 调用。';
  let cta = '';
  if (s.canInstall) {
    cta = `<button type="button" class="btn-v2 btn-v2--primary btn-v2--lg skill-modal__cta" data-modal-install="${escapeHtml(s.slug)}">安装 ${escapeHtml(s.name)}</button>`;
  } else if (s.canUninstall) {
    cta = `
      <div class="skill-modal__uninstall-note">卸载后本地技能目录中的文件将被移除，对话中无法再使用 <code>/${escapeHtml(s.slug)}</code>。</div>
      <button type="button" class="btn-v2 btn-v2--lg skill-modal__cta skill-modal__cta--danger" data-modal-uninstall="${escapeHtml(s.slug)}">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
        卸载 ${escapeHtml(s.name)}
      </button>`;
  } else {
    cta = `<button type="button" class="btn-v2 btn-v2--primary btn-v2--lg skill-modal__cta" data-modal-use="${escapeHtml(s.slug)}">使用 /${escapeHtml(s.slug)}</button>`;
  }
  return `
    <div class="skill-modal__overlay" data-modal-close>
      <div class="skill-modal skill-card-v3--${s.tone}" data-modal-content>
        <button type="button" class="skill-modal__close" data-modal-close aria-label="关闭">×</button>
        <div class="skill-modal__head">
          <div class="skill-modal__avatar">${skillBadge(s.name, 'skill-letter-avatar--large')}</div>
          <div class="skill-modal__head-copy">
            <div class="skill-modal__title-row">
              <h2 class="skill-modal__name">${escapeHtml(s.name)}</h2>
              <span class="skill-modal__head-tag">${escapeHtml(statusLabel(s))}</span>
            </div>
            <div class="skill-modal__slug">/${escapeHtml(s.slug)}</div>
          </div>
        </div>
        <div class="skill-modal__body">
          <div class="skill-modal__section">
            <div class="skill-modal__section-title">能力介绍</div>
            <div class="skill-modal__prose">${escapeHtml(s.description)}</div>
          </div>
          <div class="skill-modal__section">
            <div class="skill-modal__section-title">分类</div>
            <div class="skill-modal__chips">
              <span class="skill-modal__chip">${escapeHtml(s.category)}</span>
              <span class="skill-modal__chip">Skill</span>
            </div>
          </div>
          <div class="skill-modal__section">
            <div class="skill-modal__section-title">使用说明</div>
            <div class="skill-modal__prose skill-modal__prose--muted">${escapeHtml(usageHint)}</div>
          </div>
          <div class="skill-modal__cta-row">${cta}</div>
        </div>
      </div>
    </div>
  `;
}

function renderPluginModal(): string {
  if (!modalPlugin) return '';
  const p = modalPlugin;
  const key = p.key || p.name;
  const enabled = pluginEnabled(p);
  const toggleVisible = Boolean(p.toggle_endpoint);
  const toggleAllowed = p.system_allowed !== false && p.role_allowed !== false;
  const togglePending = togglingPlugin === key;
  const tone = toneFor(p.name);
  const blockedReason = !p.system_allowed
    ? '已被系统策略禁用'
    : !p.role_allowed
      ? '当前账号角色未获授权'
      : '';
  return `
    <div class="skill-modal__overlay" data-modal-close>
      <div class="skill-modal skill-card-v3--plugin skill-card-v3--${tone}" data-modal-content>
        <button type="button" class="skill-modal__close" data-modal-close aria-label="关闭">×</button>
        <div class="skill-modal__head">
          <div class="skill-modal__avatar skill-modal__avatar--plugin">${pluginBadge(p.label || p.name, 'plugin-letter-avatar--large')}</div>
          <div class="skill-modal__head-copy">
            <div class="skill-modal__title-row">
              <h2 class="skill-modal__name">${escapeHtml(p.label || p.name)}</h2>
              <span class="skill-modal__head-tag">插件</span>
            </div>
            <div class="skill-modal__slug">${escapeHtml(p.name)} · v${escapeHtml(p.version || '0')}</div>
          </div>
        </div>
        <div class="skill-modal__body">
          <div class="skill-modal__section">
            <div class="skill-modal__section-title">插件说明</div>
            <div class="skill-modal__prose">${escapeHtml(p.description || '扩展 Agent 运行时能力（工具、Hook、平台通道等）。')}</div>
          </div>
          <div class="skill-modal__section">
            <div class="skill-modal__section-title">运行状态</div>
            <div class="skill-modal__chips">
              <span class="skill-modal__chip">${enabled ? '已启用' : '未启用'}</span>
              <span class="skill-modal__chip">${escapeHtml(p.kind || 'standalone')}</span>
            </div>
          </div>
          ${
            toggleVisible
              ? `<div class="skill-modal__section plugin-toggle-row">
                  <div>
                    <div class="skill-modal__section-title">账号开关</div>
                    <div class="skill-modal__prose skill-modal__prose--muted">${escapeHtml(blockedReason || (enabled ? '关闭后立即撤销能力并停止当前账号的浏览器自动化。' : '开启后，下一轮任务即可使用该插件能力。'))}</div>
                  </div>
                  <label class="plugin-toggle${enabled ? ' is-on' : ''}${togglePending ? ' is-pending' : ''}" aria-label="${enabled ? '禁用' : '启用'} ${escapeHtml(p.label || p.name)}">
                    <input type="checkbox" data-plugin-toggle="${escapeHtml(key)}"${enabled ? ' checked' : ''}${!toggleAllowed || togglePending ? ' disabled' : ''}>
                    <span class="plugin-toggle__track"><span class="plugin-toggle__thumb"></span></span>
                  </label>
                </div>`
              : ''
          }
          ${
            p.tools.length
              ? `<div class="skill-modal__section"><div class="skill-modal__section-title">注册工具</div><div class="skill-modal__chips">${p.tools.map((t) => `<span class="skill-modal__chip">${escapeHtml(t)}</span>`).join('')}</div></div>`
              : ''
          }
          ${
            p.error
              ? `<div class="skill-modal__section"><div class="skill-modal__section-title">状态</div><div class="skill-modal__prose skill-modal__prose--warn">${escapeHtml(p.error)}</div></div>`
              : ''
          }
        </div>
      </div>
    </div>
  `;
}

function renderFieldChips(skillItems: SkillViewItem[]): string {
  const pool = itemsForSubview(skillItems);
  const catCounts = new Map<string, number>();

  for (const s of pool) {
    const cat = s.category?.trim() || '通用办公';
    catCounts.set(cat, (catCounts.get(cat) ?? 0) + 1);
  }

  const sortedCats = Array.from(catCounts.keys()).sort((a, b) => {
    if (a === '通用办公') return -1;
    if (b === '通用办公') return 1;
    return a.localeCompare(b, 'zh-CN');
  });

  return ['全部', ...sortedCats]
    .map(
      (cat) =>
        `<button type="button" class="hub-chip${cat === category ? ' is-active' : ''}" data-cat="${escapeHtml(cat)}">${escapeHtml(cat)}<span class="hub-chip__count">${cat === '全部' ? pool.length : (catCounts.get(cat) ?? 0)}</span></button>`,
    )
    .join('');
}

function renderSkillSubview(skillItems: SkillViewItem[]): string {
  const installedCount = skillItems.filter((s) => s.status === 'builtin' || s.status === 'installed').length;
  const availableCount = skillItems.filter((s) => s.status === 'available').length;
  return `
    <nav class="hub-sort skill-subnav" aria-label="技能视图">
      <button type="button" class="hub-sort__btn${skillSubview === 'installed' ? ' is-active' : ''}" data-skill-view="installed">
        已安装
        <span class="skill-subnav__count">${installedCount}</span>
      </button>
      <button type="button" class="hub-sort__btn${skillSubview === 'available' ? ' is-active' : ''}" data-skill-view="available">
        可安装
        <span class="skill-subnav__count">${availableCount}</span>
      </button>
    </nav>
  `;
}

function renderSkillSearch(placeholder: string): string {
  return `
    <div class="skill-search skill-search--bar">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
      <input type="text" id="skills-search-input" placeholder="${placeholder}" value="${escapeHtml(searchQ)}" />
    </div>
  `;
}

/** 技能列表网格（含空态），供整页渲染与搜索局部刷新复用。 */
function renderSkillsGridHtml(skillItems: SkillViewItem[], filteredSkills: SkillViewItem[]): string {
  const pool = itemsForSubview(skillItems);
  const availableEmpty = skillSubview === 'available' && pool.length === 0;
  const emptyTitle = skillSubview === 'available'
    ? availableEmpty
      ? '暂无可安装技能'
      : '当前筛选下没有技能'
    : '暂无已安装技能';
  const emptyDesc =
    skillSubview === 'available'
      ? availableEmpty
        ? '仓库当前没有提供 optional-skills，仍可手动添加技能到用户 skills 目录。'
        : searchQ
          ? '没有匹配搜索关键词的技能，试试其他关键词。'
          : '试试切换分类筛选。'
      : '前往「可安装」安装项目随附的技能，或手动添加技能。';
  return filteredSkills.length
    ? `<div class="skill-grid-v3">${filteredSkills.map(skillCard).join('')}</div>`
    : `<div class="v2-empty"><div class="v2-empty__icon">∅</div><div class="v2-empty__title">${emptyTitle}</div><div class="v2-empty__desc">${emptyDesc}</div></div>`;
}

function renderPluginsGridHtml(filteredPlugins: PluginItem[]): string {
  return filteredPlugins.length
    ? `<div class="skill-grid-v3">${filteredPlugins.map(pluginCard).join('')}</div>`
    : `<div class="v2-empty"><div class="v2-empty__icon">∅</div><div class="v2-empty__title">暂无插件数据</div><div class="v2-empty__desc">请确认服务已启动；插件在 plugins/ 目录由 crew 扫描加载。</div></div>`;
}

/** 仅刷新搜索结果区域，不重建搜索框，避免输入卡顿丢焦点。 */
function refreshSearchResults(): void {
  const host = $('#skills-search-results');
  if (!host) {
    renderShell();
    return;
  }

  const skillItems = buildSkillItems(store ?? { installed: [], optional: [] });
  lastSkillItems = skillItems;
  reconcileCategorySelection(skillItems);

  if (pageTab === 'skills') {
    const filteredSkills = filterSkills(skillItems);
    host.innerHTML = DOMPurify.sanitize(`
      <div class="skill-grid-wrap">${renderSkillsGridHtml(skillItems, filteredSkills)}</div>
    `);
  } else {
    const filteredPlugins = filterPlugins(plugins);
    host.innerHTML = DOMPurify.sanitize(
      `<div class="skill-grid-wrap">${renderPluginsGridHtml(filteredPlugins)}</div>`,
    );
  }

  bindResultListEvents(skillItems);
}

/** 根据分类栏实际高度决定是否显示「展开/收起」按钮。 */
function updateCategoryRailToggle(): void {
  const wrap = $('.skill-chip-rail-wrap');
  const rail = $('#skill-category-rail');
  if (!wrap || !rail) return;
  // 必须在折叠状态下测量，否则展开态 max-height 很大，scrollHeight 与 clientHeight 会相等，
  // 导致误判为不需要折叠按钮。
  const wasExpanded = rail.classList.contains('is-expanded');
  if (wasExpanded) rail.classList.remove('is-expanded');
  const collapsable = rail.scrollHeight > rail.clientHeight;
  if (wasExpanded) rail.classList.add('is-expanded');
  wrap.classList.toggle('is-collapsable', collapsable);
  if (!collapsable) {
    categoryRailExpanded = false;
    rail.classList.remove('is-expanded');
    const btn = $('[data-toggle-cat-rail]');
    if (btn) {
      btn.classList.remove('is-expanded');
      btn.setAttribute('aria-expanded', 'false');
      const span = btn.querySelector('span');
      if (span) span.textContent = '展开';
    }
  }
}

function renderShell(): void {
  const root = $('#skills-page-root');
  if (!root) return;

  const skillItems = buildSkillItems(store ?? { installed: [], optional: [] });
  lastSkillItems = skillItems;
  reconcileCategorySelection(skillItems);
  const filteredSkills = filterSkills(skillItems);
  const filteredPlugins = filterPlugins(plugins);

  const tabs = `
    <div class="hub-segment skill-tabs">
      <button type="button" class="hub-segment__item${pageTab === 'skills' ? ' is-active' : ''}" data-page-tab="skills">
        技能
        <span class="hub-segment__count">${skillItems.length}</span>
      </button>
      <button type="button" class="hub-segment__item${pageTab === 'plugins' ? ' is-active' : ''}" data-page-tab="plugins">
        插件
        <span class="hub-segment__count">${plugins.length}</span>
      </button>
    </div>
  `;

  let body = '';
  if (pageTab === 'skills') {
    const grid = renderSkillsGridHtml(skillItems, filteredSkills);
    body = `
      <div class="skill-filter-bar hub-filter-row">
        <div class="skill-chip-rail-wrap">
          <div id="skill-category-rail" class="hub-chip-rail skill-fields--scroll skill-fields--foldable${categoryRailExpanded ? ' is-expanded' : ''}">${renderFieldChips(skillItems)}</div>
          <button type="button" class="skill-chip-rail__toggle${categoryRailExpanded ? ' is-expanded' : ''}" data-toggle-cat-rail aria-expanded="${categoryRailExpanded}" aria-controls="skill-category-rail">
            <span>${categoryRailExpanded ? '收起' : '展开'}</span>
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
          </button>
        </div>
        ${renderSkillSearch(skillSubview === 'available' ? '搜索可安装技能' : '搜索已安装技能')}
      </div>
      <div id="skills-search-results">
        <div class="skill-grid-wrap">${grid}</div>
      </div>
    `;
  } else {
    const grid = renderPluginsGridHtml(filteredPlugins);
    body = `
      <div class="skill-filter-bar">
        <p class="skill-plugin-hint">插件扩展工具、Hook 与平台通道；启用规则见 config.yaml 的 plugins 配置。</p>
        ${renderSkillSearch('搜索插件')}
      </div>
      <div id="skills-search-results">
        <div class="skill-grid-wrap">${grid}</div>
      </div>
    `;
  }

  root.innerHTML = `
    <div class="page-shell page-shell--skills">
      <header class="page-header page-header--hub skill-topbar">
        <div class="page-header__copy">
          <h1 class="page-header__title skill-topbar__title">技能与插件</h1>
          <p class="page-header__desc">管理项目随附的技能与运行时插件。安装技能后，可在对话中输入 /技能名 调用。</p>
        </div>
        <div class="page-header__actions">
          <div class="evolution-toggle-row">
            <span class="evolution-toggle__label">自进化</span>
            <label class="plugin-toggle${(evolutionConfig.auto_trigger && evolutionConfig.auto_full_cycle) ? ' is-on' : ''}${togglingEvolution ? ' is-pending' : ''}" aria-label="自进化开关">
              <input type="checkbox" data-evolution-toggle${(evolutionConfig.auto_trigger && evolutionConfig.auto_full_cycle) ? ' checked' : ''}${togglingEvolution ? ' disabled' : ''}>
              <span class="plugin-toggle__track"><span class="plugin-toggle__thumb"></span></span>
            </label>
          </div>
          <button type="button" class="hub-refresh-btn" data-refresh title="刷新">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/></svg>
            刷新
          </button>
        </div>
      </header>
      <div class="skill-toolbar hub-toolbar-row">
        ${tabs}
        <div class="hub-toolbar-row__tail">${pageTab === 'skills' ? renderSkillSubview(skillItems) : ''}</div>
      </div>
      ${body}
    </div>
    ${renderSkillModal()}
    ${renderPluginModal()}
  `;
  bindEvents(skillItems);
  updateCategoryRailToggle();
}

async function installSkill(slug: string): Promise<void> {
  if (installing) {
    notify('正在处理中，请稍候');
    return;
  }
  const item = lastSkillItems.find((s) => s.slug === slug);
  // 互斥量必须先于确认框置位：确认框是全屏遮罩，且遮罩点击被判为「取消」。若等确认后
  // 再置位，双击的第 2 次 click 会落在刚铺开的遮罩上，把自己的安装静默取消掉（且无提示）。
  installing = true;
  try {
    // 技能落盘在 get_crew_home()/skills（机器级单一目录，不带 owner 维度），审计日志
    // 也叫 global-skills-audit.jsonl——装一个技能会影响本机所有登录账号，不是「我的」
    // 账号内行为。安装前必须把这个作用域说清楚。
    const isLocal = item?.source === 'local';
    const localNote = isLocal
      ? '该技能来自本地共享目录（~/.agents/skills），将以软链方式安装，源目录更新后自动同步。'
      : '';
    const agreed = await showConfirmDialog({
      title: '确认全局安装技能',
      message:
        `技能是本机全局共享能力，安装结果对本机所有登录账号生效。${localNote}`
        + `确定安装「${item?.name || slug}」吗？`,
      confirmText: '全局安装',
      cancelText: '取消',
    });
    if (!agreed) return;
    if (item?.category) rememberSkillCategory(slug, item.category);
    const res = await backendApi.installSkill(slug);
    if (!res.ok) throw new Error('install failed');
    notify(`已安装 ${slug}`);
    closeModal();
    invalidateSkills();
    await loadStore();
  } catch {
    notify('安装失败，请确认服务已启动且 slug 有效');
  } finally {
    installing = false;
  }
}

async function uninstallSkill(slug: string): Promise<void> {
  if (installing) {
    notify('正在处理中，请稍候');
    return;
  }
  const item = lastSkillItems.find((s) => s.slug === slug);
  // 同 installSkill：互斥量必须先于全屏确认框置位，否则双击的第 2 次 click 打在遮罩上
  // 会被判为「取消」，把自己的卸载静默撤销。
  installing = true;
  try {
    // 卸载同样是机器级：不是只从「我的」账号移除，本机所有账号都会一起失去该技能。
    const ok = await showConfirmDialog({
      title: '确认全局卸载技能',
      message:
        `技能是本机全局共享能力。卸载「${item?.name || slug}」后本地技能文件将被移除，`
        + `本机所有账号都将无法再通过 /${slug} 调用。确定卸载吗？`,
      confirmText: '全局卸载',
      cancelText: '取消',
    });
    if (!ok) return;
    const res = await backendApi.uninstallSkill(slug);
    if (!res.ok) throw new Error('uninstall failed');
    notify(`已卸载 ${slug}`);
    closeModal();
    skillCategoryMemory.delete(slug);
    persistSkillCategoryMemory();
    invalidateSkills();
    await loadStore();
  } catch {
    notify('卸载失败（内置技能不可卸载）');
  } finally {
    installing = false;
  }
}

async function togglePlugin(key: string, enabled: boolean): Promise<void> {
  if (togglingPlugin) {
    notify('插件状态正在更新，请稍候');
    return;
  }
  const current = plugins.find((plugin) => (plugin.key || plugin.name) === key);
  if (!current) return;
  if (!enabled) {
    const browserNote = key === 'browser'
      ? '关闭后会立即中断在途浏览器动作、关闭当前账号标签页，并让旧 ref 和审批失效。'
      : '关闭后，下一轮任务将不再获得该插件能力。';
    const confirmed = await showConfirmDialog({
      title: `关闭${current.label || current.name}`,
      message: browserNote,
      confirmText: '关闭插件',
      cancelText: '取消',
    });
    if (!confirmed) {
      renderShell();
      return;
    }
  }
  togglingPlugin = key;
  renderShell();
  try {
    const result = await backendApi.setPluginEnabled(key, enabled);
    if (!result.ok || !result.plugin) throw new Error(result.error || '插件状态更新失败');
    plugins = plugins.map((plugin) =>
      (plugin.key || plugin.name) === key ? result.plugin : plugin,
    );
    if (modalPlugin && (modalPlugin.key || modalPlugin.name) === key) {
      modalPlugin = result.plugin;
    }
    notify(`${current.label || current.name}已${enabled ? '启用' : '关闭'}`);
  } catch (error) {
    notify((error as Error).message || '插件状态更新失败');
  } finally {
    togglingPlugin = null;
    renderShell();
  }
}

function openSkillModal(slug: string, items: SkillViewItem[]): void {
  modalScrollMemory = $('#skills-page-root')?.scrollTop ?? null;
  modalSkill = items.find((s) => s.slug === slug) ?? null;
  modalPlugin = null;
  renderShell();
}

function openPluginModal(name: string): void {
  modalScrollMemory = $('#skills-page-root')?.scrollTop ?? null;
  modalPlugin = plugins.find((p) => p.name === name) ?? null;
  modalSkill = null;
  renderShell();
}

function closeModal(): void {
  modalSkill = null;
  modalPlugin = null;
  renderShell();
  const root = $('#skills-page-root');
  if (root && modalScrollMemory != null) root.scrollTop = modalScrollMemory;
  modalScrollMemory = null;
}

let togglingEvolution = false;

async function toggleEvolution(value: boolean): Promise<void> {
  if (togglingEvolution) {
    notify('自进化设置正在更新，请稍候');
    return;
  }
  togglingEvolution = true;
  renderShell();
  try {
    const result = await backendApi.updateEvolution({
      auto_trigger: value,
      auto_full_cycle: value,
    });
    if (!result.ok || !result.evolution) {
      throw new Error('更新失败');
    }
    evolutionConfig = result.evolution;
    notify(`自进化${value ? '已开启' : '已关闭'}`);
    renderShell();
  } catch (error) {
    notify((error as Error).message || '自进化设置更新失败');
    renderShell();
  } finally {
    togglingEvolution = false;
  }
}

function bindEvents(skillItems: SkillViewItem[]): void {
  $$('[data-evolution-toggle]').forEach((el) => {
    el.addEventListener('change', () => {
      void toggleEvolution((el as HTMLInputElement).checked);
    });
  });

  $$('[data-page-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      pageTab = (btn.getAttribute('data-page-tab') as PageTab) || 'skills';
      category = '全部';
      skillSubview = 'installed';
      categoryRailExpanded = false;
      closeModal();
      renderShell();
    });
  });

  $$('[data-skill-view]').forEach((btn) => {
    btn.addEventListener('click', () => {
      skillSubview = (btn.getAttribute('data-skill-view') as SkillSubview) || 'installed';
      category = '全部';
      categoryRailExpanded = false;
      closeModal();
      if (skillSubview === 'available') {
        // 刷新 optional 列表，避免安装后切回可安装视图仍显示旧状态。
        void loadStore();
      } else {
        renderShell();
      }
    });
  });

  $$('[data-cat]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const cat = btn.getAttribute('data-cat') ?? '全部';
      category = category === cat && cat !== '全部' ? '全部' : cat;
      renderShell();
    });
  });

  $$('[data-toggle-cat-rail]').forEach((btn) => {
    btn.addEventListener('click', () => {
      categoryRailExpanded = !categoryRailExpanded;
      const rail = $('#skill-category-rail');
      if (rail) rail.classList.toggle('is-expanded', categoryRailExpanded);
      btn.classList.toggle('is-expanded', categoryRailExpanded);
      btn.setAttribute('aria-expanded', String(categoryRailExpanded));
      const span = btn.querySelector('span');
      if (span) span.textContent = categoryRailExpanded ? '收起' : '展开';
    });
  });

  const search = $('#skills-search-input') as HTMLInputElement | null;
  if (search) {
    search.addEventListener('input', () => {
      searchQ = search.value;
      // 短防抖后局部刷新列表，保留搜索框焦点。
      if (localSearchTimer) window.clearTimeout(localSearchTimer);
      localSearchTimer = window.setTimeout(() => {
        localSearchTimer = null;
        refreshSearchResults();
      }, 120);
    });
  }

  bindResultListEvents(skillItems);

  $$('[data-refresh]').forEach((btn) => {
    btn.addEventListener('click', () => {
      btn.classList.add('is-spinning');
      void loadStore().finally(() => {
        window.setTimeout(() => btn.classList.remove('is-spinning'), 500);
      });
    });
  });

  $$('[data-modal-install]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const slug = btn.getAttribute('data-modal-install');
      if (slug) void installSkill(slug);
    });
  });

  $$('[data-modal-uninstall]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const slug = btn.getAttribute('data-modal-uninstall');
      if (slug) void uninstallSkill(slug);
    });
  });

  $$('[data-modal-use]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const slug = btn.getAttribute('data-modal-use');
      closeModal();
      notify(slug ? `对话中发送 /${slug} 即可激活` : '已记录');
      document.querySelector<HTMLElement>('[data-tab="chat"]')?.click();
    });
  });

  $$('.skill-modal__overlay').forEach((overlay) => {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeModal();
    });
  });
  $$('.skill-modal__close, [data-modal-close]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeModal();
    });
  });

  if (!escListenerAttached) {
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && (modalSkill || modalPlugin)) closeModal();
    });
    escListenerAttached = true;
  }
}

/** 绑定搜索结果区（卡片 / 插件开关）事件；局部刷新列表后需重绑。 */
function bindResultListEvents(skillItems: SkillViewItem[]): void {
  $$('[data-skill-slug]').forEach((card) => {
    card.addEventListener('click', (e) => {
      if ((e.target as HTMLElement).closest('[data-install],[data-uninstall]')) return;
      const slug = card.getAttribute('data-skill-slug');
      if (slug) openSkillModal(slug, skillItems);
    });
  });

  $$('[data-plugin-name]').forEach((card) => {
    card.addEventListener('click', () => {
      const name = card.getAttribute('data-plugin-name');
      if (name) openPluginModal(name);
    });
  });

  $$('[data-plugin-toggle]').forEach((input) => {
    input.addEventListener('change', (event) => {
      event.stopPropagation();
      const control = input as HTMLInputElement;
      const key = control.getAttribute('data-plugin-toggle');
      if (key) void togglePlugin(key, control.checked);
    });
  });

  $$('[data-install]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const slug = btn.getAttribute('data-install');
      if (slug) void installSkill(slug);
    });
  });

  $$('[data-uninstall]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const slug = btn.getAttribute('data-uninstall');
      if (slug) void uninstallSkill(slug);
    });
  });
}

async function loadStore(): Promise<void> {
  try {
    const [skillData, pluginData] = await Promise.all([
      backendApi.skillStore(),
      backendApi.plugins().catch(() => [] as PluginItem[]),
    ]);
    store = skillData;
    plugins = pluginData;
    if (skillData.evolution) {
      evolutionConfig = skillData.evolution;
    }
  } catch {
    store = { installed: [], optional: [] };
    plugins = [];
  }
  renderShell();
}

export function renderSkillsPage(): void {
  renderShell();
}

export function bindSkillsTab(onTab: () => void): void {
  document.querySelector('[data-tab="skills"]')?.addEventListener('click', () => {
    onTab();
    void loadStore();
  });

  // 登录态变化时刷新当前账号可见的技能与插件。
  window.addEventListener('user:login-changed', () => {
    void loadStore();
  });

  // 窗口大小变化时重新判定分类栏是否需要折叠按钮。
  window.addEventListener('resize', updateCategoryRailToggle);
}

export async function initSkillsPage(): Promise<void> {
  await loadStore();
}
