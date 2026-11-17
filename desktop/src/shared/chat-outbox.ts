export const MAX_CHAT_FRAME_BYTES = 4 * 1024 * 1024;

export interface DesktopChatOutboxEntry {
  requestId: string;
  sessionId: string;
  phase: 'queued' | 'submitted' | 'accepted' | 'withdrawing';
  value: Record<string, unknown>;
}

export interface DesktopChatOutboxSnapshot {
  scope: string;
  entries: DesktopChatOutboxEntry[];
}
