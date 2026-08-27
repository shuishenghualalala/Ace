import { describe, expect, it, vi } from 'vitest';

import { NearbyService } from '../../src/main/nearby-service';

type LineHandler = (line: string, onReady: () => void) => void;

describe('NearbyService Agent routing', () => {
  it('runs the Agent only for a remote agent.request', async () => {
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

    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(runAgentTurn.mock.calls[0]?.[0]).toEqual({
      peerId: 'ace_remote',
      peerName: 'Windows',
      requestId: 'request-1',
      text: 'hello',
    });

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
    expect(runAgentTurn).toHaveBeenCalledTimes(1);
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

  it('aborts an in-flight Agent turn when Nearby stops', async () => {
    let requestSignal: AbortSignal | undefined;
    const runAgentTurn = vi.fn((_request, signal: AbortSignal) => {
      requestSignal = signal;
      return new Promise<string>(() => undefined);
    });
    const service = new NearbyService({
      repoRoot: '/tmp/ace',
      resourcesPath: '/tmp/ace/resources',
      isPackaged: false,
      crewHome: '/tmp/ace/home',
      onEvent: vi.fn(),
      runAgentTurn,
    });
    const handleLine = (service as unknown as { handleLine: LineHandler }).handleLine.bind(service);

    handleLine(JSON.stringify({
      type: 'message',
      peer_id: 'ace_remote',
      message: {
        type: 'agent.request',
        message_id: 'request-pending',
        sender: 'ace_remote',
        payload: { text: 'wait' },
      },
    }), () => undefined);

    await vi.waitFor(() => expect(runAgentTurn).toHaveBeenCalledTimes(1));
    expect(requestSignal?.aborted).toBe(false);
    await service.stop();
    expect(requestSignal?.aborted).toBe(true);
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
});
