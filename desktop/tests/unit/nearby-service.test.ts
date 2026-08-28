import { describe, expect, it, vi } from 'vitest';

import { NearbyService } from '../../src/main/nearby-service';

type LineHandler = (line: string, onReady: () => void) => void;

describe('NearbyService Agent routing', () => {
  it('rejects a remote direct Agent request instead of running an Agent in DM', async () => {
    const runAgentTurn = vi.fn().mockResolvedValue('reply');
    const onEvent = vi.fn();
    const service = new NearbyService({
      repoRoot: '/tmp/ace',
      resourcesPath: '/tmp/ace/resources',
      isPackaged: false,
      crewHome: '/tmp/ace/home',
      onEvent,
      runAgentTurn,
    });
    const handleLine = (service as unknown as { handleLine: LineHandler }).handleLine.bind(service);
    const emit = (event: Record<string, unknown>) => handleLine(JSON.stringify(event), () => undefined);

    emit({
      type: 'ready',
      peer: { peer_id: 'ace_local', display_name: 'Mac' },
    });
    emit({
      type: 'peer_discovered',
      peer: { peer_id: 'ace_remote', display_name: 'Windows' },
    });
    emit({
      type: 'message',
      peer_id: 'ace_remote',
      message: {
        type: 'agent.request',
        message_id: 'request-1',
        sender: 'ace_remote',
        payload: { text: 'hello' },
      },
    });

    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();

    emit({
      type: 'message',
      peer_id: 'ace_remote',
      message: {
        type: 'agent.response',
        message_id: 'response-1',
        sender: 'ace_remote',
        payload: { request_id: 'request-1', text: 'reply' },
      },
    });
    emit({
      type: 'message',
      peer_id: 'ace_remote',
      message: {
        type: 'agent.request',
        message_id: 'request-2',
        sender: 'ace_local',
        payload: { text: 'own echo' },
      },
    });
    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();
    expect(onEvent).toHaveBeenCalledTimes(5);
  });

  it('skips the Agent turn when auto reply is disabled', async () => {
    const runAgentTurn = vi.fn().mockResolvedValue('reply');
    const service = new NearbyService({
      repoRoot: '/tmp/ace',
      resourcesPath: '/tmp/ace/resources',
      isPackaged: false,
      crewHome: '/tmp/ace/home',
      onEvent: vi.fn(),
      runAgentTurn,
      autoReplyEnabled: () => false,
    });
    const handleLine = (service as unknown as { handleLine: LineHandler }).handleLine.bind(service);

    handleLine(JSON.stringify({
      type: 'message',
      peer_id: 'ace_remote',
      message: {
        type: 'agent.request',
        message_id: 'request-muted',
        sender: 'ace_remote',
        payload: { text: 'hello' },
      },
    }), () => undefined);

    await Promise.resolve();
    expect(runAgentTurn).not.toHaveBeenCalled();
  });

  it('passes published Agent profiles to the pluggable runtime', () => {
    const runAgentTurn = vi.fn();
    const service = new NearbyService({
      repoRoot: '/tmp/ace',
      resourcesPath: '/tmp/ace/resources',
      isPackaged: false,
      crewHome: '/tmp/ace/home',
      onEvent: vi.fn(),
      runAgentTurn,
    });
    service.setPublishedAgents([{ public_agent_id: 'agent-1', display_name: 'Crew' }]);
    const command = (service as unknown as {
      resolveCommand: () => { args: string[] };
    }).resolveCommand();
    const flagIndex = command.args.indexOf('--published-agent');
    expect(flagIndex).toBeGreaterThan(-1);
    expect(JSON.parse(command.args[flagIndex + 1] ?? '{}')).toMatchObject({
      public_agent_id: 'agent-1',
      display_name: 'Crew',
      revision: 1,
    });
  });

  it('forwards room membership and settings events to onEvent unchanged', () => {
    const onEvent = vi.fn();
    const service = new NearbyService({
      repoRoot: '/tmp/ace',
      resourcesPath: '/tmp/ace/resources',
      isPackaged: false,
      crewHome: '/tmp/ace/home',
      onEvent,
    });
    const handleLine = (service as unknown as { handleLine: LineHandler }).handleLine.bind(service);
    const emit = (event: Record<string, unknown>) => handleLine(JSON.stringify(event), () => undefined);

    emit({
      type: 'room_member_joined',
      room_id: 'room_1',
      peer_id: 'ace_remote',
      display_name: 'Windows',
    });
    emit({ type: 'room_member_left', room_id: 'room_1', peer_id: 'ace_remote' });
    emit({
      type: 'room_settings_updated',
      room_id: 'room_1',
      agent_mode: 'auto',
      room_name: '新群名',
    });

    expect(onEvent).toHaveBeenNthCalledWith(1, {
      type: 'room_member_joined',
      room_id: 'room_1',
      peer_id: 'ace_remote',
      display_name: 'Windows',
    });
    expect(onEvent).toHaveBeenNthCalledWith(2, {
      type: 'room_member_left',
      room_id: 'room_1',
      peer_id: 'ace_remote',
    });
    expect(onEvent).toHaveBeenNthCalledWith(3, {
      type: 'room_settings_updated',
      room_id: 'room_1',
      agent_mode: 'auto',
      room_name: '新群名',
    });
  });

  it('keeps legacy peers visible but rejects WebRTC files until they advertise support', async () => {
    const service = new NearbyService({
      repoRoot: '/tmp/ace',
      resourcesPath: '/tmp/ace/resources',
      isPackaged: false,
      crewHome: '/tmp/ace/home',
      onEvent: vi.fn(),
    });
    const handleLine = (service as unknown as { handleLine: LineHandler }).handleLine.bind(service);
    const write = vi.fn();
    const internals = service as unknown as {
      start: () => Promise<void>;
      child: { stdin: { writable: boolean; write: (value: string) => void } };
    };
    internals.start = vi.fn().mockResolvedValue(undefined);
    internals.child = { stdin: { writable: true, write } };
    const command = {
      type: 'send_peer_file' as const,
      peer_id: 'ace_legacy',
      file_id: 'file-1',
      name: 'note.txt',
      mime_type: 'text/plain',
      size: 5,
      sha256: 'a'.repeat(64),
      file_path: '/tmp/note.txt',
    };

    handleLine(JSON.stringify({
      type: 'peer_discovered',
      peer: { peer_id: 'ace_legacy', display_name: 'Legacy', capabilities: ['chat'] },
    }), () => undefined);
    await expect(service.send(command)).rejects.toThrow('不支持快速文件传输');
    expect(write).not.toHaveBeenCalled();

    handleLine(JSON.stringify({
      type: 'peer_connected',
      peer: {
        peer_id: 'ace_legacy',
        display_name: 'Updated',
        capabilities: ['chat', 'file.webrtc'],
      },
    }), () => undefined);
    await expect(service.send(command)).resolves.toBeUndefined();
    expect(write).toHaveBeenCalledWith(`${JSON.stringify(command)}\n`);
  });
});
