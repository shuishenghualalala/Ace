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

export async function confirmDefaultFullAccess(parent: BrowserWindow | null): Promise<boolean> {
  if (!parent || parent.isDestroyed()) return false;
  const { response } = await dialog.showMessageBox(parent, {
    type: 'warning',
    title: '新对话默认权限',
    message: '将完全访问设为默认？',
    detail: '之后新建的对话会使用当前系统用户权限运行命令，可读写工作区外的文件并联网。',
    buttons: ['取消', '设为默认'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  });
  return response === 1;
}
