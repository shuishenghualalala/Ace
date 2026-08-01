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
}

export const messageStore: Store<MessageStoreState> = createStore<MessageStoreState>(
  {
    messages: {},
    queueHints: {},
    pendingQueues: {},
    attachments: [],
  },
  'message',
);
