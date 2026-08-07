import { createBadge, createButton, createIconButton, createTabs } from '../components/controls';
import { createIcon } from '../components/icon';
import { activateExistingModal, type OverlayHandle } from '../components/overlays';
import { createHubTemplate } from '../layouts/page-templates';

export type CapabilityHubTab = 'skills' | 'plugins';
export type CapabilityHubSubview = 'installed' | 'market';
export type CapabilityAction = 'install' | 'uninstall' | 'builtin' | 'toggle';

export interface CapabilityHubItem {
  id: string;
  kind: 'skill' | 'plugin';
  name: string;
  description: string;
  category: string;
  status: string;
  action: CapabilityAction;
  badges: string[];
  enabled?: boolean;
  toggleAllowed?: boolean;
  blockedReason?: string;
  version?: string;
  tools?: string[];
  error?: string;
  tone?: 'blue' | 'violet' | 'cyan' | 'amber' | 'green' | 'rose' | 'indigo' | 'orange';
  monogram?: string;
}

export interface CapabilityHubState {
  tab: CapabilityHubTab;
  subview: CapabilityHubSubview;
  query: string;
  category: string;
  categories: { id: string; label: string; count: number }[];
  items: CapabilityHubItem[];
  page: number;
  pageCount: number;
  selectedId?: string;
  loading?: boolean;
  refreshing?: boolean;
  error?: string;
  notice?: string;
}

export interface CapabilityHubOptions {
  state: CapabilityHubState;
  onTabChange?: (tab: CapabilityHubTab) => void;
  onSubviewChange?: (subview: CapabilityHubSubview) => void;
  onSearch?: (query: string) => void;
  onCategoryChange?: (category: string) => void;
  onRefresh?: () => void;
  onOpen?: (id: string) => void;
  onClose?: () => void;
  onAction?: (id: string, action: Exclude<CapabilityAction, 'toggle'>) => void;
  onToggle?: (id: string, enabled: boolean) => void;
  onPageChange?: (page: number) => void;
}

export interface CapabilityHubView {
  element: HTMLElement;
  update(state: CapabilityHubState): void;
  dispose(): void;
}

const decorativeSkillBadges = new Set(['推荐', '新']);

function text(tag: string, className: string, value: string): HTMLElement {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}

