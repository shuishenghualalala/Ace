/**
 * 设置 → 项目管理：列出已注册工作空间，支持取消隐藏、绑定目录与删除。
 */

import { backendApi } from '../backend-client';
import { escapeHtml, notify, state } from '../state';
import { syncLibrarySectionCounts } from './settings-library';
import { openSession } from './session-controller';
import { deleteWorkspaceById, loadWorkspaces, setWorkspaceHidden } from './workspaces';

function projectsRoot(): HTMLElement | null {
  return document.getElementById('settings-library-projects') ?? document.getElementById('settings-projects-root');
}

async function bindProjectFolder(projectId: string): Promise<void> {
  const selectFolder = window.Crew?.selectFolder;
  if (!selectFolder) {
    notify('请在桌面端选择本地文件夹');
    return;
  }
  const paths = await selectFolder();
  const rootPath = paths?.[0]?.trim();
  if (!rootPath) return;
  try {
    await backendApi.updateWorkspace(projectId, { root_path: rootPath });
    await loadWorkspaces();
    notify('已绑定项目目录');
    void renderSettingsProjectsPage();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    notify(`绑定目录失败：${msg || '未知错误'}`);
  }
}

/** 渲染项目管理列表（打开设置页「项目与会话 → 项目」时调用）。 */
export async function renderSettingsProjectsPage(): Promise<void> {
  const root = projectsRoot();
  if (!root) return;

  await loadWorkspaces();
  const projects = state.workspaces.filter((ws) => ws.id !== 'default');

  if (!projects.length) {
    root.innerHTML =
      '<p class="library-empty-hint">暂无已注册的工作空间。侧栏点击「新建工作空间」可选择本地文件夹创建。</p>';
    syncLibrarySectionCounts({ projects: 0 });
    return;
  }

  root.innerHTML = projects
    .map((ws) => {
      const hasPath = Boolean(ws.root_path?.trim());
      const path = hasPath
        ? ws.root_path!.trim()
        : '未绑定本地目录（创建时未选文件夹，或目录已移除）';
      const pathClass = hasPath ? '' : ' settings-project-path--warn';
      const hiddenBadge = ws.hidden ? '<span class="settings-project-badge">已隐藏</span>' : '';
      const unhideBtn = ws.hidden
        ? '<button type="button" class="set-v2-btn" data-project-unhide>取消隐藏</button>'
        : '';
      const bindBtn = hasPath
        ? ''
        : '<button type="button" class="set-v2-btn" data-project-bind>绑定目录</button>';
      return `
        <div class="set-v2-row settings-project-row" data-project-id="${escapeHtml(ws.id)}">
          <div class="set-v2-row__copy">
            <div class="set-v2-row__label">${escapeHtml(ws.name)} ${hiddenBadge}</div>
            <div class="set-v2-row__desc${pathClass}">${escapeHtml(path)}</div>
          </div>
          <div class="set-v2-row__control settings-project-actions">
            ${bindBtn}
            ${unhideBtn}
            <button type="button" class="set-v2-btn set-v2-btn--danger" data-project-delete>删除</button>
          </div>
        </div>
      `;
    })
    .join('');

  syncLibrarySectionCounts({ projects: projects.length });

  root.querySelectorAll('[data-project-bind]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.closest('[data-project-id]')?.getAttribute('data-project-id');
      if (!id) return;
      void bindProjectFolder(id);
    });
  });

  root.querySelectorAll('[data-project-unhide]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.closest('[data-project-id]')?.getAttribute('data-project-id');
      if (!id) return;
      void setWorkspaceHidden(id, false, openSession).then(() => {
        notify('已取消隐藏');
        void renderSettingsProjectsPage();
      });
    });
  });

  root.querySelectorAll('[data-project-delete]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.closest('[data-project-id]')?.getAttribute('data-project-id');
      if (!id) return;
      void deleteWorkspaceById(id, openSession).then(() => void renderSettingsProjectsPage());
    });
  });
}
