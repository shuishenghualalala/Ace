/**
 * 设置 → 项目与会话：折叠面板，同时展示工作空间与归档会话。
 */

import { renderSettingsProjectsPage } from './settings-projects';
import { renderSettingsSessionsPage } from './settings-sessions';

export type LibrarySection = 'projects' | 'sessions';

const STORAGE_PREFIX = 'crew.settings.library.open.';

function sectionOpen(section: LibrarySection): boolean {
  const stored = localStorage.getItem(`${STORAGE_PREFIX}${section}`);
  if (stored == null) return true;
  return stored === 'true';
}

function setSectionOpen(section: LibrarySection, open: boolean): void {
  localStorage.setItem(`${STORAGE_PREFIX}${section}`, open ? 'true' : 'false');
  const item = document.querySelector<HTMLElement>(`.library-accordion-item[data-library-section="${section}"]`);
  const header = item?.querySelector<HTMLElement>('[data-library-toggle]');
  item?.classList.toggle('is-open', open);
  header?.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function applyStoredAccordionState(): void {
  (['projects', 'sessions'] as const).forEach((section) => setSectionOpen(section, sectionOpen(section)));
}

/** 更新折叠标题上的数量角标（传入的字段才会更新）。 */
export function syncLibrarySectionCounts(updates: { projects?: number; sessions?: number }): void {
  if (updates.projects != null) {
    const el = document.getElementById('settings-library-projects-count');
    if (el) el.textContent = String(updates.projects);
  }
  if (updates.sessions != null) {
    const el = document.getElementById('settings-library-sessions-count');
    if (el) el.textContent = String(updates.sessions);
  }
}

/** 设置弹窗已打开且停在「项目与会话」时，刷新列表（侧栏归档/删除后同步）。 */
export async function refreshSettingsLibraryIfVisible(): Promise<void> {
  const pane = document.getElementById('settings-pane-library');
  const modal = document.getElementById('settings-modal');
  if (!pane || pane.hidden || !modal?.classList.contains('show')) return;
  await renderSettingsLibraryPane();
}

/** 打开「项目与会话」面板：两块内容一并渲染。 */
export async function renderSettingsLibraryPane(): Promise<void> {
  applyStoredAccordionState();
  await Promise.all([renderSettingsProjectsPage(), renderSettingsSessionsPage()]);
}

/** 绑定折叠面板交互。 */
export function bindSettingsLibraryUi(): void {
  applyStoredAccordionState();
  document.querySelectorAll<HTMLElement>('[data-library-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const section = btn.getAttribute('data-library-toggle') as LibrarySection | null;
      if (!section) return;
      const open = !sectionOpen(section);
      setSectionOpen(section, open);
    });
  });
}
