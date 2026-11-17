import { describe, it, expect, beforeEach } from 'vitest';
import {
  noteGatewaySequence,
  acceptGatewayResync,
  getGatewayCursors,
  isDuplicateGatewayChunk,
  getLastGatewaySequences,
  resetGatewaySequence,
  resetLegacyGatewaySequence,
  peekGatewaySequence,
  touchStreamActivity,
  getLastStreamActivity,
} from '../../src/ui/features/gateway-sequence';
import type { ChatChunk } from '../../src/ui/backend-client';

function chunk(partial: Partial<ChatChunk> & Pick<ChatChunk, 'kind'>): ChatChunk {
  return {
    kind: partial.kind,
    body: partial.body ?? {},
    is_final: partial.is_final ?? false,
    sequence: partial.sequence ?? 0,
    session_id: partial.session_id ?? 's1',
    gateway_sequence: partial.gateway_sequence,
    gateway_epoch: partial.gateway_epoch,
    request_id: partial.request_id,
  };
}

describe('gateway-sequence', () => {
  beforeEach(() => {
    resetGatewaySequence('s1');
  });

  it('tracks monotonic gateway_sequence per session', () => {
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_sequence: 3 }));
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_sequence: 2 }));
    expect(peekGatewaySequence('s1')).toBe(3);
  });

  it('drops duplicate frames but accepts out-of-order lower sequence frames', () => {
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_sequence: 5 }));
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_sequence: 5 }))).toBe(true);
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_sequence: 3 }))).toBe(false);
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_sequence: 3 }));
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_sequence: 3 }))).toBe(true);
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_sequence: 6 }))).toBe(false);
  });

  it('allows frames without gateway_sequence (legacy)', () => {
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta' }))).toBe(false);
  });

  it('builds subscribe payload map', () => {
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_sequence: 7 }));
    noteGatewaySequence('s2', chunk({ kind: 'delta', gateway_sequence: 2, session_id: 's2' }));
    expect(getLastGatewaySequences(['s1', 's2', 's3'])).toEqual({ s1: 7, s2: 2 });
  });

  it('resets sequence on new turn', () => {
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_sequence: 9 }));
    resetGatewaySequence('s1');
    expect(peekGatewaySequence('s1')).toBe(0);
    expect(getLastGatewaySequences(['s1'])).toEqual({});
  });

  it('tracks stream activity timestamps', () => {
    touchStreamActivity('s1', 1000);
    expect(getLastStreamActivity('s1')).toBe(1000);
  });

  it('preserves the modern cursor across turns and ignores an old epoch after a resync', () => {
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_epoch: 'old', gateway_sequence: 7 }));
    resetLegacyGatewaySequence('s1');
    expect(getGatewayCursors(['s1'])).toEqual({ s1: { epoch: 'old', sequence: 7 } });
    const resync = chunk({ kind: 'gateway_resync', gateway_epoch: 'new', gateway_sequence: 0 });
    expect(acceptGatewayResync('s1', resync)).toBe(true);
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_epoch: 'old', gateway_sequence: 8 }))).toBe(true);
    const newFirst = chunk({ kind: 'delta', gateway_epoch: 'new', gateway_sequence: 1 });
    expect(isDuplicateGatewayChunk('s1', newFirst)).toBe(false);
    noteGatewaySequence('s1', newFirst);
    expect(acceptGatewayResync('s1', resync)).toBe(false);
    expect(getGatewayCursors(['s1'])).toEqual({ s1: { epoch: 'new', sequence: 1 } });
  });

  it('treats frames through the accepted cut as covered while retaining exact deduplication afterward', () => {
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_epoch: 'epoch', gateway_sequence: 2 }));
    const resync = chunk({ kind: 'gateway_resync', gateway_epoch: 'epoch', gateway_sequence: 10 });
    expect(acceptGatewayResync('s1', resync)).toBe(true);
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_epoch: 'epoch', gateway_sequence: 3 }))).toBe(true);
    noteGatewaySequence('s1', chunk({ kind: 'delta', gateway_epoch: 'epoch', gateway_sequence: 12 }));
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_epoch: 'epoch', gateway_sequence: 11 }))).toBe(false);
    expect(isDuplicateGatewayChunk('s1', chunk({ kind: 'delta', gateway_epoch: 'epoch', gateway_sequence: 12 }))).toBe(true);
  });
});
