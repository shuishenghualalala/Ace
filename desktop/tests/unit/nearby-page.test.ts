/**
 * @vitest-environment happy-dom
 */
import { describe, expect, it, vi } from 'vitest';
import { mountNearbyPage } from '../../src/ui/features/nearby-page';

describe('nearby page', () => {
  function setup() {
    let eventListener: ((event: { type: string; [key: string]: unknown }) => void) | null = null;
    const command = vi.fn(async () => ({ ok: true as const }));
    const bridge = {
      nearbyStart: vi.fn(async () => ({ ok: true as const })),
      nearbyStop: vi.fn(async () => ({ ok: true as const })),
      nearbyCommand: command,
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
      page,
      root,
      emit: (event: { type: string; [key: string]: unknown }) => eventListener?.(event),
    };
  }

  const peer = {
    peer_id: 'ace_peer_a',
    display_name: 'Windows 工作站',
    agent_name: 'Ace Agent',
    capabilities: ['chat'],
  };

  it('discovers a peer and connects only after the user chooses it', () => {
    const { bridge, command, page, root, emit } = setup();
    page.activate();
    expect(bridge.nearbyStart).toHaveBeenCalledTimes(1);
    emit({ type: 'ready', peer: { ...peer, peer_id: 'ace_local' }, discoverable: true });
    emit({ type: 'peer_discovered', peer });

    expect(root.querySelector('.nearby-peer-card__name')?.textContent).toBe('Windows 工作站');
    expect(command).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'connect_peer' }));
    root.querySelector<HTMLElement>('.nearby-peer-card__action')!.click();
    expect(command).toHaveBeenCalledWith({ type: 'connect_peer', peer_id: 'ace_peer_a' });
    expect(root.querySelector('.nearby-peer-card__connection')?.textContent).toBe('正在连接');

    emit({ type: 'peer_connected', peer });
    expect(root.querySelector('.nearby-chat__state')?.textContent).toContain('已连接');
    expect(root.querySelector<HTMLTextAreaElement>('.nearby-composer__input')?.disabled).toBe(false);
    page.dispose();
  });

  it('updates the Bluetooth discoverability preference', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: { ...peer, peer_id: 'ace_local' }, discoverable: false });
    const toggle = root.querySelector<HTMLInputElement>('.nearby-privacy__toggle')!;
    expect(toggle.checked).toBe(false);
    expect(toggle.disabled).toBe(false);
    toggle.click();
    expect(command).toHaveBeenCalledWith({ type: 'set_discoverable', enabled: true });
    emit({ type: 'discoverability_changed', discoverable: true });
    expect(toggle.checked).toBe(true);
    page.dispose();
  });

  it('sends and receives one-to-one chat messages', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: { ...peer, peer_id: 'ace_local' } });
    emit({ type: 'peer_connected', peer });
    const input = root.querySelector<HTMLTextAreaElement>('.nearby-composer__input')!;
    input.value = '来自 Mac 的消息';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    root.querySelector<HTMLFormElement>('.nearby-composer')!.requestSubmit();
    expect(command).toHaveBeenCalledWith({
      type: 'send_agent_request',
      peer_id: 'ace_peer_a',
      text: '来自 Mac 的消息',
    });

    emit({
      type: 'message',
      peer_id: 'ace_peer_a',
      message: {
        message_id: 'local_1',
        sender: 'ace_local',
        message_type: 'agent.request',
        payload: { text: '来自 Mac 的消息' },
      },
    });
    emit({
      type: 'message',
      peer_id: 'ace_peer_a',
      message: {
        message_id: 'remote_1',
        sender: 'ace_peer_a',
        message_type: 'agent.response',
        payload: { request_id: 'local_1', text: 'Windows 已收到' },
      },
    });
    expect(root.querySelector('.nearby-chat__messages')?.textContent).toContain('来自 Mac 的消息');
    expect(root.querySelector('.nearby-chat__messages')?.textContent).toContain('Windows 已收到');
    expect(root.querySelectorAll('.nearby-message--own')).toHaveLength(1);
    page.dispose();
  });

  it('disconnects the active peer and marks removed discoveries unavailable', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: { ...peer, peer_id: 'ace_local' } });
    emit({ type: 'peer_connected', peer });
    root.querySelector<HTMLButtonElement>('.nearby-secondary-action')!.click();
    expect(command).toHaveBeenCalledWith({ type: 'disconnect_peer', peer_id: 'ace_peer_a' });
    emit({ type: 'peer_disconnected', peer_id: 'ace_peer_a' });
    expect(root.querySelector('.nearby-chat__state')?.textContent).toContain('连接已断开');
    emit({ type: 'peer_unavailable', peer_id: 'ace_peer_a' });
    expect(root.querySelector('.nearby-peer-card__connection')?.textContent).toBe('已离开');
    page.dispose();
  });

  it('pauses and resumes discovery without changing discoverability', () => {
    const { command, page, root, emit } = setup();
    const scan = root.querySelector<HTMLButtonElement>('.nearby-scan-button')!;
    scan.click();
    expect(command).toHaveBeenCalledWith({ type: 'stop_discovery' });
    emit({ type: 'discovery_stopped' });
    expect(scan.textContent).toBe('重新查找');
    scan.click();
    expect(command).toHaveBeenLastCalledWith({ type: 'start_discovery' });
    page.dispose();
  });
});
