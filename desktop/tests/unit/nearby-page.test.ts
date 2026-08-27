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
      nearbyGetSettings: vi.fn(async () => ({ ok: true as const, auto_reply: true, allowed_toolsets: [] })),
      nearbySetSettings: vi.fn(async () => ({ ok: true as const, auto_reply: true, allowed_toolsets: [] })),
      nearbySelectFile: vi.fn(async () => null),
      nearbySaveFile: vi.fn(async () => ({ ok: false, canceled: true })),
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

  const localPeer = {
    peer_id: 'ace_local',
    display_name: 'Mac',
    agent_name: 'Ace Agent',
    capabilities: ['chat'],
  };

  const peer = {
    peer_id: 'ace_peer_a',
    display_name: 'Windows 工作站',
    agent_name: 'Ace Agent',
    capabilities: ['chat'],
  };

  function connectPeer(
    emit: (event: { type: string; [key: string]: unknown }) => void,
    root: HTMLElement,
  ): void {
    emit({ type: 'peer_discovered', peer });
    const card = [...root.querySelectorAll<HTMLElement>('.nearby-peer-card')]
      .find((element) => element.textContent?.includes('Windows 工作站'));
    const action = [...(card?.querySelectorAll<HTMLButtonElement>('.nearby-peer-card__action') ?? [])]
      .find((button) => button.textContent === '连接');
    action?.click();
    emit({ type: 'peer_connected', peer });
  }

  it('discovers a peer, connects, and lights up the direct conversation', () => {
    const { bridge, command, page, root, emit } = setup();
    page.activate();
    expect(bridge.nearbyStart).toHaveBeenCalledTimes(1);
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    emit({ type: 'peer_discovered', peer });

    expect(root.querySelector('.nearby-peer-card__name')?.textContent).toBe('Windows 工作站');
    expect(command).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'connect_peer' }));
    connectPeer(emit, root);
    expect(command).toHaveBeenCalledWith({ type: 'connect_peer', peer_id: 'ace_peer_a' });

    // 连接成功后直聊会话自动生成并点亮
    expect(root.querySelector('.nearby-conv-item__name')?.textContent).toBe('Windows 工作站');
    expect(root.querySelector('.nearby-chat__state')?.textContent).toContain('已连接');
    expect(root.querySelector<HTMLTextAreaElement>('.nearby-composer__input')?.disabled).toBe(false);
    page.dispose();
  });

  it('updates the Bluetooth discoverability preference from the identity card', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: false });
    const toggle = root.querySelector<HTMLInputElement>('.nearby-privacy__toggle')!;
    expect(toggle.checked).toBe(false);
    expect(toggle.disabled).toBe(false);
    toggle.click();
    expect(command).toHaveBeenCalledWith({ type: 'set_discoverable', enabled: true });
    emit({ type: 'discoverability_changed', discoverable: true });
    expect(toggle.checked).toBe(true);
    page.dispose();
  });

  it('sends direct messages to the person by default and to the Agent when mentioned', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);
    const input = root.querySelector<HTMLTextAreaElement>('.nearby-composer__input')!;

    input.value = '来自 Mac 的消息';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    root.querySelector<HTMLFormElement>('.nearby-composer')!.requestSubmit();
    expect(command).toHaveBeenCalledWith({
      type: 'send_peer_message',
      peer_id: 'ace_peer_a',
      text: '来自 Mac 的消息',
      mentions: [],
    });

    // @ 补全里选 Agent 分组 → 走 send_agent_request
    input.value = '@Ace';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const agentOption = [...root.querySelectorAll<HTMLElement>('.nearby-mention__item')]
      .find((item) => item.textContent?.includes('Agent'));
    expect(agentOption).toBeDefined();
    agentOption!.click();
    expect(input.value).toContain('@Ace Agent');
    input.value = `${input.value}总结一下今天进展`;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    root.querySelector<HTMLFormElement>('.nearby-composer')!.requestSubmit();
    expect(command).toHaveBeenCalledWith({
      type: 'send_agent_request',
      peer_id: 'ace_peer_a',
      text: '@Ace Agent 总结一下今天进展',
    });
    page.dispose();
  });

  it('renders the thinking placeholder until the agent response arrives', () => {
    const { page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);
    const input = root.querySelector<HTMLTextAreaElement>('.nearby-composer__input')!;
    input.value = '@Ace';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    [...root.querySelectorAll<HTMLElement>('.nearby-mention__item')]
      .find((item) => item.textContent?.includes('Agent'))?.click();
    input.value = `${input.value}在吗`;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    root.querySelector<HTMLFormElement>('.nearby-composer')!.requestSubmit();

    expect(root.querySelector('.nearby-message--thinking')).not.toBeNull();
    emit({
      type: 'message',
      peer_id: 'ace_peer_a',
      message: {
        type: 'agent.response',
        message_id: 'resp_1',
        sender: 'ace_peer_a',
        payload: { request_id: 'req_1', text: '在的' },
      },
    });
    expect(root.querySelector('.nearby-message--thinking')).toBeNull();
    expect(root.querySelector('.nearby-chat__messages')?.textContent).toContain('在的');
    expect(root.querySelector('.nearby-message--agent')).not.toBeNull();
    page.dispose();
  });

  it('counts unread for inactive conversations and clears on selection', () => {
    const { page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);
    const otherPeer = { ...peer, peer_id: 'ace_peer_b', display_name: 'Linux 盒子' };
    emit({ type: 'peer_discovered', peer: otherPeer });
    emit({ type: 'peer_connected', peer: otherPeer });

    // 选中 Linux 盒子的会话；此时给 Windows 工作站的会话发消息 → 未读角标
    const linuxItem = [...root.querySelectorAll<HTMLElement>('.nearby-conv-item')]
      .find((item) => item.textContent?.includes('Linux 盒子'));
    linuxItem?.click();
    emit({ type: 'peer_message_received', peer_id: 'ace_peer_a', display_name: 'Windows 工作站', text: '看到群了吗', mentions: [], message_id: 'pm_1', timestamp: 100 });
    const items = [...root.querySelectorAll<HTMLElement>('.nearby-conv-item')];
    const windowsItem = items.find((item) => item.textContent?.includes('Windows 工作站'));
    expect(windowsItem?.querySelector('.nearby-conv-item__badge')?.textContent).toBe('1');

    windowsItem?.click();
    expect(root.querySelector('.nearby-conv-item__badge')).toBeNull();
    page.dispose();
  });

  it('creates a room, shows the member panel, and routes room messages with mentions', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);

    const createButton = [...root.querySelectorAll<HTMLButtonElement>('.nearby-scan-button')]
      .find((button) => button.textContent === '+ 建群')!;
    createButton.click();
    const popover = root.querySelector<HTMLElement>('.nearby-popover')!;
    const nameInput = popover.querySelector<HTMLInputElement>('.nearby-popover__input')!;
    nameInput.value = 'XX 项目';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    popover.querySelector<HTMLInputElement>('.nearby-popover__option input')!.click();
    const confirm = [...popover.querySelectorAll<HTMLButtonElement>('.nearby-send-button')]
      .find((button) => button.textContent === '创建')!;
    confirm.click();

    const createCall = command.mock.calls.map(([payload]) => payload as Record<string, unknown>)
      .find((payload) => payload.type === 'create_room');
    expect(createCall).toMatchObject({
      room_name: 'XX 项目',
      peer_ids: ['ace_peer_a'],
      agent_mode: 'mention',
    });
    expect(String(createCall?.room_id)).toMatch(/^[A-Za-z0-9_.:-]{1,120}$/);

    emit({
      type: 'room_created',
      room_id: String(createCall?.room_id),
      room_name: 'XX 项目',
      peer_ids: ['ace_local', 'ace_peer_a'],
      agent_mode: 'mention',
      owner_peer_id: 'ace_local',
    });

    // 群主视角：右栏展示成员层级与可编辑的触发模式
    const panel = root.querySelector<HTMLElement>('.nearby-panel')!;
    expect(panel.hidden).toBe(false);
    expect(panel.textContent).toContain('成员（2）');
    expect(panel.textContent).toContain('└ Ace Agent');
    expect(panel.querySelector<HTMLInputElement>('.nearby-mode-option input')?.disabled).toBe(false);
    expect(panel.textContent).toContain('解散群');

    const input = root.querySelector<HTMLTextAreaElement>('.nearby-composer__input')!;
    input.value = '@Ace';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    [...root.querySelectorAll<HTMLElement>('.nearby-mention__item')]
      .find((item) => item.textContent?.includes('Agent'))?.click();
    input.value = `${input.value}今天进展`;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    root.querySelector<HTMLFormElement>('.nearby-composer')!.requestSubmit();
    expect(command).toHaveBeenCalledWith({
      type: 'send_room_message',
      room_id: String(createCall?.room_id),
      text: '@Ace Agent 今天进展',
      mentions: ['ace_peer_a'],
    });
    // mention 模式 @了对方 → 出现思考中占位
    expect(root.querySelector('.nearby-message--thinking')).not.toBeNull();

    emit({
      type: 'message',
      peer_id: 'ace_peer_a',
      message: {
        type: 'room.message',
        message_id: 'rm_1',
        sender: 'ace_peer_a',
        payload: { room_id: String(createCall?.room_id), text: '进展如下', mentions: ['ace_local'] },
      },
    });
    expect(root.querySelector('.nearby-message--thinking')).toBeNull();
    expect(root.querySelector('.nearby-chat__messages')?.textContent).toContain('进展如下');
    page.dispose();
  });

  it('invites a connected peer into an owned room via the invite_to_room command', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);
    emit({
      type: 'room_created',
      room_id: 'room_1',
      room_name: 'XX 项目',
      peer_ids: ['ace_local', 'ace_peer_a'],
      agent_mode: 'mention',
      owner_peer_id: 'ace_local',
    });
    // 再接入一名不在群里的同伴，作为可邀请候选
    const otherPeer = { ...peer, peer_id: 'ace_peer_b', display_name: 'Linux 盒子' };
    emit({ type: 'peer_discovered', peer: otherPeer });
    emit({ type: 'peer_connected', peer: otherPeer });

    const panel = root.querySelector<HTMLElement>('.nearby-panel')!;
    const inviteButton = [...panel.querySelectorAll<HTMLButtonElement>('.nearby-panel__invite')]
      .find((button) => button.textContent === '邀请成员')!;
    inviteButton.click();
    const popover = root.querySelector<HTMLElement>('.nearby-popover')!;
    const option = [...popover.querySelectorAll<HTMLElement>('.nearby-popover__option')]
      .find((row) => row.textContent?.includes('Linux 盒子'))!;
    option.querySelector<HTMLInputElement>('input')!.click();
    const confirm = [...popover.querySelectorAll<HTMLButtonElement>('.nearby-send-button')]
      .find((button) => button.textContent === '邀请')!;
    confirm.click();

    expect(command).toHaveBeenCalledWith({ type: 'invite_to_room', room_id: 'room_1', peer_ids: ['ace_peer_b'] });
    page.dispose();
  });

  it('lets the owner rename the room inline and reflects the rename system message', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);
    emit({
      type: 'room_created',
      room_id: 'room_1',
      room_name: 'XX 项目',
      peer_ids: ['ace_local', 'ace_peer_a'],
      agent_mode: 'mention',
      owner_peer_id: 'ace_local',
    });

    const nameInput = root.querySelector<HTMLInputElement>('.nearby-panel__name-input')!;
    expect(nameInput.value).toBe('XX 项目');
    nameInput.value = '新群名';
    nameInput.dispatchEvent(new Event('change'));
    expect(command).toHaveBeenCalledWith({ type: 'set_room_agent_mode', room_id: 'room_1', room_name: '新群名' });

    emit({ type: 'room_settings_updated', room_id: 'room_1', room_name: '新群名', agent_mode: 'mention' });
    expect(root.querySelector('.nearby-chat__name')?.textContent).toBe('新群名');
    expect([...root.querySelectorAll<HTMLElement>('.nearby-conv-item')]
      .find((item) => item.textContent?.includes('新群名'))).toBeDefined();
    expect(root.querySelector('.nearby-chat__messages')?.textContent).toContain('群主修改了群名为「新群名」');
    page.dispose();
  });

  it('refreshes the member list and posts system messages on member join/leave events', () => {
    const { page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);
    emit({
      type: 'room_created',
      room_id: 'room_1',
      room_name: 'XX 项目',
      peer_ids: ['ace_local', 'ace_peer_a'],
      agent_mode: 'mention',
      owner_peer_id: 'ace_local',
    });
    const panel = root.querySelector<HTMLElement>('.nearby-panel')!;
    expect(panel.textContent).toContain('成员（2）');

    emit({ type: 'room_member_joined', room_id: 'room_1', peer_id: 'ace_peer_c', display_name: 'iPad mini' });
    expect(panel.textContent).toContain('成员（3）');
    expect(root.querySelector('.nearby-chat__messages')?.textContent).toContain('iPad mini 加入了群聊');

    emit({ type: 'room_member_left', room_id: 'room_1', peer_id: 'ace_peer_c', display_name: 'iPad mini' });
    expect(panel.textContent).toContain('成员（2）');
    expect(root.querySelector('.nearby-chat__messages')?.textContent).toContain('iPad mini 退出了群聊');
    page.dispose();
  });

  it('disables the composer when the direct peer disconnects but keeps the conversation', () => {
    const { page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    connectPeer(emit, root);
    emit({ type: 'peer_disconnected', peer_id: 'ace_peer_a' });
    expect(root.querySelector<HTMLTextAreaElement>('.nearby-composer__input')?.disabled).toBe(true);
    expect(root.querySelector('.nearby-chat__state')?.textContent).toContain('已断开');
    expect(root.querySelector('.nearby-conv-item__name')?.textContent).toBe('Windows 工作站');
    page.dispose();
  });

  it('pauses and resumes discovery without changing discoverability', () => {
    const { command, page, root, emit } = setup();
    emit({ type: 'ready', peer: localPeer, discoverable: true });
    const scan = [...root.querySelectorAll<HTMLButtonElement>('.nearby-scan-button')]
      .find((button) => button.textContent === '停止查找')!;
    scan.click();
    expect(command).toHaveBeenCalledWith({ type: 'stop_discovery' });
    emit({ type: 'discovery_stopped' });
    expect(scan.textContent).toBe('开始查找');
    scan.click();
    expect(command).toHaveBeenLastCalledWith({ type: 'start_discovery' });
    page.dispose();
  });
});
