import { BrowserWindow, dialog } from 'electron';
import * as path from 'path';

/** Ask for one host-mutating action without delegating authority to the renderer. */
export async function confirmCuaDriverInstall(parent: BrowserWindow | null): Promise<boolean> {
  if (!parent || parent.isDestroyed()) return false;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (confirmed: boolean): void => {
      if (settled) return;
      settled = true;
      resolve(confirmed);
      if (!window.isDestroyed()) window.close();
    };
    const window = new BrowserWindow({
      parent,
      modal: true,
      show: false,
      width: 520,
      height: 360,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      frame: false,
      backgroundColor: '#ffffff',
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        devTools: false,
      },
    });
    window.setMenuBarVisibility(false);
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.on('will-navigate', (event) => event.preventDefault());
    window.webContents.on('did-navigate-in-page', (_event, url) => {
      const action = new URL(url).hash;
      if (action === '#confirm') finish(true);
      if (action === '#cancel') finish(false);
    });
    window.webContents.on('before-input-event', (_event, input) => {
      if (input.key === 'Escape') finish(false);
    });
    window.once('ready-to-show', () => window.show());
    window.once('closed', () => {
      if (!settled) {
        settled = true;
        resolve(false);
      }
    });
    void window.loadFile(path.join(__dirname, '../assets/host-authority-dialog.html'));
  });
}

/** Require an OS-owned second confirmation before requesting full-access mode. */
export async function confirmFullAccessMode(parent: BrowserWindow | null): Promise<boolean> {
  if (!parent || parent.isDestroyed()) return false;
  const result = await dialog.showMessageBox(parent, {
    type: 'warning',
    title: '确认开启完全访问',
    message: '完全访问会扩大文件与命令能力',
    detail: '仅在你明确知道当前任务需要这些权限时继续。此确认只对本次模式切换有效。',
    buttons: ['取消', '确认开启'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  });
  return result.response === 1;
}

/** Require an OS-owned second confirmation for a high-risk command approval. */
export async function confirmDangerousAction(parent: BrowserWindow | null): Promise<boolean> {
  if (!parent || parent.isDestroyed()) return false;
  const result = await dialog.showMessageBox(parent, {
    type: 'warning',
    title: '再次确认高风险操作',
    message: '此操作被识别为高风险命令',
    detail: '继续可能删除数据、修改系统或影响其他进程。请仅在审批内容与预期完全一致时确认。',
    buttons: ['取消', '确认执行'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  });
  return result.response === 1;
}
