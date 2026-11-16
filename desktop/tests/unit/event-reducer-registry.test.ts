/**
 * @vitest-environment node
 *
 * Feature Event Reducer Registry 单测：覆盖注册、分发、幂等释放、未知事件兜底。
 * 使用独立 registry 实例，避免与模块级自注册产生重复 key 冲突。
 */
import { describe, it, expect, vi } from 'vitest';
import {
  FeatureEventReducerRegistry,
  emptyFeatureReducerResult,
  uniqueMessageId,
  type FeatureReducerContext,
  type FeatureReducerResult,
} from '../../src/ui/features/event-reducer-registry';
import type { ChatMessage } from '../../src/ui/chat-render';

function makeCtx(overrides: Partial<FeatureReducerContext> = {}): FeatureReducerContext {
  return {
    sessionId: 'sid-1',
    messages: [],
    book: {} as FeatureReducerContext['book'],
    now: 1_700_000_000_000,
    sequence: 1,
    ...overrides,
  };
}

describe('FeatureEventReducerRegistry', () => {
  it('dispatch routes registered event to its reducer', () => {
    const registry = new FeatureEventReducerRegistry();
    const reducer = vi.fn((_payload: unknown, ctx: FeatureReducerContext): FeatureReducerResult => ({
      ...emptyFeatureReducerResult(),
      messageUpserts: [{
        op: 'append',
        message: { id: 'm-1', role: 'assistant', content: 'ok', timestamp: ctx.now } as ChatMessage,
      }],
    }));

    registry.register({ feature: 'wiki', event: 'cards', version: 1, reducer });
    const payload = { pages: [] };
    const ctx = makeCtx();
    const result = registry.dispatch('wiki', 'cards', 1, payload, ctx);

    expect(reducer).toHaveBeenCalledWith(payload, ctx);
    expect(result?.messageUpserts).toHaveLength(1);
  });

  it('returns null and warns for unregistered feature/event', () => {
    const registry = new FeatureEventReducerRegistry();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const result = registry.dispatch('unknown', 'event', 1, {}, makeCtx());

    expect(result).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      '[feature-event] unhandled event: feature=unknown event=event version=1',
    );
    warnSpy.mockRestore();
  });

  it('returns null and warns for mismatched version', () => {
    const registry = new FeatureEventReducerRegistry();
    registry.register({ feature: 'x', event: 'y', version: 1, reducer: () => emptyFeatureReducerResult() });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const result = registry.dispatch('x', 'y', 2, {}, makeCtx());

    expect(result).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      '[feature-event] unhandled event: feature=x event=y version=2',
    );
    warnSpy.mockRestore();
  });

  it('throws when registering the same feature/event/version twice', () => {
    const registry = new FeatureEventReducerRegistry();
    const reg = { feature: 'x', event: 'y', version: 1, reducer: () => emptyFeatureReducerResult() };
    registry.register(reg);

    expect(() => registry.register(reg)).toThrow(
      'Feature event reducer already registered: x/y@1',
    );
  });

  it('disposer removes registration and later dispatch falls back to null', () => {
    const registry = new FeatureEventReducerRegistry();
    const reducer = vi.fn(() => emptyFeatureReducerResult());
    const dispose = registry.register({ feature: 'x', event: 'y', version: 1, reducer });

    expect(registry.dispatch('x', 'y', 1, {}, makeCtx())).not.toBeNull();
    dispose();
    expect(registry.dispatch('x', 'y', 1, {}, makeCtx())).toBeNull();
    expect(reducer).toHaveBeenCalledOnce();
  });

  it('disposer is idempotent', () => {
    const registry = new FeatureEventReducerRegistry();
    const dispose = registry.register({
      feature: 'x',
      event: 'y',
      version: 1,
      reducer: () => emptyFeatureReducerResult(),
    });

    dispose();
    expect(() => dispose()).not.toThrow();
    expect(registry.dispatch('x', 'y', 1, {}, makeCtx())).toBeNull();
  });
});

describe('uniqueMessageId', () => {
  it('uses base id when no collision', () => {
    const id = uniqueMessageId({ messages: [], now: 1, sequence: 2 }, 'team');
    expect(id).toBe('team-1-2');
  });

  it('appends incremental suffix on collision', () => {
    const messages = [{ id: 'team-1-2' }, { id: 'team-1-2-1' }] as ChatMessage[];
    const id = uniqueMessageId({ messages, now: 1, sequence: 2 }, 'team');
    expect(id).toBe('team-1-2-2');
  });
});

describe('emptyFeatureReducerResult', () => {
  it('returns a stable no-op result shape', () => {
    expect(emptyFeatureReducerResult()).toEqual({
      messageUpserts: [],
      toolUpserts: [],
      replaceBook: null,
      statusHint: undefined,
      queueHint: undefined,
      finalize: false,
    });
  });
});
