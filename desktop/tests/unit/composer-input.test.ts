/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  autoresizeTextarea,
  bindComposerIme,
  createComposerImeState,
  resetTextareaHeight,
  shouldComposerSend,
  type ComposerImeState,
} from '../../src/ui/features/composer-input';

describe('composer-input', () => {
  let state: ComposerImeState;

  beforeEach(() => {
    state = createComposerImeState();
  });

  function makeEnter(opts: Partial<KeyboardEventInit & { keyCode?: number; isComposing?: boolean }> = {}): KeyboardEvent {
    return new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
      ...opts,
    });
  }

  describe('shouldComposerSend', () => {
    it('普通 Enter 允许发送', () => {
      expect(shouldComposerSend(makeEnter(), state)).toBe(true);
    });

    it('Shift+Enter 不发送（换行）', () => {
      expect(shouldComposerSend(makeEnter({ shiftKey: true }), state)).toBe(false);
    });

    it('IME 合成中 isComposing=true 不发送', () => {
      const ev = makeEnter();
      Object.defineProperty(ev, 'isComposing', { value: true });
      expect(shouldComposerSend(ev, state)).toBe(false);
    });

    it('IME 合成状态标记期间不发送', () => {
      state.isComposing = true;
      expect(shouldComposerSend(makeEnter(), state)).toBe(false);
    });

    it('Chrome IME keyCode=229 不发送', () => {
      const ev = makeEnter({ keyCode: 229 });
      expect(shouldComposerSend(ev, state)).toBe(false);
    });

    it('compositionend 后立即触发的 Enter 不发送（Safari 兼容）', () => {
      state.justComposed = true;
      expect(shouldComposerSend(makeEnter(), state)).toBe(false);
    });

    it('非 Enter 键不发送', () => {
      expect(shouldComposerSend(new KeyboardEvent('keydown', { key: 'a' }), state)).toBe(false);
    });
  });

  describe('bindComposerIme', () => {
    it('compositionstart 设置 isComposing，compositionend 清除并标记 justComposed', () => {
      const input = document.createElement('textarea');
      bindComposerIme(input, state);

      input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
      expect(state.isComposing).toBe(true);
      expect(state.justComposed).toBe(false);

      input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }));
      expect(state.isComposing).toBe(false);
      expect(state.justComposed).toBe(true);
    });

    it('compositionend 后下一宏任务清除 justComposed', async () => {
      vi.useFakeTimers();
      const input = document.createElement('textarea');
      bindComposerIme(input, state);

      input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }));
      expect(state.justComposed).toBe(true);

      vi.advanceTimersByTime(0);
      await Promise.resolve();
      expect(state.justComposed).toBe(false);

      vi.useRealTimers();
    });

    it('返回解绑函数可移除事件监听', () => {
      const input = document.createElement('textarea');
      const unbind = bindComposerIme(input, state);
      unbind();

      input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
      expect(state.isComposing).toBe(false);
    });
  });

  describe('autoresizeTextarea / resetTextareaHeight', () => {
    it('按内容高度撑高（happy-dom scrollHeight=0 → 高度收敛为 0），溢出隐藏', () => {
      const input = document.createElement('textarea');
      autoresizeTextarea(input, 140);
      expect(input.style.height).toBe('0px');
      expect(input.style.overflowY).toBe('hidden');
    });

    it('scrollHeight 超过上限时出滚动条并钳制高度', () => {
      const input = document.createElement('textarea');
      Object.defineProperty(input, 'scrollHeight', { value: 300, configurable: true });
      autoresizeTextarea(input, 140);
      expect(input.style.height).toBe('140px');
      expect(input.style.overflowY).toBe('auto');
    });

    it('resetTextareaHeight 还原内联样式', () => {
      const input = document.createElement('textarea');
      Object.defineProperty(input, 'scrollHeight', { value: 300, configurable: true });
      autoresizeTextarea(input, 140);
      resetTextareaHeight(input);
      expect(input.style.height).toBe('');
      expect(input.style.overflowY).toBe('hidden');
    });
  });
});
