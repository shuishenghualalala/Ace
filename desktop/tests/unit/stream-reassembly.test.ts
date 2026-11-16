/**
 * stream-reassembly 单测：验证按 gateway_sequence 重组 delta（根治流式串位/丢字）。
 *
 * 核心保证：无论分片以何种顺序到达，noteDelta 返回的重组正文恒等于「按 seq 升序拼接」。
 */

import { describe, it, expect, beforeEach } from 'vitest';
import {
  parseDeltaRange,
  reconstruct,
  noteDelta,
  resetAssistant,
  resetSession,
  resetSessionExcept,
  peekFrags,
} from '../../src/ui/stream-reassembly';

describe('stream-reassembly / reconstruct', () => {
  it('returns empty string for empty buffer', () => {
    expect(reconstruct(new Map())).toBe('');
  });

  it('concatenates fragments in seq order regardless of insertion order', () => {
    // 插入顺序故意乱序：seq 2 先于 seq 1
    const frags = new Map<number, string>([
      [2, 'b'],
      [1, 'a'],
      [3, 'c'],
    ]);
    expect(reconstruct(frags)).toBe('abc');
  });

  it('handles non-contiguous seqs (status/tool 帧占用中间序号)', () => {
    const frags = new Map<number, string>([
      [1, 'a'],
      [5, 'b'],
      [9, 'c'],
    ]);
    expect(reconstruct(frags)).toBe('abc');
  });
});

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
});
