/**
 * Work 办公助手导航：工作 / 计划 / 知识三个用户入口、上下文切换与断线重拉。
 */

import type { FeatureState, WorkLocation } from '../../features/sidebar-nav';
import { productModeStore, updateProductModeView } from '../../stores/product-mode-store';
import {
  loadWorkHistory,
  loadWorkDashboard,
  refreshWorkOverview,
  connectWorkRefetch,
  workStore,
} from '../../stores/work-store';
import {
  startOfficePolling,
  stopOfficePolling,
  connectOfficeRefetch,
} from '../../stores/office-store';
import { getDashboardSnapshot, refreshDashboard, renderDashboard } from './dashboard';
import { renderWorkHistory, type WorkHistoryCommands } from './history';
import { renderItemsPage } from './items';
import { renderKnowledgePage } from './knowledge';

/** Work 暴露首页、统一计划和知识三个真实用户入口。 */
export const WORK_FEATURE_STATES: Partial<Record<WorkLocation, FeatureState>> = {
  workbench: 'available',
  knowledge: 'available',
  items: 'available',
  workspaces: 'hidden',
  templates: 'hidden',
};

let contextContainer: HTMLElement | null = null;
let refetchDispose: (() => void) | null = null;
let officeRefetchDispose: (() => void) | null = null;
let currentLocation: WorkLocation = 'workbench';

/** 根据目的地渲染对应上下文，知识页不重复办公历史。 */
export function renderWorkContext(
  container: HTMLElement,
  location: WorkLocation,
): void {
  if (location !== 'knowledge') {
    renderWorkHistory(
      container,
      undefined,
      historyCommands,
      'all',
    );
    return;
  }
  container.className = 'mw-work-knowledge-context';
  const search = document.createElement('input');
  const personal = document.createElement('button');
  search.type = 'search';
  search.className = 'mw-work-knowledge-context__search';
  search.placeholder = '搜索知识';
  search.setAttribute('aria-label', '搜索知识');
  personal.type = 'button';
  personal.className = 'mw-work-knowledge-context__item';
  personal.dataset.knowledgeScope = 'personal';
  personal.textContent = '个人知识';
  personal.setAttribute('aria-current', 'page');
  container.replaceChildren(search, personal);
}

/**
 * 进入 Work 模式时：加载历史、连接断线重拉、渲染历史侧栏。
 * 幂等：重复进入只补拉数据，不重复挂载。
 */
export async function enterWorkMode(historyContainer: HTMLElement | null): Promise<void> {
  if (!refetchDispose) refetchDispose = connectWorkRefetch();
  if (!officeRefetchDispose) officeRefetchDispose = connectOfficeRefetch();
  // 办公快照（邮件/待办/日程/会议）启动轮询并立即拉一轮；首轮空快照由 UI 显空态。
  startOfficePolling();
  await refreshWorkOverview().catch(() => {
    /* 首次拉取失败由重连或用户操作重试 */
  });
  if (historyContainer) {
    contextContainer = historyContainer;
    currentLocation = 'workbench';
    renderWorkContext(historyContainer, 'workbench');
  }
}

let historyCommands: WorkHistoryCommands = {};

/** 注入 Work 历史跳转命令；页面模块不直接依赖应用组合根。 */
export function setWorkHistoryCommands(commands: WorkHistoryCommands): void {
  historyCommands = commands;
}

/**
 * 退出 Work 模式时清理：解除重拉订阅、重置挂载标记。
 * 保留 store 数据以便快速切回。
 */
export function leaveWorkMode(): void {
  refetchDispose?.();
  refetchDispose = null;
  officeRefetchDispose?.();
  officeRefetchDispose = null;
  stopOfficePolling();
  contextContainer = null;
}

/**
 * 重渲染当前 Work 历史侧栏（保留当前 location 与搜索词）。
 * 打开事项 / 会话后调用，让对应行高亮为选中态，与通用助手体感一致。
 */
export function refreshWorkHistory(): void {
  if (contextContainer) renderWorkContext(contextContainer, currentLocation);
}

/** 当前是否处于 Work 模式。 */
export function isWorkMode(): boolean {
  return productModeStore.get().productMode === 'work';
}

/**
 * 路由到 Work location。返回是否已处理（未处理的 location 由调用方兜底）。
 * 各 location 的完整视图由对应 feature（items/workspace/knowledge/templates）渲染；
 * 此处只做最小占位与历史刷新，避免空视图。
 */
export function activateWorkLocation(
  location: WorkLocation,
  pageSlot: HTMLElement,
  options: { itemId?: string } = {},
): boolean {
  if (location === 'workspaces' || location === 'templates') return false;
  if (isWorkMode()) updateProductModeView({ lastPosition: location });
  currentLocation = location;
  pageSlot.replaceChildren();
  pageSlot.dataset.workLocation = location;
  const content = document.createElement('div');
  content.className = 'mw-work-page__content';
  if (location === 'workbench' || (location === 'items' && options.itemId)) {
    // workbench 由 dashboard 的问候语/概览充当标题，不再重复「工作」占位 heading。
    pageSlot.append(content);
  } else {
    const heading = document.createElement('h1');
    heading.className = 'mw-work-page__heading';
    heading.textContent = WORK_LOCATION_TITLES[location] ?? location;
    pageSlot.append(heading, content);
  }

  if (location === 'workbench' && options.itemId) {
    renderItemsPage(content, options.itemId, (sessionId) => {
      const linkedItem = workStore.get().items.find(
        (item) => item.processing_session_id === sessionId,
      );
      void historyCommands.openSession?.(
        sessionId,
        'work',
        undefined,
        linkedItem?.item_id,
      );
    }, historyCommands.openWorkbench);
  } else if (location === 'workbench') {
    const commands = {
      ...(historyCommands.openItem ? { openItem: historyCommands.openItem } : {}),
      ...(historyCommands.manageItems ? { openItems: historyCommands.manageItems } : {}),
      ...(historyCommands.newItem ? { newItem: historyCommands.newItem } : {}),
    };
    renderDashboard(content, getDashboardSnapshot(), '', commands);
    void loadWorkDashboard().then(async () => {
      if (!workStore.get().dashboard?.brief) await refreshDashboard();
      if (content.isConnected) renderDashboard(content, getDashboardSnapshot(), '', commands);
    });
  } else if (location === 'items') {
    renderItemsPage(content, options.itemId, (sessionId) => {
      const linkedItem = workStore.get().items.find(
        (item) => item.processing_session_id === sessionId,
      );
      void historyCommands.openSession?.(
        sessionId,
        'work',
        undefined,
        linkedItem?.item_id,
      );
    }, historyCommands.openWorkbench);
  } else if (location === 'knowledge') {
    void renderKnowledgePage(content);
  }
  if (contextContainer) renderWorkContext(contextContainer, location);
  // 切换 location 时刷新历史，保证来源标识一致。
  void loadWorkHistory()
    .then(() => {
      const context = contextContainer ?? document.getElementById('mw-work-history');
      if (context && location !== 'knowledge') {
        renderWorkContext(context, location);
      }
    })
    .catch(() => {
      /* 历史刷新失败不阻塞导航 */
    });
  return true;
}

export const WORK_LOCATION_TITLES: Record<WorkLocation, string> = {
  workbench: '工作',
  items: '计划',
  workspaces: '工作空间',
  knowledge: '知识库',
  templates: '模板',
};
