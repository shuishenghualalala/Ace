/**
 * @vitest-environment happy-dom
 *
 * 阶段 3+4：会话置顶（pinned）+ 归档（archived）前端契约测试。
 *
 * 覆盖：
 * - pinned 会话在主列表排序最前（pinned DESC, updatedAt DESC）
 * - pinned 行 DOM 带 history-item--pinned class（视觉标识）
 * - pinned 状态变化后回收行 class 同步更新（不重建节点）
 * - syncSessionsFromBackend 透传 archived/pinned 字段（后端缺省视为 false）
 * - backendApi.archiveSession / pinSession 调用正确端点与 payload
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, sessionStore } from '../../src/ui/stores/stores';
import type { SessionRow } from '../../src/ui/state';
import { renderWorkspaceHistory, syncSessionsFromBackend } from '../../src/ui/features/workspaces';
import { backendApi } from '../../src/ui/backend-client';
import type { BackendSession } from '../../src/ui/backend-client';
import { mountHistoryList } from './helpers/history-list';

function makeSession(id: string, workspaceId = 'default', extra: Partial<SessionRow> = {}): SessionRow {
  return { id, title: id, workspaceId, updatedAt: 1000, preview: '', badge: '', ...extra };
}

function backendRow(id: string, extra: Partial<BackendSession> = {}): BackendSession {
  return {
    session_id: id,
    title: id,
    message_count: 1,
    updated_at: 1,
    workspace_id: 'default',
    ...extra,
  };
}

beforeEach(() => __resetAllStoresForTest());

describe('置顶会话排序', () => {
  it('pinned 会话排在最新的未置顶会话之前', () => {
    sessionStore.set({
      sessions: [
        makeSession('old_pinned', 'default', { pinned: true, updatedAt: 1000 }),
        makeSession('recent_normal', 'default', { pinned: false, updatedAt: 9000 }),
      ],
    });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const rows = list.querySelectorAll('.conversations-list [data-session-id]');
    expect(rows[0].getAttribute('data-session-id')).toBe('old_pinned');
    expect(rows[1].getAttribute('data-session-id')).toBe('recent_normal');
  });

  it('两条 pinned 都置顶时仍按 updatedAt 倒序', () => {
    sessionStore.set({
      sessions: [
        makeSession('p1', 'default', { pinned: true, updatedAt: 1000 }),
        makeSession('p2', 'default', { pinned: true, updatedAt: 5000 }),
      ],
    });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const rows = list.querySelectorAll('.conversations-list [data-session-id]');
    expect(rows[0].getAttribute('data-session-id')).toBe('p2');
    expect(rows[1].getAttribute('data-session-id')).toBe('p1');
  });

  it('取消置顶后恢复纯 updatedAt 倒序', () => {
    sessionStore.set({
      sessions: [
        makeSession('p1', 'default', { pinned: true, updatedAt: 1000 }),
        makeSession('n1', 'default', { pinned: false, updatedAt: 9000 }),
      ],
    });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    expect(list.querySelectorAll('.conversations-list [data-session-id]')[0].getAttribute('data-session-id'))
      .toBe('p1');
    // 取消置顶
    sessionStore.set({
      sessions: [
        makeSession('p1', 'default', { pinned: false, updatedAt: 1000 }),
        makeSession('n1', 'default', { pinned: false, updatedAt: 9000 }),
      ],
    });
    renderWorkspaceHistory(() => {});
    expect(list.querySelectorAll('.conversations-list [data-session-id]')[0].getAttribute('data-session-id'))
      .toBe('n1');
  });
});

describe('置顶视觉标识', () => {
  it('pinned 行带 history-item--pinned class', () => {
    sessionStore.set({
      sessions: [
        makeSession('p1', 'default', { pinned: true }),
        makeSession('n1', 'default', { pinned: false }),
      ],
    });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const pinnedRow = list.querySelector('[data-session-id="p1"]') as HTMLElement;
    const normalRow = list.querySelector('[data-session-id="n1"]') as HTMLElement;
    expect(pinnedRow.classList.contains('history-item--pinned')).toBe(true);
    expect(normalRow.classList.contains('history-item--pinned')).toBe(false);
  });

  it('回收行：pinned 状态变化后 class 同步更新，节点身份保持', () => {
    sessionStore.set({ sessions: [makeSession('s1', 'default', { pinned: false })] });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const rowBefore = list.querySelector('[data-session-id="s1"]') as HTMLElement;
    expect(rowBefore.classList.contains('history-item--pinned')).toBe(false);

    // 同 sid 切换为 pinned：reconciler 复用节点，仅 toggled class
    sessionStore.set({ sessions: [makeSession('s1', 'default', { pinned: true })] });
    renderWorkspaceHistory(() => {});
    const rowAfter = list.querySelector('[data-session-id="s1"]') as HTMLElement;
    expect(rowAfter).toBe(rowBefore);
    expect(rowAfter.classList.contains('history-item--pinned')).toBe(true);
  });
});

describe('syncSessionsFromBackend 透传 archived/pinned', () => {
  it('ACP 会话使用后端智能体模型，且不被 Crew 会话模型标签覆盖', () => {
    syncSessionsFromBackend([
      backendRow('kimi', {
        model_label: 'DeepSeek',
        agent_label: {
          name: 'Kimi',
          provider: 'kimi',
          display_badge: 'K',
          model: 'kimi-k2.5',
        },
      }),
      backendRow('crew', {
        model_label: 'DeepSeek',
        agent_label: { name: 'Crew', provider: 'crew', display_badge: 'M' },
      }),
    ]);
    const byId = Object.fromEntries(sessionStore.get().sessions.map((session) => [session.id, session]));
    expect(byId.kimi.modelLabel).toBe('kimi-k2.5');
    expect(byId.kimi.agentLabel).toEqual({
      name: 'Kimi',
      provider: 'kimi',
      display_badge: 'K',
      model: 'kimi-k2.5',
    });
    expect(byId.crew.modelLabel).toBe('DeepSeek');
  });

  it('后端返回 archived=true/pinned=true 时写入 sessions', () => {
    syncSessionsFromBackend([
      backendRow('s1', { archived: true, pinned: false }),
      backendRow('s2', { archived: false, pinned: true }),
      backendRow('s3'), // 旧后端不返回这两个字段
    ]);
    const sessions = sessionStore.get().sessions;
    const byId = Object.fromEntries(sessions.map((s) => [s.id, s]));
    expect(byId.s1.archived).toBe(true);
    expect(byId.s1.pinned).toBe(false);
    expect(byId.s2.archived).toBe(false);
    expect(byId.s2.pinned).toBe(true);
    // 缺省视为 false（向后兼容旧后端）
    expect(byId.s3.archived).toBe(false);
    expect(byId.s3.pinned).toBe(false);
  });

  it('标题占位/已有会话字段保留既有 syncSessionsFromBackend 行为', () => {
    // 先灌一条带 titleFromSummary 的会话
    sessionStore.set({
      sessions: [makeSession('s1', 'default', { title: '已有摘要标题', titleFromSummary: true })],
    });
    // 后端列表回传占位标题，前端 title 应被保留
    syncSessionsFromBackend([backendRow('s1', { title: '新会话' })]);
    expect(sessionStore.get().sessions[0].title).toBe('已有摘要标题');
    expect(sessionStore.get().sessions[0].titleFromSummary).toBe(true);
  });
});

describe('backendApi.archiveSession / pinSession', () => {
  it('archiveSession 发 PUT /api/session/{id}/archive，body={archived}', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, archived: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    try {
      const result = await backendApi.archiveSession('sid-1', true);
      expect(result).toEqual({ ok: true, archived: true });
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      const [url, init] = fetchSpy.mock.calls[0];
      expect(String(url)).toContain('/api/session/sid-1/archive');
      expect(init?.method).toBe('PUT');
      expect(init?.body).toBe(JSON.stringify({ archived: true }));
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('pinSession 发 PUT /api/session/{id}/pin，body={pinned}', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, pinned: false }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    try {
      const result = await backendApi.pinSession('sid-2', false);
      expect(result).toEqual({ ok: true, pinned: false });
      const [url, init] = fetchSpy.mock.calls[0];
      expect(String(url)).toContain('/api/session/sid-2/pin');
      expect(init?.method).toBe('PUT');
      expect(init?.body).toBe(JSON.stringify({ pinned: false }));
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('sessions(opts.includeArchived) 透传 include_archived=true query', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    try {
      await backendApi.sessions(undefined, { includeArchived: true });
      const url = String(fetchSpy.mock.calls[0][0]);
      expect(url).toContain('include_archived=true');
      // 默认不传时不应包含
      await backendApi.sessions();
      const urlDefault = String(fetchSpy.mock.calls[1][0]);
      expect(urlDefault).not.toContain('include_archived');
    } finally {
      fetchSpy.mockRestore();
    }
  });
});
