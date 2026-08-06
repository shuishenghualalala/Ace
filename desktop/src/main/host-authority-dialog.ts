import { BrowserWindow, dialog } from 'electron';
import * as path from 'path';

/** Collect the host user's decision before weakening the global compatibility policy. */
export async function confirmStrictSecurityDisable(parent: BrowserWindow | null): Promise<boolean> {
  if (!parent || parent.isDestroyed()) return false;
  const result = await dialog.showMessageBox(parent, {
    type: 'warning',
    title: '关闭严格安全约束',
    message: '要启用兼容模式吗？',
    detail: [
      '兼容模式会放宽旧服务的明文传输、更新/安装完整性校验和默认审批。',
      '受管会话仍会在原生隔离环境中执行，隔离环境不可用时不会回退到宿主执行。',
    ].join('\n\n'),
    buttons: ['保持严格模式', '启用兼容模式'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  });
  return result.response === 1;
}

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
