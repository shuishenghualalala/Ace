/**
 * 工作室 overlay 面板状态（纯逻辑，无 DOM 依赖）。
 * 供 studio-view.ts 与 chat-controller.ts 共享，避免循环 import。
 */

export type StudioPanel = 'none' | 'chat' | 'history';

let studioPanel: StudioPanel = 'none';
let panelCollapsed = false;

export function getStudioPanel(): StudioPanel {
  return studioPanel;
}

export function isStudioPanelCollapsed(): boolean {
  return panelCollapsed;
}

export function resetStudioChromeState(): void {
  studioPanel = 'none';
  panelCollapsed = false;
}

/** 三按钮 toggle：再次点击当前项则收起为 none。 */
export function resolveStudioPanelToggle(current: StudioPanel, clicked: StudioPanel): StudioPanel {
  if (current === clicked) return 'none';
  return clicked;
}

export function setStudioPanel(next: StudioPanel): void {
  studioPanel = next;
  if (next === 'none') {
    panelCollapsed = false;
  }
  dispatchStudioChromeChanged();
}

export function setStudioPanelCollapsed(collapsed: boolean): void {
  panelCollapsed = collapsed;
  dispatchStudioChromeChanged();
}

/** 发消息后自动展开对话面板（不切回「对话」子页）。 */
export function openStudioChatPanel(): void {
  studioPanel = 'chat';
  panelCollapsed = false;
  dispatchStudioChromeChanged();
}

export function resolveChatRenderTargetId(isStudioMode: boolean): string {
  if (isStudioMode && studioPanel === 'chat' && !panelCollapsed) {
    return 'studio-chat-messages';
  }
  return 'chat-messages';
}

/** 是否处于「工作室」子页（像素 overlay）。 */
export function isStudioView(): boolean {
  return typeof document !== 'undefined' && document.body.classList.contains('studio-mode');
}

function dispatchStudioChromeChanged(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('studio-chrome:changed'));
  }
}
