/**
 * 左侧导航栏展开/收起（#sidebar-expand-btn 占位 UI 接线）。
 *
 * 窄轨（默认 52px）仅图标；展开后显示导航文字标签，便于辨认 Tab。
 */

const STORAGE_KEY = 'crew.sidebarNavExpanded';

function applyExpanded(expanded: boolean): void {
  const sidebar = document.getElementById('sidebar');
  sidebar?.classList.toggle('sidebar-nav-expanded', expanded);
  const btn = document.getElementById('sidebar-expand-btn');
  if (btn) {
    btn.setAttribute('aria-expanded', String(expanded));
    btn.title = expanded ? '收起导航' : '展开导航';
    btn.setAttribute('aria-label', btn.title);
  }
}

export function applySidebarNavExpanded(): void {
  const stored = localStorage.getItem(STORAGE_KEY) === 'true';
  applyExpanded(stored);
}

export function bindSidebarExpand(): void {
  applySidebarNavExpanded();
  document.getElementById('sidebar-expand-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    const sidebar = document.getElementById('sidebar');
    const next = !sidebar?.classList.contains('sidebar-nav-expanded');
    localStorage.setItem(STORAGE_KEY, String(next));
    applyExpanded(next);
  });
}
