/**
 * Composer 输入框键盘/IME 处理
 *
 * 将「Enter 发送」与中文/日文/韩文等 IME 合成状态判断抽成可测模块，
 * 供 desktop/src/ui/index.ts 绑定 #chat-input 使用。
 */

import { clearRuntimeStyle, setRuntimeStyle } from '../components/runtime-style';

export interface ComposerImeState {
  /** 当前是否处于 composition 合成阶段 */
  isComposing: boolean;
  /**
   * Safari 兼容标记：在 Safari 中 compositionend 会在 keydown 之前触发，
   * 此时 isComposing 已为 false，需要标记"刚刚结束合成"来拦截确认选字的那一次 Enter。
   */
  justComposed: boolean;
}

export function createComposerImeState(): ComposerImeState {
  return { isComposing: false, justComposed: false };
}

/**
 * 绑定 compositionstart/compositionend 事件，返回解绑函数。
 * compositionend 后置 justComposed=true，下一宏任务清零。
 */
export function bindComposerIme(input: HTMLTextAreaElement, state: ComposerImeState): () => void {
  const onStart = (): void => {
    state.isComposing = true;
  };
  const onEnd = (): void => {
    state.isComposing = false;
    state.justComposed = true;
    // Chrome 中 compositionend 后的下一次真实 Enter 是不同宏任务，不会被误拦截；
    // Safari 中 compositionend 先于 keydown，靠此标记拦住紧跟的确认 Enter。
    window.setTimeout(() => {
      state.justComposed = false;
    }, 0);
  };
  input.addEventListener('compositionstart', onStart);
  input.addEventListener('compositionend', onEnd);
  return () => {
    input.removeEventListener('compositionstart', onStart);
    input.removeEventListener('compositionend', onEnd);
  };
}

/**
 * 判断当前 keydown 事件是否应该触发发送。
 * 三层拦截：isComposing、keyCode===229、justComposed。
 */
export function shouldComposerSend(event: KeyboardEvent, state: ComposerImeState): boolean {
  if (event.key !== 'Enter' || event.shiftKey) return false;
  if (event.isComposing || state.isComposing) return false;
  if (event.keyCode === 229) return false;
  if (state.justComposed) return false;
  return true;
}

/**
 * 输入框自适应高度：随内容撑高，超过 maxHeight 出滚动条。
 * 主对话（index.ts）与 Wiki 右栏 Composer（wiki-agent.ts）共用。
 */
export function autoresizeTextarea(input: HTMLTextAreaElement, maxHeight: number): void {
  setRuntimeStyle(input, 'height', 'auto');
  const nextHeight = Math.min(input.scrollHeight, maxHeight);
  setRuntimeStyle(input, 'height', `${nextHeight}px`);
  setRuntimeStyle(input, 'overflowY', input.scrollHeight > maxHeight ? 'auto' : 'hidden');
}

/** 发送/清空后还原输入框高度（与 autoresizeTextarea 配对）。 */
export function resetTextareaHeight(input: HTMLTextAreaElement): void {
  clearRuntimeStyle(input, 'height');
  setRuntimeStyle(input, 'overflowY', 'hidden');
}
