/**
 * stream-reassembly 单测：验证按 gateway_sequence 重组 delta（根治流式串位/丢字）。
 *
 * 在可验证的边界内按原序重组，无法验证时保留正文并请求权威快照。
 */

import { describe, it, expect, beforeEach } from 'vitest';
import {
  parseDeltaRange,
  noteDeltaBoundary,
  noteDelta,
  resetAssistant,
  resetSession,
  resetSessionExcept,
  peekFrags,
} from '../../src/ui/stream-reassembly';

describe('stream-reassembly / noteDelta', () => {
  beforeEach(() => {
    resetSession('s1');
    resetSession('s2');
  });

  it('returns the text on the first fragment', () => {
    expect(noteDelta('s1', 'm-1', 10, 'hello')).toBe('hello');
  });

  it('accumulates in-order deltas correctly', () => {
    noteDelta('s1', 'm-1', 1, 'a');
    noteDelta('s1', 'm-1', 2, 'b');
    expect(noteDelta('s1', 'm-1', 3, 'c')).toBe('abc');
  });

  it('reassembles correctly when deltas arrive OUT OF ORDER (reconnect replay)', () => {
    // 重连 replay：旧低序号帧晚到。盲目 cur+text 会拼成 "bca"（串位），重组恒为 "abc"。
    noteDelta('s1', 'm-1', 2, 'b');
    noteDelta('s1', 'm-1', 3, 'c');
    expect(noteDelta('s1', 'm-1', 1, 'a')).toBe('abc');
  });

  it('reassembles correctly with gaps between delta seqs (其它 kind 占用 seq)', () => {
    noteDelta('s1', 'm-1', 5, 'a');
    noteDelta('s1', 'm-1', 12, 'b');
    noteDelta('s1', 'm-1', 30, 'c');
    expect(peekFrags('s1', 'm-1').get(5)).toBe('a');
    // 重组结果仍是升序拼接
    expect(noteDelta('s1', 'm-1', 31, 'd')).toBe('abcd');
  });

  it('is idempotent on duplicate seq (去重层二次防御)', () => {
    noteDelta('s1', 'm-1', 1, 'a');
    noteDelta('s1', 'm-1', 1, 'a'); // 重复 seq
    expect(noteDelta('s1', 'm-1', 2, 'b')).toBe('ab');
  });

  it('isolates sessions and assistant turns', () => {
    noteDelta('s1', 'm-1', 1, 'a');
    noteDelta('s2', 'm-1', 1, 'x'); // 同 aid 不同 session
    noteDelta('s1', 'm-2', 1, 'q'); // 同 session 不同 aid（不同回合）
    expect(peekFrags('s1', 'm-1').get(1)).toBe('a');
    expect(peekFrags('s2', 'm-1').get(1)).toBe('x');
    expect(peekFrags('s1', 'm-2').get(1)).toBe('q');
  });

  it('keeps only a bounded suffix of a long exact stream and rejects old unseen text', () => {
    const fragments = Array.from({ length: 5000 }, (_, index) => `${index}汉🙂`);
    let content: string | null = null;
    fragments.forEach((fragment, index) => {
      content = noteDelta('s1', 'm-1', index * 2 + 2, fragment);
    });
    expect(content).toBe(fragments.join(''));
    expect(peekFrags('s1', 'm-1').size).toBeLessThanOrEqual(512);
    expect(noteDelta('s1', 'm-1', 5, 'old')).toBeNull();
    expect(noteDelta('s1', 'm-1', 10001, '!')).toBe(fragments.join('') + '!');
  });

  it('rejects a late delta across a tool boundary and a retroactive tool completion', () => {
    expect(noteDelta('s1', 'process', 8, 'before')).toBe('before');
    expect(noteDeltaBoundary('s1', 11)).toBe(true);
    expect(noteDelta('s1', 'answer', 14, 'after')).toBe('after');
    expect(noteDelta('s1', 'answer', 10, 'misplaced')).toBeNull();
    expect(noteDeltaBoundary('s1', 13)).toBe(false);
    expect(noteDelta('s1', 'answer', 15, '!')).toBe('after!');
  });
});

describe('stream-reassembly / lifecycle resets', () => {
  beforeEach(() => {
    resetSession('s1');
  });

  it('resetAssistant clears only that turn', () => {
    noteDelta('s1', 'm-1', 1, 'a');
    noteDelta('s1', 'm-2', 1, 'b');
    resetAssistant('s1', 'm-1');
    expect(peekFrags('s1', 'm-1').size).toBe(0);
    expect(peekFrags('s1', 'm-2').get(1)).toBe('b');
  });

  it('resetSession clears all turns for the session', () => {
    noteDelta('s1', 'm-1', 1, 'a');
    noteDelta('s1', 'm-2', 1, 'b');
    resetSession('s1');
    expect(peekFrags('s1', 'm-1').size).toBe(0);
    expect(peekFrags('s1', 'm-2').size).toBe(0);
  });

  it('resetSessionExcept keeps live tail, drops replaced turns', () => {
    noteDelta('s1', 'm-old', 1, 'a');
    noteDelta('s1', 'm-live', 1, 'b');
    resetSessionExcept('s1', new Set(['m-live']));
    expect(peekFrags('s1', 'm-old').size).toBe(0);
    expect(peekFrags('s1', 'm-live').get(1)).toBe('b');
  });

  it('peekFrags returns a defensive copy', () => {
    noteDelta('s1', 'm-1', 1, 'a');
    const copy = peekFrags('s1', 'm-1');
    copy.set(99, 'mutated');
    // 原缓冲不受影响
    expect(peekFrags('s1', 'm-1').has(99)).toBe(false);
  });
});

