/**
 * @vitest-environment happy-dom
 */
import { describe, expect, it, vi } from 'vitest';
import { mountNearbyPage } from '../../src/ui/features/nearby-page';

describe('nearby page', () => {
  function setup() {
    let eventListener: ((event: { type: string; [key: string]: unknown }) => void) | null = null;
    const command = vi.fn(async () => ({ ok: true as const }));
    const nearbySelectFile = vi.fn(async () => null);
    const nearbySaveFile = vi.fn(async () => ({ ok: true, canceled: false, path: '/tmp/example.txt' }));
    const bridge = {
      nearbyStart: vi.fn(async () => ({ ok: true as const })),
      nearbyStop: vi.fn(async () => ({ ok: true as const })),
      nearbyCommand: command,
      nearbySelectFile,
      nearbySaveFile,
      onNearbyEvent: vi.fn((listener: typeof eventListener) => {
        eventListener = listener;
        return () => { eventListener = null; };
      }),
    } as unknown as Window['Crew'];
    const root = document.createElement('div');
    document.body.replaceChildren(root);
    const page = mountNearbyPage(root, bridge);
    return {
      bridge,
      command,
      nearbySelectFile,
      nearbySaveFile,
      page,
      root,
      emit: (event: { type: string; [key: string]: unknown }) => eventListener?.(event),
    };
  }

  const peer = {
    peer_id: 'crew_peer_a',
    display_name: 'Agent A',
    agent_name: 'Researcher',
    capabilities: ['chat'],
    connection: 'connected',
  };

  it('starts discovery, renders connected peers, and creates a selected room', async () => {
    const { bridge, command, page, root, emit } = setup();
    page.activate();
    expect(bridge.nearbyStart).toHaveBeenCalledTimes(1);
    emit({ type: 'ready', peer: { ...peer, peer_id: 'crew_local' } });
    emit({ type: 'peer_connected', peer });

    const checkbox = root.querySelector<HTMLInputElement>('.nearby-peer-card input');
    expect(checkbox).not.toBeNull();
    checkbox!.click();
    root.querySelector<HTMLButtonElement>('.nearby-primary-action')!.click();

    expect(command).toHaveBeenCalledWith(expect.objectContaining({
      type: 'create_room',
      peer_ids: ['crew_peer_a'],
    }));
    page.dispose();
  });

  it('shows the discoverability preference and keeps remote workspaces private', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: { ...peer, peer_id: 'crew_local' }, discoverable: false });
    emit({ type: 'peer_discovered', peer: { ...peer, connection: 'discovered' } });

    const toggle = root.querySelector<HTMLInputElement>('.nearby-privacy__toggle');
    expect(toggle?.checked).toBe(false);
    expect(toggle?.disabled).toBe(false);
    expect(root.querySelector('.nearby-peer-card__workspace')?.textContent).toContain('对方本机私有');

    toggle?.click();
    expect(command).toHaveBeenCalledWith({ type: 'set_discoverable', enabled: true });
    emit({ type: 'discoverability_changed', discoverable: true });
    expect(toggle?.checked).toBe(true);
    page.dispose();
  });

  it('enters a room and sends room messages through IPC', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: { ...peer, peer_id: 'crew_local' } });
    emit({ type: 'room_joined', room_id: 'room_1', room_name: '测试群聊', peer_ids: ['crew_local', 'crew_peer_a'] });
    expect(root.querySelector('.nearby-room')?.hidden).toBe(false);
    const input = root.querySelector<HTMLInputElement>('.nearby-room__input')!;
    input.value = 'hello';
    root.querySelector<HTMLFormElement>('.nearby-room__form')!.requestSubmit();
    expect(command).toHaveBeenCalledWith(expect.objectContaining({
      type: 'send_room_message',
      room_id: 'room_1',
      text: 'hello',
      mentions: [],
    }));
    emit({
      type: 'message',
      peer_id: 'crew_peer_a',
      message: { message_id: 'm1', sender: 'crew_peer_a', message_type: 'room.message', payload: { room_id: 'room_1', text: 'hi' } },
    });
    expect(root.querySelector('.nearby-room__messages')?.textContent).toContain('hi');
    page.dispose();
  });

  it('renders a WeChat-style room row, member panel, and composer state', () => {
    const { page, root, emit } = setup();
    emit({ type: 'ready', peer: { ...peer, peer_id: 'crew_local' } });
    emit({ type: 'peer_connected', peer });
    const send = root.querySelector<HTMLButtonElement>('.nearby-room__form [type="submit"]');
    expect(send?.disabled).toBe(true);

    emit({ type: 'room_joined', room_id: 'room_1', room_name: '项目讨论组', peer_ids: ['crew_local', 'crew_peer_a'] });
    expect(root.querySelector('.nearby-room-list__item')?.textContent).toContain('项目讨论组');
    expect(root.querySelector('.nearby-member-panel')?.hidden).toBe(true);

    root.querySelector<HTMLButtonElement>('.nearby-room__header-actions button')!.click();
    expect(root.querySelector('.nearby-member-panel')?.hidden).toBe(false);
    expect(root.querySelector('.nearby-member-panel')?.textContent).toContain('Agent A');

    const input = root.querySelector<HTMLInputElement>('.nearby-room__input')!;
    input.value = '准备发送';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    expect(send?.disabled).toBe(false);
    emit({ type: 'peer_disconnected', peer_id: 'crew_peer_a' });
    expect(root.querySelector('.nearby-member-panel')?.textContent).toContain('已断开');
    page.dispose();
  });

  it('sends mentions and reply references with a room message', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: { ...peer, peer_id: 'crew_local' } });
    emit({ type: 'peer_connected', peer });
    emit({ type: 'room_joined', room_id: 'room_1', room_name: '测试群聊', peer_ids: ['crew_local', 'crew_peer_a'] });

    root.querySelector<HTMLButtonElement>('.nearby-composer-action')!.click();
    root.querySelector<HTMLButtonElement>('.nearby-mention-menu__item')!.click();
    const input = root.querySelector<HTMLInputElement>('.nearby-room__input')!;
    input.value += '请看这里';
    root.querySelector<HTMLFormElement>('.nearby-room__form')!.requestSubmit();
    expect(command).toHaveBeenCalledWith(expect.objectContaining({
      type: 'send_room_message',
      mentions: ['crew_peer_a'],
    }));

    emit({
      type: 'message',
      peer_id: 'crew_peer_a',
      message: { message_id: 'm1', sender: 'crew_peer_a', message_type: 'room.message', payload: { room_id: 'room_1', text: '请回复我' } },
    });
    root.querySelector<HTMLButtonElement>('.nearby-message__reply-action')!.click();
    input.value = '收到';
    root.querySelector<HTMLFormElement>('.nearby-room__form')!.requestSubmit();
    expect(command).toHaveBeenLastCalledWith(expect.objectContaining({
      type: 'send_room_message',
      reply_to: { message_id: 'm1', sender: 'crew_peer_a', text: '请回复我' },
    }));
    page.dispose();
  });

  it('sends file metadata and reassembles out-of-order file chunks', async () => {
    const { command, nearbySaveFile, nearbySelectFile, page, root, emit } = setup();
    nearbySelectFile.mockResolvedValue({
      file_id: 'file_1',
      name: 'notes.txt',
      mime_type: 'text/plain',
      size: 7,
      sha256: 'a'.repeat(64),
      data_base64: 'YWJjZGVm',
    });
    emit({ type: 'ready', peer: { ...peer, peer_id: 'crew_local' } });
    emit({ type: 'room_joined', room_id: 'room_1', room_name: '测试群聊', peer_ids: ['crew_local', 'crew_peer_a'] });
    root.querySelector<HTMLButtonElement>('.nearby-composer-action:nth-of-type(2)')!.click();
    await Promise.resolve();

    expect(command).toHaveBeenCalledWith(expect.objectContaining({
      type: 'send_room_file',
      room_id: 'room_1',
      file_id: 'file_1',
      name: 'notes.txt',
      data_base64: 'YWJjZGVm',
    }));

    const chunk = (index: number, data: string) => ({
      message_id: `chunk_${index}`,
      sender: 'crew_peer_a',
      message_type: 'room.file',
      payload: {
        room_id: 'room_1',
        file: {
          file_id: 'file_1',
          name: 'notes.txt',
          mime_type: 'text/plain',
          size: 7,
          sha256: 'a'.repeat(64),
          chunk_index: index,
          chunk_total: 2,
          data_base64: data,
        },
      },
    });
    emit({ type: 'message', peer_id: 'crew_peer_a', message: chunk(1, 'ZGVm') });
    expect(root.querySelector('.nearby-message__file')).toBeNull();
    emit({ type: 'message', peer_id: 'crew_peer_a', message: chunk(0, 'YWJj') });
    expect(root.querySelector('.nearby-message__file-name')?.textContent).toBe('notes.txt');
    root.querySelector<HTMLButtonElement>('.nearby-message__file-save')!.click();
    await Promise.resolve();
    expect(nearbySaveFile).toHaveBeenCalledWith({
      name: 'notes.txt',
      mime_type: 'text/plain',
      size: 7,
      sha256: 'a'.repeat(64),
      data_base64: 'YWJjZGVm',
    });
    page.dispose();
  });
});
