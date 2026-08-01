/**
 * 渲染层 7 个领域 store 的统一 barrel（SPEC §3.2 要求 7 个独立 store 文件）。
 *
 * 通过 barrel re-export 保持 `./stores/stores` 统一导入路径。
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
  uiStore,
  type UiStoreState,
} from './ui-store';
export {
  cronStore,
  type CronJobStoreState,
  type CronJobScope,
} from './cron-store';

import { sessionStore } from './session-store';
import { messageStore } from './message-store';
import { taskStore } from './task-store';
import { configStore } from './config-store';
import { workspaceStore } from './workspace-store';
import { uiStore } from './ui-store';
import { cronStore } from './cron-store';

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
    activeExternalTeamIdBySession: {},
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
}

/** 兼容旧导出：ALL_STORES 仍可被外部引用。 */
export const ALL_STORES = {
  session: sessionStore,
  message: messageStore,
  task: taskStore,
  config: configStore,
  workspace: workspaceStore,
  ui: uiStore,
  cron: cronStore,
} as const;
