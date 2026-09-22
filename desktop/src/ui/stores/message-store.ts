/**
 * message-store：每会话消息 + 队列提示 + 待发队列 + 附件
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { Attachment } from '../backend-client';
import type { ChatMessage, PendingMessage } from '../chat-render';

export interface MessageStoreState {
  messages: Record<string, ChatMessage[]>;
  queueHints: Record<string, string>;
  pendingQueues: Record<string, PendingMessage[]>;
  attachments: Attachment[];
  /** 历史回填失败（且当前无消息可显示）的会话：驱动聊天区「加载失败 + 重试」卡。 */
  historyLoadErrors: Set<string>;
}

export const messageStore: Store<MessageStoreState> = createStore<MessageStoreState>(
  {
    messages: {},
    queueHints: {},
    pendingQueues: {},
    attachments: [],
    historyLoadErrors: new Set<string>(),
  },
  'message',
);
