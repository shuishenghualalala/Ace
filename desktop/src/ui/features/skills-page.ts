/**
 * 技能页 v3
 *
 * - 一级 Tab：技能 | 插件；技能下二级：已安装 | 技能市场（切换思路参考 QClaw，视觉独立设计）
 * - 分类筛选条 + 搜索同行 + 竖向卡片网格（无顶部三列摘要卡）
 * - 技能描述来自后端 SKILL.md frontmatter（非前端手写）
 * - 技能卡详情弹窗，支持安装/卸载
 */

import { backendApi, type OptionalSkill, type PluginItem, type Skill, type SkillStore } from '../backend-client';
import { $, notify } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import {
  createCapabilityHubView,
  type CapabilityHubItem,
  type CapabilityHubState,
  type CapabilityHubView,
} from './capability-hub';
import { invalidateSkills } from './skill-store';

type PageTab = 'skills' | 'plugins';

/** 技能 Tab 下的二级视图：已安装 vs Ace/main 提供的可安装技能 */
type SkillSubview = 'installed' | 'market';

type SkillStatus = 'builtin' | 'installed' | 'available';

interface SkillViewItem {
  slug: string;
  name: string;
  description: string;
  category: string;
  source: Skill['source'] | 'optional' | 'local';
  /** 是否从本机共享 Skill 目录接入；移除时保留原始 Skill。 */
  isLocalShared?: boolean | undefined;
  status: SkillStatus;
  canInstall: boolean;
  canUninstall: boolean;
  tone: (typeof SKILL_TONES)[number];
  badges: ('featured' | 'new' | 'builtin' | 'local')[];
  aliases?: string[];
}

/**
 * backend 在 installed 上可能返回 is_local_shared（本机共享目录接入标记）；
 * backend-client 类型暂未声明，这里本地放宽以承接运行时字段。
 */
type InstalledSkill = Skill & { is_local_shared?: boolean };

/**
 * backend 在 store.local 返回 ~/.agents/skills 中未安装的本地共享技能（跨 agent 共享，软链安装）。
 * backend-client 类型暂未声明，这里本地放宽以承接运行时字段。
 */
type SkillStoreWithLocal = SkillStore & { local?: OptionalSkill[] };

const SKILL_TONES = ['blue', 'violet', 'cyan', 'amber', 'green', 'rose', 'indigo', 'orange'] as const;

let store: SkillStore | null = null;
let plugins: PluginItem[] = [];
let navigateToChat: (() => void) | null = null;
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
let capabilityHubView: CapabilityHubView | null = null;
let skillsLoading = false;
let togglingEvolution = false;
/** 自进化配置 */
let evolutionConfig = { auto_trigger: false, auto_full_cycle: false, visible: false };

const SKILL_CATEGORY_MEMORY_KEY = 'crew.skill.category-by-slug';

/** 安装后 API 若暂未带回 category，用市场侧记忆兜底（slug → 分类名）。 */
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

