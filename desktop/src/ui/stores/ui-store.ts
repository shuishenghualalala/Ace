/**
 * ui-store：当前 tab / system 子面板 / 网关连接状态 / 反馈草稿等 UI 状态
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { TabKey, SystemPanelKey, FeedbackDraft, FeedbackListItem } from '../state';

export interface UiStoreState {
  activeTab: TabKey;
  activeSystemPanel: SystemPanelKey;
  backendConnected: boolean;
  /** BackendChatSocket 实例本身，不参与持久化也不参与订阅通知（避免循环）。 */
  socket: unknown | null;
  feedbackDraft: FeedbackDraft;
  /** 服务端反馈列表（反馈页“提交记录”数据源）。 */
  feedbackList: FeedbackListItem[];
  editingResend: boolean;
}

export const uiStore: Store<UiStoreState> = createStore<UiStoreState>(
  {
    activeTab: 'chat',
    activeSystemPanel: 'overview',
    backendConnected: false,
    socket: null,
    feedbackDraft: { title: '', description: '', images: [] },
    feedbackList: [],
    editingResend: false,
  },
  'ui',
);
