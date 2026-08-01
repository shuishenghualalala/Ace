/**
 * diffRenderUnits 纯逻辑单测（X3b）。
 *
 * 这是 X3b 的核心安全网：流式渲染没有 DOM-level 测试，diff 函数的正确性靠这里锁定。
 * 覆盖：空→空、append（一条 / 多条）、patch（sig 变）、remove（末尾 / 中间）、
 * 编辑截断（删尾再追加）、回合顺序稳定、全部 reuse（零 patch）、以及 move（remove+append 语义）。
 */
import { describe, it, expect } from 'vitest';
import { diffRenderUnits, type RenderUnit } from '../../src/ui/chat-diff';

function u(key: string, sig: string): RenderUnit {
  return { key, sig };
}

/** 只看某一类型的 op（按 key 索引），便于断言「key X 是否被声明为 patch/reuse/...」。 */
function opsByKey(ops: ReturnType<typeof diffRenderUnits>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const op of ops) out[op.key] = op.type;
  return out;
}

describe('diffRenderUnits', () => {
  it('empty → empty yields no ops', () => {
    expect(diffRenderUnits([], [])).toEqual([]);
  });

  it('empty → one unit yields a single append', () => {
    const ops = diffRenderUnits([], [u('a', '1')]);
    expect(opsByKey(ops)).toEqual({ a: 'append' });
    expect(ops).toHaveLength(1);
  });

  it('appends multiple new units (in order)', () => {
    const ops = diffRenderUnits([], [u('a', '1'), u('b', '2'), u('c', '3')]);
    expect(ops.map((o) => o.key)).toEqual(['a', 'b', 'c']);
    expect(ops.every((o) => o.type === 'append')).toBe(true);
  });

  it('identical sigs → all reuse, zero patch', () => {
    const prev = [u('a', '1'), u('b', '2'), u('c', '3')];
    const ops = diffRenderUnits(prev, prev);
    expect(opsByKey(ops)).toEqual({ a: 'reuse', b: 'reuse', c: 'reuse' });
    expect(ops.some((o) => o.type === 'patch')).toBe(false);
    expect(ops.some((o) => o.type === 'append')).toBe(false);
    expect(ops.some((o) => o.type === 'remove')).toBe(false);
  });

  it('sig change on one unit → patch only that unit, others reuse', () => {
    const prev = [u('a', '1'), u('b', '2'), u('c', '3')];
    const next = [u('a', '1'), u('b', 'CHANGED'), u('c', '3')];
    const ops = diffRenderUnits(prev, next);
    expect(opsByKey(ops)).toEqual({ a: 'reuse', b: 'patch', c: 'reuse' });
  });

  it('multiple sig changes → multiple patch ops', () => {
    const prev = [u('a', '1'), u('b', '2'), u('c', '3')];
    const next = [u('a', 'x'), u('b', 'y'), u('c', '3')];
    expect(opsByKey(diffRenderUnits(prev, next))).toEqual({ a: 'patch', b: 'patch', c: 'reuse' });
  });

  it('remove last unit', () => {
    const prev = [u('a', '1'), u('b', '2'), u('c', '3')];
    const next = [u('a', '1'), u('b', '2')];
    const ops = diffRenderUnits(prev, next);
    expect(opsByKey(ops)).toEqual({ a: 'reuse', b: 'reuse', c: 'remove' });
  });

  it('remove middle unit (others reuse)', () => {
    const prev = [u('a', '1'), u('b', '2'), u('c', '3')];
    const next = [u('a', '1'), u('c', '3')];
    const ops = diffRenderUnits(prev, next);
    expect(opsByKey(ops)).toEqual({ a: 'reuse', c: 'reuse', b: 'remove' });
  });

  it('edit-truncate: remove tail then append fresh unit', () => {
    // 模拟撤回编辑：原 [u1, t1, t2]，编辑后只保留 [u1] 并新加 [u1b]
    const prev = [u('u1', '1'), u('t1', '2'), u('t2', '3')];
    const next = [u('u1', '1'), u('u1b', 'new')];
    const ops = diffRenderUnits(prev, next);
    expect(opsByKey(ops)).toEqual({ u1: 'reuse', u1b: 'append', t1: 'remove', t2: 'remove' });
  });

  it('streaming turn: only the in-flight turn sig changes (others untouched)', () => {
    // 5 个已完成回合 + 1 个流式中（sig 随 chunk 变）
    const prev = [
      u('t0', '0'),
      u('t1', '1'),
      u('t2', '2'),
      u('t3', '3'),
      u('t4', '4'),
      u('t5', 'streaming-sig-A'),
    ];
    const next = [
      u('t0', '0'),
      u('t1', '1'),
      u('t2', '2'),
      u('t3', '3'),
      u('t4', '4'),
      u('t5', 'streaming-sig-B'), // 只有流式回合的 sig 变
    ];
    const ops = diffRenderUnits(prev, next);
    // 前 5 个 reuse，最后一个 patch —— 这是 X3b 的核心性能保证
    expect(opsByKey(ops)).toEqual({
      t0: 'reuse',
      t1: 'reuse',
      t2: 'reuse',
      t3: 'reuse',
      t4: 'reuse',
      t5: 'patch',
    });
  });

  it('move (same key, different position) emits reuse — apply reorders via appendChild-move', () => {
    // 单元换了位置但 sig 没变：diff 只声明 reuse，apply 侧按 next 顺序 appendChild-move 重排。
    // 这保证 move 不会误触发重建（节点 identity 保留）。
    const prev = [u('a', '1'), u('b', '2')];
    const next = [u('b', '2'), u('a', '1')];
    const ops = diffRenderUnits(prev, next);
    expect(opsByKey(ops)).toEqual({ b: 'reuse', a: 'reuse' });
    expect(ops.some((o) => o.type === 'patch')).toBe(false);
  });

  it('move + sig change → patch (content changed, must rebuild)', () => {
    const prev = [u('a', '1'), u('b', '2')];
    const next = [u('b', '2'), u('a', 'CHANGED')];
    expect(opsByKey(diffRenderUnits(prev, next))).toEqual({ b: 'reuse', a: 'patch' });
  });

  it('is deterministic: same inputs always produce same op list', () => {
    const prev = [u('a', '1'), u('b', '2'), u('c', '3')];
    const next = [u('a', '1'), u('d', '4')];
    const r1 = diffRenderUnits(prev, next);
    const r2 = diffRenderUnits(prev, next);
    expect(r2).toEqual(r1);
  });

  it('op order: reuse/patch/append in next order, then removes', () => {
    const prev = [u('a', '1'), u('b', '2'), u('gone', 'x')];
    const next = [u('a', '1'), u('b', 'CHANGED'), u('fresh', '9')];
    const ops = diffRenderUnits(prev, next);
    // 顺序：按 next 顺序的前缀（reuse/patch/append），再 remove
    expect(ops.map((o) => `${o.type}:${o.key}`)).toEqual([
      'reuse:a',
      'patch:b',
      'append:fresh',
      'remove:gone',
    ]);
  });

  it('duplicate keys in next: last one wins for sig comparison', () => {
    // 防御性：正常 next 不会重复 key；若出现，diff 不应崩溃，且 sig 比对用最后一次。
    const prev = [u('a', '1')];
    const next = [u('a', '1'), u('a', '2')];
    const ops = diffRenderUnits(prev, next);
    // 第二个 a 的 sig 变了 → patch（保守：重建比 stale 安全）
    const aOps = ops.filter((o) => o.key === 'a');
    expect(aOps.some((o) => o.type === 'reuse')).toBe(true);
    expect(aOps.some((o) => o.type === 'patch')).toBe(true);
  });

  it('gate/empty/anchor constant-sig units reuse across frames when state unchanged', () => {
    // 模拟 __gateway / __empty / __anchor 这类 sig 恒定的单元：连续帧全部 reuse
    const prev = [u('__gateway', 'C'), u('__anchor', 'C')];
    const next = [u('__gateway', 'C'), u('__anchor', 'C')];
    expect(opsByKey(diffRenderUnits(prev, next))).toEqual({
      __gateway: 'reuse',
      __anchor: 'reuse',
    });
  });
});
