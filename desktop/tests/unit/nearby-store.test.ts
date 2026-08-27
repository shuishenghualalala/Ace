import { describe, expect, it } from 'vitest';
import {
  NearbyStore,
  dmConversationId,
  roomConversationId,
} from '../../src/ui/features/nearby-store';

const localPeer = {
  peer_id: 'ace_local',
  display_name: 'Mac',
  agent_name: 'Ace Agent',
  capabilities: ['chat'],
};

const remotePeer = {
  peer_id: 'ace_remote',
  display_name: 'Windows 工作站',
  agent_name: 'Ace Agent',
  capabilities: ['chat'],
};

function readyStore(): NearbyStore {
  const store = new NearbyStore();
  store.applyEvent({ type: 'ready', peer: localPeer, discoverable: true });
  return store;
}

function roomMessage(sender: string, text: string, messageId: string, mentions: string[] = []) {
  return {
    type: 'message',
    peer_id: sender,
    message: {
      type: 'room.message',
      message_id: messageId,
      sender,
      payload: { room_id: 'room_1', text, mentions },
    },
  };
}

describe('NearbyStore history hydration', () => {
  it('hydrates rooms and dms from history_snapshot without counting unread', () => {
    const store = readyStore();
    store.applyEvent({
      type: 'history_snapshot',
      rooms: [{
        room_id: 'room_1',
        room_name: '项目群',
        agent_mode: 'auto',
        peer_ids: ['ace_local', 'ace_remote'],
        messages: [{
          type: 'room.message',
          message_id: 'room_msg_1',
          sender: 'ace_remote',
          payload: { room_id: 'room_1', text: '早上好', mentions: [] },
        }],
      }],
      dms: [{
        peer_id: 'ace_remote',
        messages: [
          { type: 'peer.message', message_id: 'dm_1', sender: 'ace_remote', payload: { text: '在吗', mentions: [] } },
          { type: 'agent.request', message_id: 'dm_2', sender: 'ace_local', payload: { text: '总结一下' } },
          { type: 'agent.response', message_id: 'dm_3', sender: 'ace_remote', payload: { request_id: 'dm_2', text: '总结如下' } },
        ],
      }],
    });

    const room = store.conversations.get(roomConversationId('room_1'));
    expect(room).toMatchObject({ kind: 'room', title: '项目群', agentMode: 'auto', unread: 0 });
    expect(store.conversationMessages(roomConversationId('room_1'))).toHaveLength(1);

    const dm = store.conversations.get(dmConversationId('ace_remote'));
    expect(dm).toMatchObject({ kind: 'dm', peerId: 'ace_remote', unread: 0 });
    const messages = store.conversationMessages(dmConversationId('ace_remote'));
    // 直聊合流：peer.message 与 agent.request/response 进入同一会话，保持到达顺序
    expect(messages.map((message) => message.kind)).toEqual(['text', 'text', 'agent']);
    expect(messages.map((message) => message.isOwn)).toEqual([false, true, false]);
  });

  it('reassembles chunked room files into a single file message', () => {
    const store = readyStore();
    const chunk = (index: number, total: number, data: string) => ({
      type: 'room.file',
      message_id: `file_chunk_${index}`,
      sender: 'ace_remote',
      payload: {
        room_id: 'room_1',
        mentions: [],
        file: {
          file_id: 'file_1',
          name: 'notes.txt',
          mime_type: 'text/plain',
          size: 6,
          sha256: 'abc',
          chunk_index: index,
          chunk_total: total,
          data_base64: data,
        },
      },
    });
    store.applyEvent({
      type: 'history_snapshot',
      rooms: [{
        room_id: 'room_1',
        room_name: '项目群',
        agent_mode: 'mention',
        peer_ids: ['ace_local', 'ace_remote'],
        messages: [chunk(1, 2, 'Zm9v'), chunk(0, 2, 'aGVsbG8=')],
      }],
      dms: [],
    });

    const messages = store.conversationMessages(roomConversationId('room_1'));
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      kind: 'file',
      isOwn: false,
      file: { file_id: 'file_1', name: 'notes.txt', complete: true, data_base64: 'aGVsbG8=Zm9v' },
    });
  });

  it('reassembles chunked direct files in the human DM conversation', () => {
    const store = readyStore();
    store.applyEvent({ type: 'peer_connected', peer: remotePeer });
    for (const [index, data] of ['aGVs', 'bG8='].entries()) {
      store.applyEvent({
        type: 'message',
        peer_id: 'ace_remote',
        message: {
          type: 'peer.file',
          message_id: `dm_file_${index}`,
          sender: 'ace_remote',
          payload: {
            file: {
              file_id: 'file_dm', name: 'hello.txt', mime_type: 'text/plain', size: 5,
              sha256: 'abc', chunk_index: index, chunk_total: 2, data_base64: data,
            },
          },
        },
      });
    }
    expect(store.conversationMessages(dmConversationId('ace_remote')).at(-1)).toMatchObject({
      kind: 'file',
      file: { file_id: 'file_dm', complete: true, data_base64: 'aGVsbG8=' },
    });
  });
});

