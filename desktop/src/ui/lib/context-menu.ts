/**
 * 轻量固定定位上下文菜单（复用 assets/styles/context-menu.css）。
 * 点击外部、Esc、滚动时自动关闭。
 */

export type ContextMenuItem = {
  id: string;
  label: string;
  disabled?: boolean;
  danger?: boolean;
  onSelect: () => void | Promise<void>;
};

let activeMenu: HTMLElement | null = null;
let dismissHandlers: Array<() => void> = [];

/** 关闭当前菜单并移除全局监听。 */
export function dismissContextMenu(): void {
  activeMenu?.remove();
  activeMenu = null;
  for (const off of dismissHandlers) off();
  dismissHandlers = [];
}

function positionMenu(menu: HTMLElement, anchor: HTMLElement): void {
  const rect = anchor.getBoundingClientRect();
  const margin = 6;
  menu.style.visibility = 'hidden';
  menu.style.left = '0';
  menu.style.top = '0';
  document.body.appendChild(menu);
  const menuRect = menu.getBoundingClientRect();
  let left = rect.right - menuRect.width;
  let top = rect.bottom + margin;
  if (left < margin) left = margin;
  if (left + menuRect.width > window.innerWidth - margin) {
    left = Math.max(margin, window.innerWidth - menuRect.width - margin);
  }
  if (top + menuRect.height > window.innerHeight - margin) {
    top = Math.max(margin, rect.top - menuRect.height - margin);
  }
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.style.visibility = '';
}

/**
 * 在锚点元素旁展示菜单。
 * @param anchor 通常为 ⋯ 按钮或会话行
 */
export function showContextMenu(anchor: HTMLElement, items: ContextMenuItem[]): void {
  dismissContextMenu();
  const menu = document.createElement('div');
  menu.className = 'context-menu';
  menu.setAttribute('role', 'menu');
  for (const item of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'context-menu-item';
    btn.setAttribute('role', 'menuitem');
    btn.textContent = item.label;
    btn.disabled = !!item.disabled;
    if (item.danger) btn.dataset.danger = 'true';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (btn.disabled) return;
      dismissContextMenu();
      void item.onSelect();
    });
    menu.appendChild(btn);
  }
  positionMenu(menu, anchor);
  activeMenu = menu;

  const onOutside = (e: MouseEvent) => {
    const t = e.target as Node | null;
    if (t && menu.contains(t)) return;
    dismissContextMenu();
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') dismissContextMenu();
  };
  const onScroll = () => dismissContextMenu();
  window.setTimeout(() => {
    document.addEventListener('click', onOutside, true);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onScroll, true);
  }, 0);
  dismissHandlers.push(() => document.removeEventListener('click', onOutside, true));
  dismissHandlers.push(() => document.removeEventListener('keydown', onKey));
  dismissHandlers.push(() => window.removeEventListener('scroll', onScroll, true));
}
