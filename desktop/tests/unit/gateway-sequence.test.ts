import { describe, it, expect, beforeEach } from 'vitest';
import {
  noteGatewaySequence,
  isDuplicateGatewayChunk,
  getLastGatewaySequences,
  resetGatewaySequence,
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
});