describe('NearbyStore unread counting', () => {
  it('counts unread for non-active conversations and clears on activation', () => {
    const store = readyStore();
    store.applyEvent({ type: 'peer_connected', peer: remotePeer });
    const dmId = dmConversationId('ace_remote');
    // peer_connected 在无当前会话时自动点亮直聊
    expect(store.activeConversationId).toBe(dmId);

    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '群', peer_ids: ['ace_local', 'ace_remote'], agent_mode: 'mention' });
    expect(store.activeConversationId).toBe(roomConversationId('room_1'));

    store.applyEvent({ type: 'peer_message_received', peer_id: 'ace_remote', display_name: 'Windows 工作站', text: 'hi', mentions: [], message_id: 'p1', timestamp: 100 });
    store.applyEvent({ type: 'peer_message_received', peer_id: 'ace_remote', display_name: 'Windows 工作站', text: '在忙吗', mentions: [], message_id: 'p2', timestamp: 101 });
    expect(store.conversations.get(dmId)?.unread).toBe(2);

    store.setActiveConversation(dmId);
    expect(store.conversations.get(dmId)?.unread).toBe(0);
  });

  it('does not count unread for own message echoes', () => {
    const store = readyStore();
    store.applyEvent({ type: 'peer_connected', peer: remotePeer });
    store.applyEvent({
      type: 'message',
      peer_id: 'ace_remote',
      message: { type: 'peer.message', message_id: 'own_1', sender: 'ace_local', payload: { text: '我发的', mentions: [] } },
    });
    expect(store.conversations.get(dmConversationId('ace_remote'))?.unread).toBe(0);
  });
});

describe('NearbyStore conversation availability', () => {
  it('requires a connected peer for direct and room conversations', () => {
    const store = readyStore();
    store.applyEvent({ type: 'peer_connected', peer: remotePeer });
    store.applyEvent({
      type: 'room_created',
      room_id: 'room_1',
      room_name: '项目群',
      peer_ids: ['ace_local', 'ace_remote'],
      agent_mode: 'mention',
    });
    const dm = store.conversations.get(dmConversationId('ace_remote'))!;
    const room = store.conversations.get(roomConversationId('room_1'))!;
    expect(store.isConversationOnline(dm)).toBe(true);
    expect(store.isConversationOnline(room)).toBe(true);

    store.applyEvent({ type: 'peer_disconnected', peer_id: 'ace_remote' });
    expect(store.isConversationOnline(dm)).toBe(false);
    expect(store.isConversationOnline(room)).toBe(false);
  });
});

