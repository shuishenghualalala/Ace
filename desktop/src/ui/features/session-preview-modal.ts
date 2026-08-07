/**
 * 会话历史只读预览弹窗（设置 → 归档会话卡片点击）。
 */

import { backendApi } from '../backend-client';
import { renderConversationPreview } from '../chat-render';
import { mapBackendHistoryItem } from './history-mapping';
import { notify, state } from '../state';
import {
  closeAccountOverlay,
  ensureAccountOverlay,
  openAccountOverlay,
} from './account-overlays';

function hideModal(): void {
  closeAccountOverlay('session-preview-modal');
  const root = document.getElementById('session-preview-messages');
  root?.replaceChildren();
}

/** 拉取并展示指定会话的历史消息（只读，沿用对话区渲染）。 */
export async function openSessionPreviewModal(sessionId: string, title: string): Promise<void> {
  const titleEl = document.getElementById('session-preview-title');
  const root = document.getElementById('session-preview-messages');
  if (!titleEl || !root) return;

  titleEl.textContent = title.trim() || '会话预览';
  const loading = document.createElement('p');
  loading.className = 'session-preview-empty';
  loading.textContent = '加载中…';
  root.replaceChildren(loading);
  openAccountOverlay('session-preview-modal', {
    initialFocus: document.getElementById('session-preview-close') ?? undefined,
  });

  try {
    const items = await backendApi.history(sessionId);
    const messages = items.map((item) => mapBackendHistoryItem(item, sessionId));
    renderConversationPreview(root, messages, state.configModel);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    const error = document.createElement('p');
    error.className = 'session-preview-empty';
    error.textContent = `加载失败：${msg || '未知错误'}`;
    root.replaceChildren(error);
    notify('加载会话历史失败');
  }
}

/** 绑定预览弹窗关闭交互。 */
export function bindSessionPreviewModal(): void {
  ensureAccountOverlay('session-preview-modal');
  document.getElementById('session-preview-close')?.addEventListener('click', hideModal);
  document.getElementById('session-preview-done')?.addEventListener('click', hideModal);
}
