/**
 * 会话历史只读预览弹窗（设置 → 归档会话卡片点击）。
 */

import { backendApi } from '../backend-client';
import { renderConversationPreview } from '../chat-render';
import { mapBackendHistoryItem } from './history-mapping';
import { escapeHtml, notify, state } from '../state';

function showModal(): void {
  const modal = document.getElementById('session-preview-modal');
  if (!modal) return;
  if (modal.parentElement !== document.body) {
    document.body.appendChild(modal);
  }
  modal.style.display = 'flex';
  modal.classList.add('show');
}

function hideModal(): void {
  const modal = document.getElementById('session-preview-modal');
  if (!modal) return;
  modal.style.display = 'none';
  modal.classList.remove('show');
  const root = document.getElementById('session-preview-messages');
  root?.replaceChildren();
}

/** 拉取并展示指定会话的历史消息（只读，沿用对话区渲染）。 */
export async function openSessionPreviewModal(sessionId: string, title: string): Promise<void> {
  const titleEl = document.getElementById('session-preview-title');
  const root = document.getElementById('session-preview-messages');
  if (!titleEl || !root) return;

  titleEl.textContent = title.trim() || '会话预览';
  root.innerHTML = '<p class="session-preview-empty">加载中…</p>';
  showModal();

  try {
    const items = await backendApi.history(sessionId);
    const messages = items.map((item) => mapBackendHistoryItem(item, sessionId));
    renderConversationPreview(root, messages, state.configModel);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    root.innerHTML = `<p class="session-preview-empty">加载失败：${escapeHtml(msg || '未知错误')}</p>`;
    notify('加载会话历史失败');
  }
}

/** 绑定预览弹窗关闭交互。 */
export function bindSessionPreviewModal(): void {
  document.getElementById('session-preview-close')?.addEventListener('click', hideModal);
  document.getElementById('session-preview-done')?.addEventListener('click', hideModal);
  document.getElementById('session-preview-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) hideModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.getElementById('session-preview-modal')?.classList.contains('show')) {
      hideModal();
    }
  });
}
