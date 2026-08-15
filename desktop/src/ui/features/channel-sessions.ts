/**
 * 渠道会话侧栏：绑定者可见的平台分组列表。
 */

import { backendApi } from '../backend-client';
import { state, type SessionRow } from '../state';

export interface ChannelSessionGroup {
  platform: string;
  label: string;
  sessions: SessionRow[];
}

function mapChannelSession(row: {
  session_id: string;
  title?: string;
  updated_at: number;
  workspace_id?: string;
  platform?: string;
}): SessionRow {
  return {
    id: row.session_id,
    title: row.title?.trim() || '渠道对话',
    updatedAt: row.updated_at * 1000,
    preview: '',
    badge: row.platform || '渠道',
    workspaceId: row.workspace_id || 'default',
    titleFromSummary: false,
    archived: false,
    pinned: false,
    ...(row.platform ? { channelPlatform: row.platform } : {}),
  };
}

export function syncChannelSessionsFromBackend(
  groups: Array<{
    platform: string;
    label: string;
    sessions: Array<{ session_id: string; title?: string; updated_at: number; workspace_id?: string; platform?: string }>;
  }>,
): void {
  state.channelSessionGroups = groups.map((g) => ({
    platform: g.platform,
    label: g.label,
    sessions: g.sessions.map((s) => mapChannelSession({ ...s, platform: g.platform })),
  }));
}

export async function loadChannelSessions(): Promise<void> {
  if (!state.backendConnected) return;
  try {
    const { platforms } = await backendApi.channelSessions();
    syncChannelSessionsFromBackend(platforms ?? []);
  } catch {
    state.channelSessionGroups = [];
  }
}

export function isChannelSessionId(sessionId: string | null | undefined): boolean {
  return !!sessionId && sessionId.startsWith('agent:main:');
}

export function findChannelSession(sessionId: string): SessionRow | undefined {
  for (const group of state.channelSessionGroups) {
    const hit = group.sessions.find((s) => s.id === sessionId);
    if (hit) return hit;
  }
  return undefined;
}

/** WS 事件：刷新渠道分组；若正在查看该 session 由 chat-controller 另行拉 history。 */
export async function refreshChannelSessionsOnEvent(platform?: string, sessionId?: string): Promise<void> {
  await loadChannelSessions();
  if (sessionId && state.activeSessionId === sessionId) {
    window.dispatchEvent(new CustomEvent('channel-session:updated', { detail: { platform, sessionId } }));
  }
}
