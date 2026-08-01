/**
 * workspace-store：工作空间树 / 当前 workspace / 历史侧栏 / 管理弹窗
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { Workspace } from '../backend-client';

export interface ChannelSessionGroup {
  platform: string;
  label: string;
  sessions: {
    id: string;
    title: string;
    updatedAt: number;
    preview: string;
    badge: string;
    workspaceId: string;
    titleFromSummary?: boolean;
    archived?: boolean;
    pinned?: boolean;
    channelPlatform?: string;
  }[];
}

export interface WorkspaceStoreState {
  workspaces: Workspace[];
  expandedWorkspaces: Record<string, boolean>;
  /** 渠道文件夹展开态（platform → expanded） */
  channelExpanded: Record<string, boolean>;
  channelSessionGroups: ChannelSessionGroup[];
  wsShowAll: Record<string, boolean>;
  currentWorkspaceId: string;
  historyCollapsed: boolean;
  historyFilter: string;
  selectedSessions: Record<string, boolean>;
  manageMode: boolean;
}

export const workspaceStore: Store<WorkspaceStoreState> = createStore<WorkspaceStoreState>(
  {
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
  },
  'workspace',
);