/** Renders the shared, state-complete Skills and Plugins Hub. */
export function createCapabilityHubView(options: CapabilityHubOptions): CapabilityHubView {
  const template = createHubTemplate();
  const heading = document.createElement('div');
  const title = text('h1', 'mw-hub-heading__title', '技能市场');
  const description = text('p', 'mw-hub-heading__description', '');
  const actions = document.createElement('div');
  const filterRow = document.createElement('div');
  const subviewHost = document.createElement('div');
  const categories = document.createElement('div');
  const search = document.createElement('label');
  const searchInput = document.createElement('input');
  const notice = document.createElement('div');
  const results = document.createElement('div');
  const pagination = document.createElement('nav');
  const overlay = document.createElement('div');
  const tabs = createTabs({
    label: '能力类型',
    value: options.state.tab,
    items: [
      { id: 'skills', label: '技能' },
      { id: 'plugins', label: '插件' },
    ],
    onChange: (id) => options.onTabChange?.(id as CapabilityHubTab),
  });
  let current = options.state;
  let selectedId = current.selectedId;
  let detailHandle: OverlayHandle | undefined;
  let detailTrigger: HTMLElement | undefined;
  let detailDialogId = 0;
  let replacingDetail = false;
  let disposing = false;

  const createIdentity = (item: CapabilityHubItem): HTMLElement => {
    const identity = document.createElement('span');
    const monogram = document.createElement('span');
    identity.className = 'mw-identity mw-capability-card__identity';
    identity.dataset.tone = item.tone ?? 'blue';
    monogram.className = 'mw-identity__monogram';
    monogram.textContent = item.monogram ?? item.name.trim().slice(0, 1).toLocaleUpperCase();
    identity.append(
      createIcon(item.kind === 'skill' ? 'skill-badge' : 'plugin-badge', { size: 40 }),
      monogram,
    );
    return identity;
  };

  template.element.classList.add('mw-capability-hub');
  heading.className = 'mw-hub-heading';
  heading.append(title, description);
  actions.className = 'mw-hub-actions';
  filterRow.className = 'mw-capability-hub__filters';
  subviewHost.className = 'mw-capability-hub__subview';
  categories.className = 'mw-capability-hub__categories';
  search.className = 'mw-capability-hub__search';
  search.append(createIcon('icon-search'), searchInput);
  searchInput.type = 'search';
  searchInput.dataset.capabilitySearch = '';
  searchInput.autocomplete = 'off';
  notice.className = 'mw-capability-hub__notice';
  notice.setAttribute('role', 'status');
  results.className = 'mw-capability-hub__grid';
  pagination.className = 'mw-capability-hub__pagination';
  pagination.setAttribute('aria-label', '技能市场分页');
  overlay.className = 'mw-overlay-layer mw-capability-hub__overlay';
  overlay.hidden = true;
  template.slots.header.append(heading, actions);
  template.slots.primaryNavigation.append(tabs.element, subviewHost);
  filterRow.append(categories, search);
  template.slots.filters.append(filterRow, notice);
  template.slots.results.append(results, pagination);
  template.element.append(overlay);

  const clearDetail = (): void => {
    detailHandle = undefined;
    if (replacingDetail) return;
    selectedId = undefined;
    detailTrigger = undefined;
    overlay.replaceChildren();
    if (!disposing) options.onClose?.();
  };

  const closeDetail = (): void => {
    if (detailHandle) detailHandle.close();
    else clearDetail();
  };

  const renderDetail = (): void => {
    const item = current.items.find((candidate) => candidate.id === selectedId);
    if (!item) {
      if (detailHandle) detailHandle.close();
      else overlay.hidden = true;
      return;
    }
    if (detailHandle) {
      replacingDetail = true;
      detailHandle.close();
      replacingDetail = false;
    }
    selectedId = item.id;
    overlay.replaceChildren();
    const dialog = document.createElement('section');
    const header = document.createElement('header');
    const titleGroup = document.createElement('div');
    const body = document.createElement('div');
    const footer = document.createElement('footer');
    const titleId = `mw-capability-detail-${++detailDialogId}`;
    const close = createIconButton({ label: '关闭', icon: 'icon-close', variant: 'ghost', onPress: closeDetail });
    dialog.className = 'mw-dialog mw-capability-detail';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', titleId);
    header.className = 'mw-dialog__header mw-capability-detail__header';
    titleGroup.className = 'mw-capability-detail__title-group';
    const detailTitle = text('h2', 'mw-capability-detail__title', item.name);
    const subtitle = item.action === 'builtin'
      ? `/${item.id}`
      : item.version ? `v${item.version}` : '';
    detailTitle.id = titleId;
    titleGroup.append(detailTitle);
    if (subtitle) titleGroup.append(text('p', 'mw-capability-detail__subtitle', subtitle));
    close.element.classList.add('mw-dialog__close');
    header.append(createIdentity(item), titleGroup, close.element);

    body.className = 'mw-dialog__body mw-capability-detail__body';
    const summary = document.createElement('section');
    summary.className = 'mw-capability-detail__section';
    summary.append(
      text('h3', 'mw-capability-detail__label', '能力介绍'),
      text('p', 'mw-capability-detail__description', item.description),
    );
    const metadata = document.createElement('section');
    const metadataBadges = document.createElement('div');
    metadata.className = 'mw-capability-detail__section';
    metadataBadges.className = 'mw-capability-detail__metadata';
    metadataBadges.append(
      createBadge({ label: item.status, compact: true }),
      createBadge({ label: item.category, compact: true }),
      ...item.badges
        .filter((badge) => (
          badge !== item.status
          && badge !== item.category
          && !(item.kind === 'skill' && decorativeSkillBadges.has(badge))
        ))
        .map((badge) => createBadge({
        label: badge,
        tone: badge === '推荐' ? 'warning' : 'neutral',
        compact: true,
        })),
    );
    metadata.append(text('h3', 'mw-capability-detail__label', '分类与状态'), metadataBadges);
    body.append(summary, metadata);
    if (item.tools?.length) {
      const toolSection = document.createElement('section');
      const tools = document.createElement('div');
      toolSection.className = 'mw-capability-detail__section';
      tools.className = 'mw-capability-detail__tools';
      tools.append(...item.tools.map((tool) => createBadge({ label: tool, compact: true })));
      toolSection.append(text('h3', 'mw-capability-detail__label', '注册工具'), tools);
      body.append(toolSection);
    }
    if (item.blockedReason) body.append(text('p', 'mw-capability-detail__warning', item.blockedReason));
    if (item.error) body.append(text('p', 'mw-capability-detail__error', item.error));

    footer.className = 'mw-dialog__footer mw-capability-detail__footer';
    if (item.action === 'toggle') {
      const toggleRow = document.createElement('label');
      const toggle = document.createElement('input');
      toggleRow.className = 'mw-capability-detail__toggle';
      toggle.type = 'checkbox';
      toggle.checked = Boolean(item.enabled);
      toggle.disabled = item.toggleAllowed === false;
      toggle.dataset.capabilityDetailToggle = item.id;
      toggle.setAttribute('role', 'switch');
      toggle.addEventListener('change', () => options.onToggle?.(item.id, toggle.checked));
      toggleRow.append(toggle, document.createTextNode(item.enabled ? '已启用' : '未启用'));
      footer.append(toggleRow);
    } else {
      const detailItemAction = item.action as Exclude<CapabilityAction, 'toggle'>;
      const actionLabels = {
        install: `安装 ${item.name}`,
        uninstall: `卸载 ${item.name}`,
        builtin: `使用 /${item.id}`,
      } as const;
      const detailAction = createButton({
        label: actionLabels[detailItemAction],
        variant: detailItemAction === 'uninstall' ? 'danger' : 'primary',
        onPress: () => options.onAction?.(item.id, detailItemAction),
      });
      detailAction.element.dataset.capabilityDetailAction = item.id;
      footer.append(detailAction.element);
    }
    dialog.append(header, body, footer);
    overlay.append(dialog);
    detailHandle = activateExistingModal({
      root: overlay,
      panel: dialog,
      trigger: detailTrigger,
      initialFocus: close.element,
      onClose: clearDetail,
    });
  };

  const renderCard = (item: CapabilityHubItem): HTMLElement => {
    const card = document.createElement('article');
    const open = document.createElement('button');
    const copy = document.createElement('span');
    const headingRow = document.createElement('span');
    const footer = document.createElement('span');
    card.className = 'mw-capability-card';
    card.dataset.capabilityId = item.id;
    open.type = 'button';
    open.className = 'mw-capability-card__open';
    open.dataset.capabilityOpen = item.id;
    open.addEventListener('click', () => {
      selectedId = item.id;
      detailTrigger = open;
      options.onOpen?.(item.id);
      renderDetail();
    });
    copy.className = 'mw-capability-card__copy';
    headingRow.className = 'mw-capability-card__heading';
    headingRow.append(text('strong', '', item.name));
    for (const badge of item.badges.filter((label) => (
      label !== item.status
      && label !== item.category
      && !(item.kind === 'skill' && decorativeSkillBadges.has(label))
    ))) {
      const tone = badge === '推荐' ? 'warning' : badge === '新' ? 'info' : 'neutral';
      headingRow.append(createBadge({ label: badge, tone, compact: true }));
    }
    footer.className = 'mw-capability-card__footer';
    footer.append(
      createBadge({
        label: item.category,
        compact: true,
      }),
      text('span', '', item.version ? `${item.status} · v${item.version}` : item.status),
    );
    copy.append(headingRow, text('span', 'mw-capability-card__description', item.description), footer);
    open.append(createIdentity(item), copy);
    card.append(open);

    if (item.action === 'toggle') {
      const label = document.createElement('label');
      const toggle = document.createElement('input');
      label.className = 'mw-capability-card__switch';
      toggle.type = 'checkbox';
      toggle.checked = Boolean(item.enabled);
      toggle.disabled = item.toggleAllowed === false;
      toggle.dataset.capabilityToggle = item.id;
      toggle.setAttribute('role', 'switch');
      toggle.setAttribute('aria-label', `${item.enabled ? '禁用' : '启用'} ${item.name}`);
      toggle.addEventListener('change', () => options.onToggle?.(item.id, toggle.checked));
      label.append(toggle, document.createElement('span'));
      card.append(label);
    } else {
      const labels = { install: '安装', uninstall: '卸载', builtin: '内置' } as const;
      const itemAction = item.action as Exclude<CapabilityAction, 'toggle'>;
      const button = createButton({
        label: labels[itemAction],
        ...(itemAction === 'install'
          ? { icon: 'icon-plus' as const }
          : itemAction === 'builtin'
            ? { icon: 'icon-check' as const }
            : {}),
        variant: itemAction === 'uninstall' ? 'outline' : 'secondary',
        size: 'small',
        disabled: itemAction === 'builtin',
        onPress: () => options.onAction?.(item.id, itemAction),
      });
      button.element.dataset.capabilityAction = item.id;
      card.dataset.action = itemAction;
      card.append(button.element);
    }
    if (item.blockedReason) card.append(text('p', 'mw-capability-card__warning', item.blockedReason));
    if (item.error) card.append(text('p', 'mw-capability-card__error', item.error));
    return card;
  };

  const renderSubview = (): void => {
    subviewHost.replaceChildren();
    if (current.tab !== 'skills') return;
    const control = createTabs({
      label: '技能视图',
      value: current.subview,
      items: [
        { id: 'installed', label: '已安装' },
        { id: 'market', label: '技能市场' },
      ],
      onChange: (id) => options.onSubviewChange?.(id as CapabilityHubSubview),
    });
    subviewHost.append(control.element);
  };

  const renderCategories = (): void => {
    categories.replaceChildren();
    categories.hidden = current.tab !== 'skills';
    if (categories.hidden) return;
    for (const category of current.categories) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mw-capability-hub__category';
      button.dataset.selected = String(category.id === current.category);
      button.append(document.createTextNode(category.label), createBadge({ label: String(category.count), compact: true }));
      button.addEventListener('click', () => options.onCategoryChange?.(category.id));
      categories.append(button);
    }
  };

  const renderPagination = (): void => {
    pagination.replaceChildren();
    pagination.hidden = current.tab !== 'skills' || current.subview !== 'market' || current.pageCount <= 1;
    if (pagination.hidden) return;
    const previous = createButton({
      label: '上一页',
      variant: 'ghost',
      size: 'small',
      disabled: current.page <= 1,
      onPress: () => options.onPageChange?.(current.page - 1),
    });
    const next = createButton({
      label: '下一页',
      variant: 'ghost',
      size: 'small',
      disabled: current.page >= current.pageCount,
      onPress: () => options.onPageChange?.(current.page + 1),
    });
    previous.element.dataset.pagePrev = '';
    next.element.dataset.pageNext = '';
    pagination.append(previous.element, text('span', '', `第 ${current.page} / ${current.pageCount} 页`), next.element);
  };

  const render = (): void => {
    const isPlugin = current.tab === 'plugins';
    tabs.setValue(current.tab);
    title.textContent = isPlugin ? '插件' : '技能市场';
    description.textContent = isPlugin
      ? '管理扩展工具、Hook 与平台通道，并查看账号与策略可用性。'
      : '浏览已安装能力或从技能市场补充新的工作方式。';
    searchInput.placeholder = isPlugin ? '搜索插件' : current.subview === 'market' ? '搜索技能市场' : '搜索已安装技能';
    searchInput.setAttribute('aria-label', searchInput.placeholder);
    if (searchInput.value !== current.query) searchInput.value = current.query;
    notice.textContent = current.notice ?? '';
    notice.hidden = !current.notice;
    results.replaceChildren();
    results.dataset.state = current.loading ? 'loading' : current.error ? 'error' : 'ready';
    renderSubview();
    renderCategories();
    if (current.loading) {
      results.append(text('p', 'mw-hub-state', `正在加载${isPlugin ? '插件' : '技能'}…`));
    } else if (current.error) {
      const error = text('p', 'mw-hub-state', current.error);
      error.setAttribute('role', 'alert');
      results.append(error);
    } else if (!current.items.length) {
      results.append(text('p', 'mw-hub-state', isPlugin ? '暂无插件数据' : '当前筛选下没有技能'));
    } else {
      results.append(...current.items.map(renderCard));
    }
    renderPagination();
    renderDetail();
  };

  const handleSearch = (): void => options.onSearch?.(searchInput.value);
  searchInput.addEventListener('input', handleSearch);
  const refresh = createButton({
    label: '刷新',
    icon: 'icon-refresh',
    variant: 'secondary',
    onPress: () => options.onRefresh?.(),
  });
  actions.append(refresh.element);
  render();

  return {
    element: template.element,
    update(state) {
      current = state;
      selectedId = state.selectedId ?? selectedId;
      render();
    },
    dispose() {
      disposing = true;
      detailHandle?.dispose();
      overlay.remove();
      tabs.dispose();
      refresh.dispose();
      searchInput.removeEventListener('input', handleSearch);
    },
  };
}
