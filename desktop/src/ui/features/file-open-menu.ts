import { notify } from '../state';

let activeMenu: HTMLElement | null = null;
let activeAnchor: HTMLElement | null = null;
let removeGlobalListeners: (() => void) | null = null;

const FOLDER_ICON = '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M2.5 5.5h5l1.4 1.6h8.6v7.4a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5v-9Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M2.5 7.1h15" stroke="currentColor" stroke-width="1.5"/></svg>';
const APP_ICON = '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="3" y="3" width="14" height="14" rx="3" stroke="currentColor" stroke-width="1.5"/><path d="M7 13 10 7l3 6M8.2 10.8h3.6" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg>';
type MenuIcon = typeof FOLDER_ICON | typeof APP_ICON;

function closeFileOpenMenu(): void {
  activeMenu?.remove();
  if (activeAnchor) activeAnchor.setAttribute('aria-expanded', 'false');
  activeMenu = null;
  activeAnchor = null;
  removeGlobalListeners?.();
  removeGlobalListeners = null;
}

function positionMenu(menu: HTMLElement, anchor: HTMLElement): void {
  const anchorRect = anchor.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const margin = 8;
  const left = Math.min(
    Math.max(margin, anchorRect.right - menuRect.width),
    window.innerWidth - menuRect.width - margin,
  );
  const preferredTop = anchorRect.bottom + 5;
  const top = preferredTop + menuRect.height <= window.innerHeight - margin
    ? preferredTop
    : Math.max(margin, anchorRect.top - menuRect.height - 5);
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

function menuButton(label: string, icon: MenuIcon): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'file-open-menu__item';
  button.setAttribute('role', 'menuitem');
  const iconWrap = document.createElement('span');
  iconWrap.className = 'file-open-menu__icon';
  // `icon` is restricted to the two compile-time SVG constants above.
  iconWrap.innerHTML = icon;
  button.appendChild(iconWrap);
  const text = document.createElement('span');
  text.className = 'file-open-menu__label';
  text.textContent = label;
  button.appendChild(text);
  return button;
}

async function revealInFolder(targetPath: string): Promise<void> {
  if (!window.Crew?.showItemInFolder) {
    notify('当前环境不支持打开文件夹');
    return;
  }
  try {
    await window.Crew.showItemInFolder(targetPath);
  } catch (error) {
    notify(`打开失败：${error instanceof Error ? error.message : String(error)}`);
  }
}

async function openWithApplication(
  targetPath: string,
  application: { id: string; name: string },
): Promise<void> {
  if (!window.Crew?.openPathWith) {
    notify('当前环境不支持指定程序打开');
    return;
  }
  try {
    await window.Crew.openPathWith(targetPath, application.id);
    notify(`已用 ${application.name} 打开`);
  } catch (error) {
    notify(`打开失败：${error instanceof Error ? error.message : String(error)}`);
  }
}

function bindGlobalClose(menu: HTMLElement, anchor: HTMLElement): () => void {
  const onPointerDown = (event: PointerEvent) => {
    const target = event.target instanceof Node ? event.target : null;
    if (target && (menu.contains(target) || anchor.contains(target))) return;
    closeFileOpenMenu();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeFileOpenMenu();
      anchor.focus();
    }
  };
  const onViewportChange = () => closeFileOpenMenu();
  document.addEventListener('pointerdown', onPointerDown, true);
  document.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('resize', onViewportChange);
  window.addEventListener('scroll', onViewportChange, true);
  return () => {
    document.removeEventListener('pointerdown', onPointerDown, true);
    document.removeEventListener('keydown', onKeyDown, true);
    window.removeEventListener('resize', onViewportChange);
    window.removeEventListener('scroll', onViewportChange, true);
  };
}

/**
 * 打开文件操作菜单。菜单中的应用标识来自主进程，实际启动时主进程会重新
 * 枚举并校验，renderer 不能借此启动任意可执行文件。
 */
export async function showFileOpenMenu(anchor: HTMLElement, targetPath: string): Promise<void> {
  if (activeAnchor === anchor && activeMenu) {
    closeFileOpenMenu();
    return;
  }
  closeFileOpenMenu();

  const menu = document.createElement('div');
  menu.className = 'file-open-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', '文件打开方式');

  const reveal = menuButton('在资源管理器中显示', FOLDER_ICON);
  reveal.addEventListener('click', () => {
    closeFileOpenMenu();
    void revealInFolder(targetPath);
  });
  menu.appendChild(reveal);

  const separator = document.createElement('div');
  separator.className = 'file-open-menu__separator';
  separator.setAttribute('role', 'separator');
  menu.appendChild(separator);

  const loading = document.createElement('div');
  loading.className = 'file-open-menu__state';
  loading.textContent = '正在查找可用程序…';
  menu.appendChild(loading);

  document.body.appendChild(menu);
  activeMenu = menu;
  activeAnchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  removeGlobalListeners = bindGlobalClose(menu, anchor);
  positionMenu(menu, anchor);

  try {
    const applications = await window.Crew?.listOpenApplications?.(targetPath) ?? [];
    if (activeMenu !== menu) return;
    loading.remove();
    if (applications.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'file-open-menu__state';
      empty.textContent = '未发现可打开此文件的程序';
      menu.appendChild(empty);
    } else {
      for (const application of applications) {
        const button = menuButton(`打开于 ${application.name}`, APP_ICON);
        button.addEventListener('click', () => {
          closeFileOpenMenu();
          void openWithApplication(targetPath, application);
        });
        menu.appendChild(button);
      }
    }
    positionMenu(menu, anchor);
  } catch (error) {
    if (activeMenu !== menu) return;
    loading.textContent = `程序列表读取失败：${error instanceof Error ? error.message : String(error)}`;
    loading.classList.add('is-error');
    positionMenu(menu, anchor);
  }
}
