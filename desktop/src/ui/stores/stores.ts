/**
 * 渲染层领域 store 的统一 barrel。
 *
 * 旧版单文件 `stores.ts` 在 T6 拆成 7 个独立文件 + barrel re-export，
 * 旧 import 路径 `./stores/stores` 仍可用，调用方零改动。
 */
export {
  sessionStore,
  type SessionStoreState,
} from './session-store';
export {
  messageStore,
  type MessageStoreState,
} from './message-store';
export {
  taskStore,
  type TaskStoreState,
} from './task-store';
export {
  configStore,
  type ConfigStoreState,
} from './config-store';
export {
  workspaceStore,
  type WorkspaceStoreState,
} from './workspace-store';
export {
  authStore,
  type AuthStoreState,
} from './auth-store';
export {
  uiStore,
  type UiStoreState,
} from './ui-store';
export {
  cronStore,
  type CronJobStoreState,
  type CronJobScope,
} from './cron-store';
export {
  externalStore,
  type ExternalStoreState,
} from './external-store';
export {
  productModeStore,
  type ProductMode,
  type ProductModeStoreState,
  type ProductModeViewState,
} from './product-mode-store';
export {
  workStore,
  type WorkStoreState,
} from './work-store';
export {
  officeStore,
  type OfficeSnapshots,
} from './office-store';

import { sessionStore } from './session-store';
import { messageStore } from './message-store';
import { taskStore } from './task-store';
import { configStore } from './config-store';
import { workspaceStore } from './workspace-store';
import { authStore } from './auth-store';
import { uiStore } from './ui-store';
import { cronStore } from './cron-store';
import { externalStore } from './external-store';
import { defaultProductModeState, productModeStore } from './product-mode-store';
import { workStore } from './work-store';
import { officeStore } from './office-store';

/** 测试钩子：把全部 store 重置为初始状态。 */
export function __resetAllStoresForTest(): void {
  sessionStore.replace({
    sessions: [],
    backendSessions: [],
    activeSessionId: null,
    sessionStatuses: {},
    busySessions: {},
    subscribedSessions: new Set(),
    books: {},
    suppressChunks: new Set(),
    editFromIdx: {},
    userFoldedTurns: new Set(),
    userUnfoldedTurns: new Set(),
    unreadCompletedSessions: new Set(),
  });
  messageStore.replace({
    messages: {},
    queueHints: {},
    pendingQueues: {},
    attachments: [],
  });
  taskStore.replace({ tasks: [], taskBoardOpen: false, taskBoardWidth: 320, kanbanBoard: null });
  configStore.replace({
    config: null,
    configModel: '__default__',
    mode: 'agent',
    composerMode: 'craft',
  });
  workspaceStore.replace({
    workspaces: [],
    expandedWorkspaces: {},
    channelExpanded: {},
    channelSessionGroups: [],
    wsShowAll: {},
    currentWorkspaceId: 'default',
    historyCollapsed: false,
    historyFilter: '',
    selectedSessions: {},
    manageMode: false,
  });
  authStore.replace({ userInfo: null, isLoggedIn: false });
  uiStore.replace({
    activeTab: 'chat',
    activeSystemPanel: 'overview',
    backendConnected: false,
    socket: null,
    feedbackDraft: { title: '', description: '', images: [] },
    feedbackList: [],
    editingResend: false,
  });
  cronStore.replace({
    cronJobs: [],
    cronJobScope: 'all',
    cronJobDetailId: null,
    cronDeleteConfirmId: null,
  });
  externalStore.replace({
    activeExternalTeamIdBySession: {},
  });
  productModeStore.replace(defaultProductModeState());
  workStore.replace({
    history: [],
    selectedWorkspaceId: null,
    items: [],
    dashboard: null,
    sources: [],
    personalKnowledge: [],
    templates: [],
    preferences: [],
    preferenceAutoLearning: true,
    settings: {},
    indexStatus: {},
    loading: false,
    error: null,
  });
  officeStore.replace({
    mail: null,
    todo: null,
    schedule: null,
    meeting: null,
    loading: false,
    error: null,
  });
}

/** 兼容旧导出：ALL_STORES 仍可被外部引用。 */
export const ALL_STORES = {
  session: sessionStore,
  message: messageStore,
  task: taskStore,
  config: configStore,
  workspace: workspaceStore,
  auth: authStore,
  ui: uiStore,
  cron: cronStore,
  external: externalStore,
  productMode: productModeStore,
  work: workStore,
  office: officeStore,
} as const;
