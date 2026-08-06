/**
 * 代码块复制按钮：扫描容器内的 `[data-copy]` 按钮，挂 click 事件复制对应代码文本。
 *
 * 设计要点：
 *  - 幂等：用 `dataset.copyBound` 标记已绑定，重复调用不会重复挂事件。
 *    chat-controller 每次 patch/reuse 重建节点后会重新调用本函数；新节点没有标记，
 *    会被绑定；复用的旧节点已有标记，跳过。
 *  - 定位代码文本：按钮在 `.code-block-header` 里，对应代码在同级 `.chat-md-code > code`。
 *    用 DOM 遍历而非闭包捕获文本，避免流式中代码内容变化后闭包持有旧文本。
 *  - 反馈：复制成功后按钮文字短暂改成「已复制」，1.2s 后恢复，给用户可见反馈。
 *  - 安全：navigator.clipboard 可能在非 secure context 不可用，做兜底 try/catch。
 */

import { setRuntimeStyle } from '../components/runtime-style';

const BOUND_FLAG = 'copyBound';
const FEEDBACK_MS = 1200;

/**
 * 给 root 内所有未绑定的 `[data-copy]` 按钮挂复制事件。
 *
 * @param root 某个消息节点或 document；只扫描其内部未绑定的按钮。
 */
export function attachCopyButtons(root: HTMLElement | Document): void {
  const buttons = root.querySelectorAll<HTMLButtonElement>('button[data-copy]');
  buttons.forEach((btn) => {
    if (btn.dataset[BOUND_FLAG]) return;
    btn.dataset[BOUND_FLAG] = '1';
    btn.addEventListener('click', () => handleCopy(btn));
  });
}

/** 复制按钮 click 处理：找对应代码文本 → 写剪贴板 → 短暂反馈。 */
async function handleCopy(btn: HTMLButtonElement): Promise<void> {
  const code = findCodeText(btn);
  if (!code) return;
  try {
    await navigator.clipboard.writeText(code);
    showFeedback(btn, '已复制');
  } catch {
    // clipboard API 不可用（非 secure context / 权限被拒）——退回 execCommand 兜底。
    if (fallbackExecCopy(code)) {
      showFeedback(btn, '已复制');
    } else {
      showFeedback(btn, '复制失败');
    }
  }
}

/** 从按钮向上找 .code-block-wrapper，再向下找 .chat-md-code > code 的 textContent。 */
function findCodeText(btn: HTMLButtonElement): string | null {
  const wrapper = btn.closest('.code-block-wrapper');
  if (!wrapper) return null;
  const codeEl = wrapper.querySelector('.chat-md-code > code') as HTMLElement | null;
  if (!codeEl) return null;
  return codeEl.textContent ?? '';
}

/** 短暂改按钮文字做反馈，FEEDBACK_MS 后恢复。多次触发会重置计时器。 */
function showFeedback(btn: HTMLButtonElement, text: string): void {
  const original = btn.dataset.originalText ?? btn.textContent ?? '复制';
  btn.dataset.originalText = original;
  btn.textContent = text;
  // 清掉上一次的计时器（dataset 上存 timer id）。
  const prev = btn.dataset.feedbackTimer;
  if (prev) window.clearTimeout(Number(prev));
  const timer = window.setTimeout(() => {
    btn.textContent = original;
    delete btn.dataset.feedbackTimer;
  }, FEEDBACK_MS);
  btn.dataset.feedbackTimer = String(timer);
}

/** execCommand 兜底：用于 navigator.clipboard 不可用的旧环境。 */
function fallbackExecCopy(text: string): boolean {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    setRuntimeStyle(ta, 'position', 'fixed');
    setRuntimeStyle(ta, 'opacity', '0');
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
