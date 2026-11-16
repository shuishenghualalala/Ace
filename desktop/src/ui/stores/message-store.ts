/**
 * message-store：每会话消息 + 队列提示 + 待发队列 + 附件
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { Attachment } from '../backend-client';
import type { ChatMessage, PendingMessage } from '../chat-render';

export interface HistoryPagingState {
  /** 窗口化历史是否还有更早页（P1-6 触顶翻页驱动）。 */
  hasMore: boolean;
  /** 下一页游标（后端 next_before 原样保存，对客户端不透明）。 */
  nextBefore: string | null;
}

export interface MessageStoreState {
  messages: Record<string, ChatMessage[]>;
  queueHints: Record<string, string>;
  pendingQueues: Record<string, PendingMessage[]>;
  attachments: Attachment[];
  /** 历史回填失败（且当前无消息可显示）的会话：驱动聊天区「加载失败 + 重试」卡。 */
  historyLoadErrors: Set<string>;
  /** 历史回填进行中的会话：驱动聊天区骨架占位（P1-6）。 */
  historyLoading: Set<string>;
  /** 窗口化历史分页游标（按会话）。 */
  historyPaging: Record<string, HistoryPagingState>;
}

export const messageStore: Store<MessageStoreState> = createStore<MessageStoreState>(
  {
    messages: {},
    queueHints: {},
    pendingQueues: {},
    attachments: [],
    historyLoadErrors: new Set<string>(),
    historyLoading: new Set<string>(),
    historyPaging: {},
  },
  'message',
);