describe('NearbyStore room agent_mode maintenance', () => {
  it('tracks agent_mode through create/join/settings events and ownership', () => {
    const store = readyStore();
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '项目群', peer_ids: ['ace_local'], agent_mode: 'mention', owner_peer_id: 'ace_local' });
    expect(store.conversations.get(roomConversationId('room_1'))).toMatchObject({ agentMode: 'mention', isOwner: true, ownerPeerId: 'ace_local' });

    store.applyEvent({ type: 'room_settings_updated', room_id: 'room_1', agent_mode: 'quiet' });
    const room = store.conversations.get(roomConversationId('room_1'));
    expect(room?.agentMode).toBe('quiet');
    const systemMessage = store.conversationMessages(roomConversationId('room_1')).at(-1);
    expect(systemMessage?.kind).toBe('system');
    expect(systemMessage?.text).toContain('安静模式');

    store.applyEvent({ type: 'room_joined', room_id: 'room_2', room_name: '别的群', peer_ids: ['ace_local', 'ace_other'], agent_mode: 'auto', owner_peer_id: 'ace_other' });
    expect(store.conversations.get(roomConversationId('room_2'))).toMatchObject({ agentMode: 'auto', isOwner: false, ownerPeerId: 'ace_other' });
  });

  it('removes the conversation on room_left', () => {
    const store = readyStore();
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '项目群', peer_ids: ['ace_local'], agent_mode: 'mention', owner_peer_id: 'ace_local' });
    expect(store.activeConversationId).toBe(roomConversationId('room_1'));
    store.applyEvent({ type: 'room_left', room_id: 'room_1' });
    expect(store.conversations.has(roomConversationId('room_1'))).toBe(false);
    expect(store.activeConversationId).toBeNull();
  });
});

describe('NearbyStore room ownership via owner_peer_id', () => {
  it('marks the local user as owner only when owner_peer_id matches the local peer', () => {
    const store = readyStore();
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '我建的群', peer_ids: ['ace_local'], agent_mode: 'mention', owner_peer_id: 'ace_local' });
    expect(store.conversations.get(roomConversationId('room_1'))?.isOwner).toBe(true);

    store.applyEvent({ type: 'room_joined', room_id: 'room_2', room_name: '别人的群', peer_ids: ['ace_other', 'ace_local'], agent_mode: 'mention', owner_peer_id: 'ace_other' });
    expect(store.conversations.get(roomConversationId('room_2'))?.isOwner).toBe(false);
  });

  it('restores ownership from history_snapshot owner_peer_id', () => {
    const store = readyStore();
    store.applyEvent({
      type: 'history_snapshot',
      rooms: [{
        room_id: 'room_1',
        room_name: '项目群',
        agent_mode: 'mention',
        peer_ids: ['ace_local', 'ace_remote'],
        owner_peer_id: 'ace_local',
        messages: [],
      }],
      dms: [],
    });
    expect(store.conversations.get(roomConversationId('room_1'))).toMatchObject({ isOwner: true, ownerPeerId: 'ace_local' });
  });
});

describe('NearbyStore room member join/leave', () => {
  function createOwnedRoom(store: NearbyStore): void {
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '项目群', peer_ids: ['ace_local'], agent_mode: 'mention', owner_peer_id: 'ace_local' });
  }

  it('adds the member and posts a system message on room_member_joined', () => {
    const store = readyStore();
    createOwnedRoom(store);
    store.applyEvent({ type: 'room_member_joined', room_id: 'room_1', peer_id: 'ace_remote', display_name: 'Windows 工作站' });
    const conversation = store.conversations.get(roomConversationId('room_1'))!;
    expect(conversation.memberIds).toContain('ace_remote');
    expect(store.conversationMessages(conversation.id).at(-1)).toMatchObject({ kind: 'system', text: 'Windows 工作站 加入了群聊' });
  });

  it('falls back to peer_id when display_name is missing', () => {
    const store = readyStore();
    createOwnedRoom(store);
    store.applyEvent({ type: 'room_member_joined', room_id: 'room_1', peer_id: 'ace_remote' });
    expect(store.conversationMessages(roomConversationId('room_1')).at(-1)?.text).toBe('ace_remote 加入了群聊');
  });

  it('removes the member, clears the pending reply and posts a system message on room_member_left', () => {
    const store = readyStore();
    createOwnedRoom(store);
    const roomId = roomConversationId('room_1');
    store.applyEvent({ type: 'room_member_joined', room_id: 'room_1', peer_id: 'ace_remote', display_name: 'Windows 工作站' });
    store.expectAgentReply(roomId, ['ace_remote']);
    store.applyEvent({ type: 'room_member_left', room_id: 'room_1', peer_id: 'ace_remote', display_name: 'Windows 工作站' });
    const conversation = store.conversations.get(roomId)!;
    expect(conversation.memberIds).not.toContain('ace_remote');
    expect(store.pendingAgentSenders(roomId)).toEqual([]);
    expect(store.conversationMessages(roomId).at(-1)).toMatchObject({ kind: 'system', text: 'Windows 工作站 退出了群聊' });
  });
});

