import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

import { describe, expect, it, vi } from 'vitest';

import {
  NearbyAgentBridge,
  loadNearbyAgentSettings,
  saveNearbyAgentSettings,
  type NearbyAgentSettings,
} from '../../src/main/nearby-agent-bridge';
import type { NearbyCommand, NearbyEvent } from '../../src/main/nearby-service';

function createBridge(settings: Partial<NearbyAgentSettings> = {}) {
  const sent: NearbyCommand[] = [];
  const runAgentTurn = vi.fn().mockResolvedValue('agent reply');
  const bridge = new NearbyAgentBridge({
    sendCommand: (command) => {
      sent.push(command);
    },
    runAgentTurn,
    getSettings: () => ({
      autoReply: settings.autoReply ?? true,
      allowedToolsets: settings.allowedToolsets ?? [],
    }),
  });
  bridge.handleEvent({ type: 'ready', peer: { peer_id: 'ace_local', display_name: 'Mac' } });
  bridge.handleEvent({ type: 'peer_connected', peer: { peer_id: 'ace_remote', display_name: 'Windows' } });
  return { bridge, sent, runAgentTurn };
}

function roomMessage(sender: string, text: string, mentions: string[] = [], messageId = 'msg_1'): NearbyEvent {
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

function createRoom(agentMode: string): NearbyEvent {
  return {
    type: 'room_created',
    room_id: 'room_1',
    room_name: '项目群',
    peer_ids: ['ace_local', 'ace_remote'],
    agent_mode: agentMode,
  };
}

describe('NearbyAgentBridge room adjudication', () => {
  it('mention mode responds only when the local peer is mentioned', async () => {
    const { bridge, sent, runAgentTurn } = createBridge({ allowedToolsets: ['wiki'] });
    bridge.handleEvent(createRoom('mention'));

    bridge.handleEvent(roomMessage('ace_remote', 'hello', [], 'msg_1'));
    expect(runAgentTurn).not.toHaveBeenCalled();

    bridge.handleEvent(roomMessage('ace_remote', '@Mac 总结一下', ['ace_local'], 'msg_2'));
    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(runAgentTurn.mock.calls[0]?.[0]).toMatchObject({
      peerId: 'ace_remote',
      peerName: 'Windows',
      requestId: 'msg_2',
      text: '@Mac 总结一下',
      roomId: 'room_1',
      roomName: '项目群',
      allowedToolsets: ['wiki'],
    });
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      type: 'send_room_message',
      room_id: 'room_1',
      text: 'agent reply',
      mentions: ['ace_remote'],
    });
  });

  it('auto mode responds to any remote message', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent(createRoom('auto'));

    bridge.handleEvent(roomMessage('ace_remote', 'plain message', [], 'msg_1'));
    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
  });

  it('keeps Agent replies in history without triggering another Agent turn', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent(createRoom('auto'));
    const agentReply = roomMessage('ace_remote', 'agent reply', ['ace_local'], 'msg_agent');
    const replyMessage = agentReply.message as { payload: Record<string, unknown> };
    replyMessage.payload.agent_sender = { public_agent_id: 'agent_mori', display_name: 'Mori' };

    bridge.handleEvent(agentReply);
    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();

    bridge.handleEvent(roomMessage('ace_remote', 'human follow-up', [], 'msg_human'));
    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(runAgentTurn.mock.calls[0]?.[0].history).toEqual([{ sender: 'Mori', text: 'agent reply' }]);
  });

  it('routes a structured room mention to the selected published Agent', async () => {
    const sent: NearbyCommand[] = [];
    const runAgentTurn = vi.fn().mockResolvedValue('Mori reply');
    const bridge = new NearbyAgentBridge({
      sendCommand: (command) => sent.push(command),
      runAgentTurn,
      getSettings: () => ({ autoReply: true, allowedToolsets: [] }),
    });
    bridge.handleEvent({
      type: 'ready',
      peer: {
        peer_id: 'ace_local',
        display_name: 'Mac',
        published_agents: [{ public_agent_id: 'agent_mori', display_name: 'Mori' }],
      },
    });
    bridge.handleEvent(createRoom('mention'));
    const event = roomMessage('ace_remote', '@Mori 总结一下', ['ace_local'], 'msg_target');
    const message = event.message as { payload: Record<string, unknown> };
    message.payload.agent_mentions = [{ peer_id: 'ace_local', public_agent_id: 'agent_mori' }];
    bridge.handleEvent(event);

    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(runAgentTurn.mock.calls[0]?.[0]).toMatchObject({
      publicAgentId: 'agent_mori',
      agentDisplayName: 'Mori',
    });
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      type: 'send_room_message',
      agent_sender: { public_agent_id: 'agent_mori', display_name: 'Mori' },
    });
  });

  it('quiet mode never responds, even when mentioned', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent(createRoom('quiet'));

    bridge.handleEvent(roomMessage('ace_remote', '@Mac 在吗', ['ace_local'], 'msg_1'));
    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();
  });

  it('room_settings_updated switches the mode applied to later messages', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent(createRoom('auto'));
    bridge.handleEvent({ type: 'room_settings_updated', room_id: 'room_1', agent_mode: 'quiet' });

    bridge.handleEvent(roomMessage('ace_remote', 'plain message', [], 'msg_1'));
    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();
  });

  it('room_settings_updated renames the room used in later Agent turns', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent(createRoom('auto'));
    bridge.handleEvent({
      type: 'room_settings_updated',
      room_id: 'room_1',
      agent_mode: 'auto',
      room_name: '新群名',
    });

    bridge.handleEvent(roomMessage('ace_remote', 'plain message', [], 'msg_1'));
    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(runAgentTurn.mock.calls[0]?.[0].roomName).toBe('新群名');
  });

  it('ignores room member join/leave events for adjudication', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent({ ...createRoom('auto'), owner_peer_id: 'ace_remote' });
    bridge.handleEvent({
      type: 'room_member_joined',
      room_id: 'room_1',
      peer_id: 'ace_new',
      display_name: 'New',
    });
    bridge.handleEvent({
      type: 'room_member_left',
      room_id: 'room_1',
      peer_id: 'ace_new',
      display_name: 'New',
    });
    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();
  });

  it('does not respond when auto reply is disabled', async () => {
    const { bridge, runAgentTurn } = createBridge({ autoReply: false });
    bridge.handleEvent(createRoom('auto'));

    bridge.handleEvent(roomMessage('ace_remote', 'plain message', [], 'msg_1'));
    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();
  });

  it('ignores messages sent by the local peer', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent(createRoom('auto'));

    bridge.handleEvent(roomMessage('ace_local', 'own echo', [], 'msg_1'));
    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();
  });

  it('deduplicates a message id while its turn is running', async () => {
    const sent: NearbyCommand[] = [];
    const runAgentTurn = vi.fn(() => new Promise<string>(() => undefined));
    const bridge = new NearbyAgentBridge({
      sendCommand: (command) => {
        sent.push(command);
      },
      runAgentTurn,
      getSettings: () => ({ autoReply: true, allowedToolsets: [] }),
    });
    bridge.handleEvent({ type: 'ready', peer: { peer_id: 'ace_local', display_name: 'Mac' } });
    bridge.handleEvent(createRoom('auto'));

    const event = roomMessage('ace_remote', 'hello', [], 'msg_dup');
    bridge.handleEvent(event);
    bridge.handleEvent(event);
    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(runAgentTurn).toHaveBeenCalledTimes(1);
    bridge.dispose();
  });

  it('passes recent room history from the snapshot to the Agent turn', async () => {
    const { bridge, runAgentTurn } = createBridge();
    bridge.handleEvent({
      type: 'history_snapshot',
      rooms: [{
        room_id: 'room_1',
        room_name: '项目群',
        agent_mode: 'auto',
        peer_ids: ['ace_local', 'ace_remote'],
        messages: [{
          type: 'room.message',
          message_id: 'msg_0',
          sender: 'ace_remote',
          payload: { room_id: 'room_1', text: 'earlier', mentions: [] },
        }],
      }],
      dms: [],
    });

    bridge.handleEvent(roomMessage('ace_remote', 'now', [], 'msg_1'));
    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(runAgentTurn.mock.calls[0]?.[0].history).toEqual([{ sender: 'Windows', text: 'earlier' }]);
  });
});

