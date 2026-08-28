/**
 * @vitest-environment happy-dom
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mountNearbyPage } from '../../src/ui/features/nearby-page';
import { conversationAdapters } from '../../src/ui/features/conversation-adapters';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';

describe('companion management hub', () => {
  function setup() {
    __resetAllStoresForTest();
    let eventListener: ((event: { type: string; [key: string]: unknown }) => void) | null = null;
    const command = vi.fn(async () => ({ ok: true as const }));
    const gatewayFetch = vi.fn(async (url: string, init?: { method?: string; body?: string }) => {
      if (url.endsWith('/api/companion/link-state')) {
        const payload = JSON.parse(init?.body ?? '{}') as Record<string, unknown>;
        if (payload.type === 'message') {
          return {
            status: 200,
            statusText: 'OK',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
              ok: true,
              appended: true,
              binding: {
                kind: 'nearby_dm', target_id: 'ace_peer_a',
                session_id: 'agent:main:nearby:dm:test', workspace_id: 'companion', title: '林墨',
                capabilities: {
                  can_send_text: true, can_attach: true, can_mention_people: false,
                  can_mention_agents: false, show_model_picker: false, show_skills: false, show_plan_mode: false,
                },
              },
            }),
          };
        }
      }
      if (url.includes('/api/session/agent%3Amain%3Anearby%3Adm%3Atest')) {
        const body = url.endsWith('/status')
          ? { live: 'idle', last_status: '' }
          : url.endsWith('/plan')
            ? { has_plan: false, active: false }
            : url.endsWith('/todos')
              ? { todos: [] }
              : [{
                role: 'user', content: '实时收到', name: '林墨', message_id: 'remote-live-1',
                origin: {
                  source: 'companion', sender_kind: 'human', sender_id: 'ace_peer_a',
                  sender_name: '林墨', is_self: false, delivery_state: 'delivered',
                },
              }];
        return {
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        };
      }
      if (url.endsWith('/api/companion/conversations')) {
        return {
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            conversations: [{
              kind: 'nearby_dm', target_id: 'ace_peer_a', session_id: 'agent:main:nearby:dm:test',
              workspace_id: 'companion', title: '林墨',
              capabilities: {
                can_send_text: true, can_attach: true, can_mention_people: false,
                can_mention_agents: false, show_model_picker: false, show_skills: false, show_plan_mode: false,
              },
            }],
            peers: [], rooms: [],
          }),
        };
      }
      if (url.endsWith('/api/companion/files/prepare')) {
        return {
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            ok: true,
            file: {
              file_id: 'file-1', name: 'note.txt', path: '/tmp/note.txt', type: 'file',
              mime_type: 'text/plain', size: 5,
              sha256: '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824',
            },
          }),
        };
      }
      if (url.endsWith('/api/companion/conversations/open')) {
        const payload = JSON.parse(init?.body ?? '{}') as Record<string, string>;
        return {
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            ok: true,
            kind: payload.kind,
            target_id: payload.target_id,
            session_id: 'agent:main:nearby:dm:opened',
            workspace_id: payload.workspace_id,
            title: payload.title,
            capabilities: {
              can_send_text: true, can_attach: true, can_mention_people: false,
              can_mention_agents: false, show_model_picker: false, show_skills: false, show_plan_mode: false,
            },
          }),
        };
      }
      if (url.includes('/api/companion/conversations/') && url.endsWith('/messages')) {
        return {
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ ok: true, event_id: 'event-1', status: 'queued' }),
        };
      }
      if (url.endsWith('/api/workspaces')) {
        return {
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify([
            { id: 'companion', name: '同伴空间', description: '', instructions: '' },
            { id: 'project-a', name: 'Ace 项目', description: '', instructions: '', root_path: '/tmp/ace' },
          ]),
        };
      }
      if (url.endsWith('/api/companion/profile')) {
        const published = init?.method === 'PUT'
          ? JSON.parse(init.body ?? '{}').published_agent_refs as string[]
          : ['builtin:crew'];
        return {
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            profile: {},
            public_profile: {},
            agent_candidates: [
              {
                source_ref: 'builtin:crew', source_kind: 'builtin', source_id: 'crew',
                display_name: 'Crew', description: '主 Agent', provider: 'crew', available: true,
                published: published.includes('builtin:crew'), public_agent_id: 'agent_crew',
              },
              {
                source_ref: 'external:codex', source_kind: 'external', source_id: 'codex',
                display_name: 'Codex', description: '外援 Agent', provider: 'acp', available: true,
                published: published.includes('external:codex'), public_agent_id: 'agent_codex',
              },
            ],
          }),
        };
      }
      return {
        status: 200,
        statusText: 'OK',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ok: true }),
      };
    });
    const bridge = {
      nearbyStart: vi.fn(async () => ({ ok: true as const })),
      nearbyStop: vi.fn(async () => ({ ok: true as const })),
      nearbyCommand: command,
      gatewayFetch,
      onNearbyEvent: vi.fn((listener: typeof eventListener) => {
        eventListener = listener;
        return () => { eventListener = null; };
      }),
    } as unknown as Window['Crew'];
    window.Crew = bridge;
    const root = document.createElement('div');
    document.body.replaceChildren(root);
    const page = mountNearbyPage(root, bridge);
    return {
      bridge,
      command,
      gatewayFetch,
      page,
      root,
      emit: (event: { type: string; [key: string]: unknown }) => eventListener?.(event),
    };
  }

  const localPeer = {
    peer_id: 'ace_local',
    display_name: 'AHUAMAO',
    agent_name: 'Crew',
    capabilities: ['chat'],
    published_agents: [],
  };

  const peer = {
    peer_id: 'ace_peer_a',
    display_name: '林墨',
    agent_name: 'Mori',
    capabilities: ['chat', 'file'],
    published_agents: [{
      public_agent_id: 'agent_mori',
      display_name: 'Mori',
      source_kind: 'local',
      source_ref: 'builtin:crew',
      description: '擅长把散乱讨论整理成决策稿',
    }],
  };

  afterEach(() => {
    document.body.replaceChildren();
    vi.restoreAllMocks();
  });

  it('uses the PRD management layout and does not render a second chat surface', () => {
    const { page, root, emit } = setup();
    page.activate();
    emit({ type: 'ready', peer: localPeer, discoverable: true });

    expect(root.querySelector('.companion-rail')).not.toBeNull();
    expect(root.querySelector('.companion-main')).not.toBeNull();
    expect(root.textContent).toContain('遇见附近的 Ace');
    expect(root.textContent).toContain('正在寻找附近的 Ace');
    expect(root.querySelector('.nearby-chat')).toBeNull();
    expect(root.querySelector('.nearby-composer')).toBeNull();
    expect(root.querySelector('.nearby-radar')).toBeNull();
    expect(root.querySelector('.nearby-page__header')).toBeNull();
    page.dispose();
  });

  it('focuses one nearby person card and previews only the published Agent identity', () => {
    const { page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_discovered', peer });

    const card = root.querySelector('.companion-meet-card');
    expect(card?.textContent).toContain('林墨');
    expect(card?.textContent).toContain('Mori');
    expect(card?.textContent).toContain('不可私聊');
    expect(root.querySelectorAll('.companion-meet-card')).toHaveLength(1);

    root.querySelector<HTMLButtonElement>('.companion-meet-card .companion-button.is-primary')?.click();
    expect(root.textContent).toContain('正在连接 林墨');
    page.dispose();
  });

  it('opens a person management page after connection and keeps messaging in the main chat', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_discovered', peer });
    root.querySelector<HTMLButtonElement>('.companion-meet-card .companion-button.is-primary')?.click();
    expect(command).toHaveBeenCalledWith({ type: 'connect_peer', peer_id: 'ace_peer_a' });
    emit({ type: 'peer_connected', peer });

    expect(root.querySelector('.companion-main__header')?.textContent).toContain('同伴资料');
    expect(root.textContent).toContain('人与人的关系和私聊入口都在这里管理');
    expect(root.textContent).toContain('Agent 是对方的群内能力，不是联系人');
    expect(root.textContent).toContain('私聊本人');
    expect(root.textContent).toContain('消息只发送给本人，不会调用随行 Agent');
    expect(root.textContent).toContain('邀请进群');
    expect(root.querySelector('.nearby-composer')).toBeNull();
    page.dispose();
  });

  it('asks for a real workspace before entering the main conversation', async () => {
    const { page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_connected', peer });
    [...root.querySelectorAll<HTMLButtonElement>('.companion-main__header .companion-button')]
      .find((button) => button.textContent === '私聊本人')?.click();

    await vi.waitFor(() => expect(root.querySelectorAll('.companion-workspace-option')).toHaveLength(2));
    const selected = root.querySelector<HTMLInputElement>('.companion-workspace-option input:checked');
    expect(selected?.value).toBe('companion');
    expect(root.querySelector('.companion-workspace-sheet')?.textContent).toContain('Ace 项目');
    expect(root.querySelector('.companion-workspace-sheet')?.textContent).toContain('进入主对话');
    [...root.querySelectorAll<HTMLButtonElement>('.companion-workspace-sheet .companion-button')]
      .find((button) => button.textContent === '取消')?.click();
    page.dispose();
  });

  it('marks a direct main conversation as the companion person', async () => {
    const { gatewayFetch, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_connected', peer });
    [...root.querySelectorAll<HTMLButtonElement>('.companion-main__header .companion-button')]
      .find((button) => button.textContent === '私聊本人')?.click();
    await vi.waitFor(() => expect(root.querySelector('.companion-workspace-sheet')).not.toBeNull());
    [...root.querySelectorAll<HTMLButtonElement>('.companion-workspace-sheet .companion-button')]
      .find((button) => button.textContent === '进入主对话')?.click();
    await vi.waitFor(() => expect(gatewayFetch.mock.calls.some(([url, init]) => (
      url.endsWith('/api/companion/conversations/open')
      && JSON.parse(init?.body ?? '{}').title === '林墨 · 同伴本人'
    ))).toBe(true));
    page.dispose();
  });

  it('disables the message entry while the companion is offline and projects the state', async () => {
    const { gatewayFetch, page, root, emit } = setup();
    await vi.waitFor(() => expect(
      gatewayFetch.mock.calls.some(([url]) => url.endsWith('/api/companion/conversations')),
    ).toBe(true));
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_connected', peer });
    emit({ type: 'peer_disconnected', peer_id: peer.peer_id });

    const button = [...root.querySelectorAll<HTMLButtonElement>('.companion-main__header .companion-button')]
      .find((item) => item.textContent === '本人离线');
    expect(button?.disabled).toBe(true);
    expect(root.textContent).toContain('对方本人重新上线后才能进入主对话');
    button?.click();
    expect(root.querySelector('.companion-workspace-sheet')).toBeNull();
    await vi.waitFor(() => expect(gatewayFetch.mock.calls.some(([url, init]) => (
      url.endsWith('/api/companion/link-state')
      && JSON.parse(init?.body ?? '{}').connection_state === 'disconnected'
    ))).toBe(true));

    const adapter = conversationAdapters.resolve('agent:main:nearby:dm:test');
    expect(adapter?.abilities('agent:main:nearby:dm:test')).toMatchObject({
      canSendText: false,
      canAttach: false,
      unavailableReason: '同伴暂时离线，重新连接后才能发消息',
    });
    await expect(adapter?.send({
      sessionId: 'agent:main:nearby:dm:test',
      text: '不能发送',
      attachments: [],
    })).rejects.toThrow('同伴暂时离线');
    page.dispose();
  });

  it('creates a group by selecting people and never auto-selects an Agent', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_connected', peer });
    root.querySelector<HTMLButtonElement>('[aria-label="创建群聊"]')?.click();

    const sheet = root.querySelector<HTMLElement>('.companion-sheet')!;
    expect(sheet.textContent).toContain('Agent 不会被自动带入');
    const name = sheet.querySelector<HTMLInputElement>('.companion-sheet__name')!;
    name.value = '同伴产品小队';
    sheet.querySelector<HTMLInputElement>('.companion-member-option input')!.click();
    [...sheet.querySelectorAll<HTMLButtonElement>('.companion-button')]
      .find((button) => button.textContent === '创建')?.click();

    expect(command).toHaveBeenCalledWith(expect.objectContaining({
      type: 'create_room',
      room_name: '同伴产品小队',
      peer_ids: ['ace_peer_a'],
      agent_mode: 'mention',
    }));
    page.dispose();
  });

  it('manages the published Crew and external Agents from My Card', async () => {
    const { bridge, gatewayFetch, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    const myCard = [...root.querySelectorAll<HTMLButtonElement>('.companion-rail-row')]
      .find((button) => button.textContent?.includes('我的名片'))!;
    myCard.click();
    await vi.waitFor(() => expect(root.querySelectorAll('.companion-publication-option')).toHaveLength(2));

    const checkboxes = [...root.querySelectorAll<HTMLInputElement>('.companion-publication-option input')];
    expect(checkboxes[0]?.checked).toBe(true);
    expect(checkboxes[1]?.checked).toBe(false);
    checkboxes[1]?.click();
    root.querySelector<HTMLFormElement>('.companion-publications')?.requestSubmit();
    await vi.waitFor(() => expect(bridge.nearbyStop).toHaveBeenCalled());

    const update = gatewayFetch.mock.calls.find(([, init]) => init?.method === 'PUT');
    expect(JSON.parse(update?.[1]?.body ?? '{}')).toEqual({
      published_agent_refs: ['builtin:crew', 'external:codex'],
    });
    page.dispose();
  });

  it('sends main-conversation attachments through the same Companion adapter', async () => {
    const { command, gatewayFetch, page, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_connected', peer });
    await vi.waitFor(() => expect(
      gatewayFetch.mock.calls.some(([url]) => url.endsWith('/api/companion/conversations')),
    ).toBe(true));
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    const adapter = conversationAdapters.resolve('agent:main:nearby:dm:test');
    expect(adapter?.abilities('agent:main:nearby:dm:test').canAttach).toBe(true);
    await adapter?.send({
      sessionId: 'agent:main:nearby:dm:test',
      text: '',
      attachments: [{ id: 'file-1', name: 'note.txt', path: '/tmp/note.txt', type: 'file', size: 5 }],
    });
    expect(command).toHaveBeenCalledWith({
      type: 'send_peer_file', peer_id: 'ace_peer_a', file_id: 'file-1', name: 'note.txt',
      mime_type: 'text/plain', size: 5,
      sha256: '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824',
      file_path: '/tmp/note.txt',
      client_message_id: 'event-1',
    });
    expect(gatewayFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/companion/outbox/event-1/settle'),
      expect.objectContaining({ body: JSON.stringify({ status: 'sent' }) }),
    );
    emit({ type: 'message_delivered', peer_id: 'ace_peer_a', message_id: 'event-1' });
    await vi.waitFor(() => expect(gatewayFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/companion/outbox/event-1/settle'),
      expect.objectContaining({ body: JSON.stringify({ status: 'delivered' }) }),
    ));
    page.dispose();
  });

  it('projects received direct files into the canonical main-conversation history', async () => {
    const { gatewayFetch, page, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_connected', peer });
    emit({
      type: 'message',
      peer_id: 'ace_peer_a',
      message: {
        type: 'peer.file', message_id: 'chunk-1', sender: 'ace_peer_a',
        payload: {
          file: {
            file_id: 'received-1', name: 'hello.txt', mime_type: 'text/plain', size: 5,
            sha256: '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824',
            chunk_index: 0, chunk_total: 1, data_base64: 'aGVsbG8=',
          },
        },
      },
    });
    await vi.waitFor(() => expect(gatewayFetch.mock.calls.some(([, init]) => {
      if (!init?.body) return false;
      const body = JSON.parse(init.body) as { type?: string; file?: { file_id?: string } };
      return body.type === 'file' && body.file?.file_id === 'received-1';
    })).toBe(true));
    page.dispose();
  });

  it('reloads the active main conversation when a remote message arrives', async () => {
    const { page, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_connected', peer });
    sessionStore.set({ activeSessionId: 'agent:main:nearby:dm:test' });

    emit({
      type: 'peer_message_received',
      peer_id: 'ace_peer_a',
      display_name: '林墨',
      text: '实时收到',
      message_id: 'remote-live-1',
      timestamp: 1,
    });

    await vi.waitFor(() => {
      const messages = messageStore.get().messages['agent:main:nearby:dm:test'] ?? [];
      expect(messages).toHaveLength(1);
      expect(messages[0]).toMatchObject({
        id: 'remote-live-1',
        content: '实时收到',
        companionAuthor: { name: '林墨', isSelf: false },
      });
    });
    page.dispose();
  });
});