describe('stream-reassembly / live 合并帧 vs replay 单帧去重', () => {
  beforeEach(() => {
    resetSession('s1');
  });

  it('parseDeltaRange 解析合法区间并规范化 start/end', () => {
    expect(parseDeltaRange({ delta_start: 3, delta_end: 1 })).toEqual({ start: 1, end: 3 });
    expect(parseDeltaRange({ delta_start: '2', delta_end: '4' })).toEqual({ start: 2, end: 4 });
  });

  it('parseDeltaRange 对缺失/非法区间返回 null，缺省时回退 sequence', () => {
    expect(parseDeltaRange({})).toBeNull();
    expect(parseDeltaRange({}, 7)).toEqual({ start: 7, end: 7 });
    expect(parseDeltaRange({ delta_start: 0, delta_end: 0 })).toBeNull();
    expect(parseDeltaRange({ delta_start: 'x', delta_end: 2 })).toBeNull();
  });

  it('先收 live 合并帧、再回放被合并的单帧：重复前缀被剥离', () => {
    // 复现排队派发后的残字：live 限流合并帧（gw21，区间 1..2），replay 回放未合并
    // 单帧（gw20，区间 1..1）；被合并成员的 gw 未登记，精确序号去重拦不住。
    // 旧逻辑拼出「你你好，」，新逻辑恒为「你好，」。
    noteDelta('s1', 'm-1', 21, '你好，', { start: 1, end: 2 });
    expect(noteDelta('s1', 'm-1', 20, '你', { start: 1, end: 1 })).toBe('你好，');
  });

  it('合并帧与多条 replay 单帧交错：已覆盖区间整段跳过', () => {
    noteDelta('s1', 'm-1', 21, '你好', { start: 1, end: 2 }); // live 合并帧
    noteDelta('s1', 'm-1', 20, '你', { start: 1, end: 1 }); // replay 单帧
    expect(noteDelta('s1', 'm-1', 22, '好', { start: 2, end: 2 })).toBe('你好'); // replay 单帧
  });

  it('多段合并 + replay 单帧混合：全程无重复字', () => {
    noteDelta('s1', 'm-1', 21, '你好', { start: 1, end: 2 }); // live 合并 [1,2]
    noteDelta('s1', 'm-1', 23, '世界', { start: 3, end: 4 }); // live 合并 [3,4]
    noteDelta('s1', 'm-1', 20, '你', { start: 1, end: 1 }); // replay 单帧
    expect(noteDelta('s1', 'm-1', 22, '世', { start: 3, end: 3 })).toBe('你好世界'); // replay 单帧
  });

  it('区间不重叠的相邻片段文本巧合相同时不误伤（合法重复文本照常拼接）', () => {
    noteDelta('s1', 'm-1', 1, 'ab', { start: 1, end: 2 });
    expect(noteDelta('s1', 'm-1', 2, 'ab', { start: 3, end: 4 })).toBe('abab');
  });

  it('无区间的旧帧回退纯升序拼接（向后兼容）', () => {
    noteDelta('s1', 'm-1', 1, 'a', { start: 1, end: 1 });
    expect(noteDelta('s1', 'm-1', 2, 'b')).toBe('ab');
  });

  it('replaces covered raw fragments with a fully known legacy batch', () => {
    noteDelta('s1', 'm-1', 1, 'A', { start: 1, end: 1 });
    noteDelta('s1', 'm-1', 3, 'A', { start: 3, end: 3 });
    expect(noteDelta('s1', 'm-1', 3, 'ABA', { start: 1, end: 3 })).toBe('ABA');
    expect(noteDelta('s1', 'm-1', 2, 'B', { start: 2, end: 2 })).toBe('ABA');
  });

  it('requires a snapshot for partially overlapping opaque batches', () => {
    expect(noteDelta('s1', 'm-1', 3, 'ABAB', { start: 1, end: 3 })).toBe('ABAB');
    expect(noteDelta('s1', 'm-1', 5, 'ABAB', { start: 3, end: 5 })).toBeNull();
    expect(noteDelta('s1', 'm-1', 6, '!', { start: 6, end: 6 })).toBe('ABAB!');
  });
});

describe('stream-reassembly / exact live batches', () => {
  beforeEach(() => resetSession('s1'));

  it('merges raw replay and overlapping batches without deleting repeated text', () => {
    expect(noteDelta('s1', 'm-1', 14, 'AB')).toBe('AB');
    expect(noteDelta('s1', 'm-1', 14, '🙂AB', null, [[10, 2], [12, 0], [14, 2]])).toBe('🙂AB');
    expect(noteDelta('s1', 'm-1', 15, 'ABAB', null, [[14, 2], [15, 2]])).toBe('🙂ABAB');
    expect(noteDelta('s1', 'm-1', 10, '🙂')).toBe('🙂ABAB');
  });

  it('rejects malformed boundaries and conflicting overlapping text atomically', () => {
    expect(noteDelta('s1', 'm-1', 2, '🙂')).toBe('🙂');
    const malformed = [
      [[2, 1], [3, 1]], [[2, 1], [2, 2]], [[1, 1], [2, 2]], [[1, -1], [3, 4]],
      Array.from({ length: 257 }, (_, index) => [index + 1, 0]),
    ];
    for (const parts of malformed) {
      expect(noteDelta('s1', 'm-1', 3, '🙂!', null, parts)).toBeNull();
    }
    expect(noteDelta('s1', 'm-1', 3, 'X!', null, [[2, 1], [3, 1]])).toBeNull();
    expect(peekFrags('s1', 'm-1')).toEqual(new Map([[2, '🙂']]));
    expect(noteDelta('s1', 'm-1', 3, '!')).toBe('🙂!');
  });
});