function toneFor(seed: string): (typeof SKILL_TONES)[number] {
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

function buildSkillItems(data: SkillStore): SkillViewItem[] {
  const items: SkillViewItem[] = [];
  for (const s of data.installed ?? []) {
    const installed = s as InstalledSkill;
    const isBuiltin = s.source === 'builtin';
    items.push({
      slug: s.slug,
      name: s.display_name || s.name,
      description: s.description_zh || s.description,
      category: resolveSkillCategory(s.slug, s.category),
      source: s.source,
      isLocalShared: installed.is_local_shared,
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
  for (const o of (data as SkillStoreWithLocal).local ?? []) {
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

/** 按二级视图切分技能池：已安装含内置与用户目录；可安装来自 optional-skills 与本地共享。 */
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

function capabilityCategories(skillItems: SkillViewItem[]): CapabilityHubState['categories'] {
  const pool = itemsForSubview(skillItems);
  const counts = new Map<string, number>();
  for (const item of pool) {
    const name = item.category?.trim() || '通用办公';
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  const entries = [...counts.entries()].sort(([left], [right]) => {
    if (left === '通用办公') return -1;
    if (right === '通用办公') return 1;
    return left.localeCompare(right, 'zh-CN');
  });
  return [
    { id: '全部', label: '全部', count: pool.length },
    ...entries.map(([name, count]) => ({ id: name, label: name, count })),
  ];
}

function skillCapability(item: SkillViewItem): CapabilityHubItem {
  return {
    id: item.slug,
    kind: 'skill',
    name: item.name,
    description: item.description,
    category: item.category,
    status: statusLabel(item),
    action: item.canInstall ? 'install' : item.canUninstall ? 'uninstall' : 'builtin',
    badges: item.badges.map((badge) => (
      badge === 'featured' ? '推荐' : badge === 'new' ? '新' : badge === 'local' ? '本地' : '内置'
    )),
    tone: item.tone,
    monogram: skillInitial(item.name),
  };
}

function pluginCapability(plugin: PluginItem): CapabilityHubItem {
  const enabled = pluginEnabled(plugin);
  const toggleAllowed = plugin.system_allowed !== false && plugin.role_allowed !== false;
  const blockedReason = plugin.system_allowed === false
    ? '已被系统策略禁用'
    : plugin.role_allowed === false
      ? '当前账号角色未获授权'
      : '';
  return {
    id: plugin.key || plugin.name,
    kind: 'plugin',
    name: plugin.label || plugin.name,
    description: plugin.description || '扩展 Agent 运行时能力（工具、Hook、平台通道等）。',
    category: plugin.kind || 'standalone',
    status: enabled ? '已启用' : '未启用',
    action: 'toggle',
    badges: ['插件'],
    tone: toneFor(plugin.key || plugin.name),
    monogram: skillInitial(plugin.label || plugin.name),
    enabled,
    toggleAllowed: Boolean(plugin.toggle_endpoint) && toggleAllowed,
    ...(blockedReason ? { blockedReason } : {}),
    ...(plugin.version ? { version: plugin.version } : {}),
    ...(plugin.tools.length ? { tools: plugin.tools } : {}),
    ...(plugin.error ? { error: plugin.error } : {}),
  };
}

function capabilityState(): CapabilityHubState {
  const skillItems = buildSkillItems(store ?? { installed: [], optional: [] });
  lastSkillItems = skillItems;
  reconcileCategorySelection(skillItems);
  const skillResults = filterSkills(skillItems).map(skillCapability);
  const pluginResults = filterPlugins(plugins).map(pluginCapability);
  const selectedId = modalSkill?.slug || (modalPlugin ? modalPlugin.key || modalPlugin.name : '');
  return {
    tab: pageTab,
    subview: skillSubview,
    query: searchQ,
    category,
    categories: capabilityCategories(skillItems),
    items: pageTab === 'skills' ? skillResults : pluginResults,
    page: 1,
    pageCount: 1,
    ...(selectedId ? { selectedId } : {}),
    loading: skillsLoading,
    refreshing: skillsLoading,
  };
}

function changeCapabilityTab(tab: PageTab): void {
  pageTab = tab;
  category = '全部';
  skillSubview = 'installed';
  modalSkill = null;
  modalPlugin = null;
  renderShell();
}

function changeSkillSubview(subview: SkillSubview): void {
  skillSubview = subview;
  category = '全部';
  modalSkill = null;
  modalPlugin = null;
  renderShell();
}

function changeCapabilityCategory(nextCategory: string): void {
  category = nextCategory;
  renderShell();
}

function searchCapabilities(query: string): void {
  searchQ = query;
  if (localSearchTimer) window.clearTimeout(localSearchTimer);
  localSearchTimer = window.setTimeout(() => {
    localSearchTimer = null;
    renderShell();
  }, 120);
}

function openCapability(id: string): void {
  modalScrollMemory = $('#skills-page-root')?.scrollTop ?? null;
  if (pageTab === 'skills') {
    modalSkill = lastSkillItems.find((item) => item.slug === id) ?? null;
    modalPlugin = null;
  } else {
    modalPlugin = plugins.find((plugin) => (plugin.key || plugin.name) === id) ?? null;
    modalSkill = null;
  }
}

function syncEvolutionToggle(): void {
  const actions = capabilityHubView?.element.querySelector<HTMLElement>('.mw-hub-actions');
  if (!actions) return;
  actions.querySelector('[data-evolution-control]')?.remove();
  const row = document.createElement('label');
  const input = document.createElement('input');
  const track = document.createElement('span');
  row.className = 'evolution-toggle-row';
  row.dataset.evolutionControl = '';
  input.type = 'checkbox';
  input.checked = evolutionConfig.auto_trigger && evolutionConfig.auto_full_cycle;
  input.disabled = togglingEvolution;
  input.setAttribute('role', 'switch');
  input.setAttribute('aria-label', '自进化');
  track.className = 'plugin-toggle__track';
  track.append(document.createElement('span'));
  track.firstElementChild?.classList.add('plugin-toggle__thumb');
  const toggle = document.createElement('span');
  toggle.className = `plugin-toggle${input.checked ? ' is-on' : ''}${togglingEvolution ? ' is-pending' : ''}`;
  toggle.append(input, track);
  row.append(document.createTextNode('自进化'), toggle);
  input.addEventListener('change', () => void toggleEvolution(input.checked));
  actions.prepend(row);
}

function renderShell(): void {
  const root = $('#skills-page-root');
  if (!root) return;
  if (!capabilityHubView) {
    capabilityHubView = createCapabilityHubView({
      state: capabilityState(),
      onTabChange: (tab) => changeCapabilityTab(tab),
      onSubviewChange: (subview) => changeSkillSubview(subview),
      onSearch: searchCapabilities,
      onCategoryChange: changeCapabilityCategory,
      onRefresh: () => {
        void loadStore();
      },
      onOpen: openCapability,
      onClose: () => {
        modalSkill = null;
        modalPlugin = null;
      },
      onAction: (id, action) => {
        if (action === 'install') void installSkill(id);
        if (action === 'uninstall') void uninstallSkill(id);
        if (action === 'builtin') {
          notify(`对话中发送 /${id} 即可激活`);
          navigateToChat?.();
        }
      },
      onToggle: (id, enabled) => void togglePlugin(id, enabled),
      onPageChange: () => undefined,
    });
    root.replaceChildren(capabilityHubView.element);
  } else {
    capabilityHubView.update(capabilityState());
  }
  syncEvolutionToggle();
}

async function toggleEvolution(value: boolean): Promise<void> {
  if (togglingEvolution) return;
  togglingEvolution = true;
  renderShell();
  try {
    const result = await backendApi.updateEvolution({
      auto_trigger: value,
      auto_full_cycle: value,
    });
    if (!result.ok || !result.evolution) throw new Error('自进化设置更新失败');
    evolutionConfig = result.evolution;
    notify(`自进化${value ? '已开启' : '已关闭'}`);
  } catch (error) {
    notify((error as Error).message || '自进化设置更新失败');
  } finally {
    togglingEvolution = false;
    renderShell();
  }
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
    // 本地共享 Skill 只移除 Crew 中的接入入口，原始 Skill 仍保留在本机共享目录。
    const isLocalShared = item?.isLocalShared === true;
    const ok = await showConfirmDialog({
      title: isLocalShared ? '确认从 Crew 中移除技能' : '确认全局卸载技能',
      message: isLocalShared
        ? `移除「${item?.name || slug}」后，本机所有账号都无法再通过 Crew 调用该技能，`
          + '但不会删除电脑中全局共享目录里的原始技能，之后仍可重新添加。确定移除吗？'
        : `技能是本机全局共享能力。卸载「${item?.name || slug}」后本地技能文件将被移除，`
          + `本机所有账号都将无法再通过 /${slug} 调用。确定卸载吗？`,
      confirmText: isLocalShared ? '从 Crew 中移除' : '全局卸载',
      cancelText: '取消',
    });
    if (!ok) return;
    const res = await backendApi.uninstallSkill(slug);
    if (!res.ok) throw new Error('uninstall failed');
    notify(isLocalShared ? `已从 Crew 中移除 ${slug}` : `已卸载 ${slug}`);
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

function closeModal(): void {
  modalSkill = null;
  modalPlugin = null;
  renderShell();
  const root = $('#skills-page-root');
  if (root && modalScrollMemory != null) root.scrollTop = modalScrollMemory;
  modalScrollMemory = null;
}

async function loadStore(): Promise<void> {
  skillsLoading = true;
  renderShell();
  try {
    const [skillData, pluginData] = await Promise.all([
      backendApi.skillStore(),
      backendApi.plugins().catch(() => [] as PluginItem[]),
    ]);
    store = skillData;
    plugins = pluginData;
    if (skillData.evolution) evolutionConfig = skillData.evolution;
  } catch {
    store = { installed: [], optional: [] };
    plugins = [];
  }
  skillsLoading = false;
  renderShell();
}

export function renderSkillsPage(): void {
  renderShell();
}

export function activateSkillsPage(): void {
  void loadStore();
}

export function bindSkillsPageLifecycle(onNavigateToChat: () => void): () => void {
  navigateToChat = onNavigateToChat;
  // 登录态变化时（登录成功 / 退出）刷新当前账号可见的技能与插件。
  const onLoginChanged = (): void => {
    void loadStore();
  };

  window.addEventListener('user:login-changed', onLoginChanged);
  return () => {
    if (navigateToChat === onNavigateToChat) navigateToChat = null;
    window.removeEventListener('user:login-changed', onLoginChanged);
    capabilityHubView?.dispose();
    capabilityHubView = null;
  };
}

export async function initSkillsPage(): Promise<void> {
  await loadStore();
}