describe('NearbyStore room rename', () => {
  it('renames the conversation and posts a system message when room_settings_updated carries room_name', () => {
    const store = readyStore();
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '项目群', peer_ids: ['ace_local'], agent_mode: 'mention', owner_peer_id: 'ace_local' });
    const note = store.applyEvent({ type: 'room_settings_updated', room_id: 'room_1', room_name: '新群名' });
    const roomId = roomConversationId('room_1');
    expect(store.conversations.get(roomId)?.title).toBe('新群名');
    expect(store.conversationMessages(roomId).at(-1)).toMatchObject({ kind: 'system', text: '群主修改了群名为「新群名」' });
    expect(note?.text).toContain('新群名');
  });

  it('applies name and agent_mode together when both change', () => {
    const store = readyStore();
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '项目群', peer_ids: ['ace_local'], agent_mode: 'mention', owner_peer_id: 'ace_local' });
    store.applyEvent({ type: 'room_settings_updated', room_id: 'room_1', room_name: '新群名', agent_mode: 'quiet' });
    const roomId = roomConversationId('room_1');
    const conversation = store.conversations.get(roomId)!;
    expect(conversation).toMatchObject({ title: '新群名', agentMode: 'quiet' });
    const systemTexts = store.conversationMessages(roomId).filter((message) => message.kind === 'system').map((message) => message.text);
    expect(systemTexts).toContain('群主修改了群名为「新群名」');
    expect(systemTexts.some((text) => text.includes('安静模式'))).toBe(true);
  });
});

describe('NearbyStore agent reply expectations', () => {
  it('renders the awaited room reply as an agent message and clears the placeholder', () => {
    const store = readyStore();
    store.applyEvent({ type: 'peer_connected', peer: remotePeer });
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '项目群', peer_ids: ['ace_local', 'ace_remote'], agent_mode: 'mention' });
    const roomId = roomConversationId('room_1');

    store.expectAgentReply(roomId, ['ace_remote']);
    expect(store.pendingAgentSenders(roomId)).toEqual(['ace_remote']);

    store.applyEvent(roomMessage('ace_remote', '这是 Agent 的总结', 'rm_1', ['ace_local']));
    expect(store.pendingAgentSenders(roomId)).toEqual([]);
    const messages = store.conversationMessages(roomId);
    expect(messages.at(-1)).toMatchObject({ kind: 'agent', senderPeerId: 'ace_remote', isOwn: false });
  });

  it('keeps unexpected room messages as plain human messages', () => {
    const store = readyStore();
    store.applyEvent({ type: 'peer_connected', peer: remotePeer });
    store.applyEvent({ type: 'room_created', room_id: 'room_1', room_name: '项目群', peer_ids: ['ace_local', 'ace_remote'], agent_mode: 'mention' });
    store.applyEvent(roomMessage('ace_remote', '人发的消息', 'rm_1'));
    expect(store.conversationMessages(roomConversationId('room_1')).at(-1)?.kind).toBe('text');
  });

  it('clears the pending direct reply when the peer disconnects', () => {
    const store = readyStore();
    store.applyEvent({ type: 'peer_connected', peer: remotePeer });
    const dmId = dmConversationId('ace_remote');
    store.expectAgentReply(dmId, ['ace_remote']);
    store.applyEvent({ type: 'peer_disconnected', peer_id: 'ace_remote' });
    expect(store.pendingAgentSenders(dmId)).toEqual([]);
    expect(store.peers.get('ace_remote')?.connection).toBe('disconnected');
  });
});