describe('nearby agent settings file', () => {
  function tempCrewHome(): string {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'ace-nearby-settings-'));
  }

  it('defaults to auto reply on with an empty toolset allowlist', () => {
    const crewHome = tempCrewHome();
    expect(loadNearbyAgentSettings(crewHome)).toEqual({ autoReply: true, allowedToolsets: [] });
    fs.rmSync(crewHome, { recursive: true, force: true });
  });

  it('merges patches without dropping fields owned by other writers', () => {
    const crewHome = tempCrewHome();
    const settingsDir = path.join(crewHome, 'nearby');
    fs.mkdirSync(settingsDir, { recursive: true });
    fs.writeFileSync(path.join(settingsDir, 'settings.json'), '{"discoverable":false}\n', 'utf8');

    const saved = saveNearbyAgentSettings(crewHome, { autoReply: false, allowedToolsets: ['wiki', 'wiki'] });
    expect(saved).toEqual({ autoReply: false, allowedToolsets: ['wiki'] });

    const onDisk = JSON.parse(
      fs.readFileSync(path.join(settingsDir, 'settings.json'), 'utf8'),
    ) as Record<string, unknown>;
    expect(onDisk.discoverable).toBe(false);
    expect(onDisk.auto_reply).toBe(false);
    expect(onDisk.allowed_toolsets).toEqual(['wiki']);
    expect(loadNearbyAgentSettings(crewHome)).toEqual({ autoReply: false, allowedToolsets: ['wiki'] });
    fs.rmSync(crewHome, { recursive: true, force: true });
  });
});
