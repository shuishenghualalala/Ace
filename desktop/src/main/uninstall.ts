/**
 * Electron 主进程 – 托盘「卸载 Crew」完整流程。
 *
 * 用户确认后由主进程排空资源，再启动平台卸载程序。按用户选择保留或清理数据，
 * 清理由退出后执行的脚本负责。
 */
import { app, BrowserWindow, dialog, nativeImage, type NativeImage } from 'electron';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { launchDetachedInstaller } from './update/installer-handoff';

// ─── 可注入的依赖接口 ────────────────────────────────────────────────────────
// 通过 setUninstallDeps() 从 index.ts 注入，解耦对主模块内部状态的直接依赖。

export interface UninstallDeps {
  /** 获取主窗口引用（用于弹窗父窗口） */
  getMainWindow(): BrowserWindow | null;
  /** 取得卸载交接并等待资源关闭；已有退出流程时返回 null。 */
  beginHandoff(): Promise<void> | null;
  /** 卸载器已启动时退出，否则恢复桌面资源。 */
  finishHandoff(launched: boolean): void;
  /** 向渲染进程发送指令隐藏后端断连遮罩（卸载期间不应弹出） */
  suppressBackendOverlay(): void;
  /** Remove Windows sandbox accounts/WFP before the application binary disappears. */
  cleanupSecurity?(): Promise<boolean>;
}

let deps: UninstallDeps | null = null;

/**
 * 注入卸载所需的依赖。必须在调用 handleUninstall() 之前执行一次。
 * 通常在 index.ts 的 bootstrap 阶段调用。
 */
export function setUninstallDeps(d: UninstallDeps): void {
  deps = d;
}

// ─── 图标解析 ────────────────────────────────────────────────────────────────

/** 解析 Crew 应用图标，用于弹窗的 icon 选项。 */
function resolveDialogIcon(): NativeImage {
  const iconPath = path.join(__dirname, '../assets/icon.png');
  const image = nativeImage.createFromPath(iconPath);
  return image.isEmpty() ? nativeImage.createEmpty() : image;
}

// ─── 用户家目录 ──────────────────────────────────────────────────────────────

/** 解析 Crew 用户家目录（与后端 crew/state/home.py 保持一致）。 */
export function getCrewHome(): string {
  return process.env.CREW_HOME || path.join(os.homedir(), '.Crew');
}

// ─── 弹窗：确认卸载 ──────────────────────────────────────────────────────────

/**
 * 第一弹：确认是否卸载 Crew。
 * @returns true 表示用户点击「卸载」，false 表示取消。
 */
function confirmUninstall(win: BrowserWindow): boolean {
  const result = dialog.showMessageBoxSync(win, {
    type: 'warning',
    buttons: ['卸载', '取消'],
    defaultId: 1,
    cancelId: 1,
    title: '卸载 Crew',
    message: '确定要卸载 Crew 吗？',
    detail: '卸载将删除应用程序文件。您可以选择是否保留用户数据。',
    icon: resolveDialogIcon(),
  });
  return result === 0;
}

// ─── 弹窗：是否保留用户数据 ──────────────────────────────────────────────────

/**
 * 第二弹：是否保留用户家目录。
 * @returns true 表示用户选择「保留用户数据」，false 表示「删除所有数据」。
 */
function askKeepUserData(win: BrowserWindow): boolean {
  const crewHome = getCrewHome();
  const electronUserData = app.getPath('userData');
  const result = dialog.showMessageBoxSync(win, {
    type: 'question',
    buttons: ['保留用户数据', '删除所有数据'],
    defaultId: 0,
    cancelId: 0,
    title: '用户数据',
    message: '是否保留用户数据？',
    detail: [
      '选择「删除所有数据」将清除以下目录：',
      `• ${crewHome}`,
      '  （会话记录、技能配置、记忆文件等）',
      `• ${electronUserData}`,
      '  （Electron 运行时数据：偏好、缓存、登录态等）',
      '选择「保留」可方便日后重装时恢复。',
    ].join('\n'),
    icon: resolveDialogIcon(),
  });
  return result === 0;
}

// ─── 错误弹窗 ────────────────────────────────────────────────────────────────

function showUninstallError(win: BrowserWindow | null, title: string, message: string, detail: string): void {
  if (!win || win.isDestroyed()) return;
  dialog.showMessageBoxSync(win, {
    type: 'error',
    buttons: ['确定'],
    title,
    message,
    detail,
    icon: resolveDialogIcon(),
  });
}

function showUnsupportedPlatform(win: BrowserWindow | null): void {
  if (!win || win.isDestroyed()) return;
  dialog.showMessageBoxSync(win, {
    type: 'error',
    buttons: ['确定'],
    title: '卸载失败',
    message: '当前平台不支持托盘卸载',
    detail: '请通过系统「应用程序」或命令行卸载。',
    icon: resolveDialogIcon(),
  });
}

// ─── 用户数据清理 ────────────────────────────────────────────────────────────
//
// 用户数据目录（~/.Crew + userData）的删除统一交给平台延迟卸载脚本在
// app 退出后执行。不在主流程同步删除：
//   • userData 被 Electron 主进程持有句柄，运行时删不掉
//   • ~/.Crew 含 db/缓存，同步 fs.rmSync 阻塞主线程导致 UI 卡死。
// app 退出后所有句柄释放，延迟脚本 rm -rf 删得干净且不卡 UI。

// ─── 平台卸载器 ──────────────────────────────────────────────────────────────

/**
 * 启动平台特定的卸载程序。
 *
 * @param postQuitDirs Electron 退出后需删除的目录（如 userData）。
 *   这些目录在 app 运行时被 Electron 持有句柄，无法在 cleanupUserData 中安全删除，
 *   故交给延迟脚本在 app 完全退出后清理。
 * @returns true 表示卸载器已成功启动，false 表示启动失败。
 */
async function launchPlatformUninstaller(win: BrowserWindow, postQuitDirs: string[]): Promise<boolean> {
  if (process.platform === 'win32') {
    return launchWindowsUninstaller(win, postQuitDirs);
  }
  if (process.platform === 'linux') {
    await launchLinuxUninstaller(postQuitDirs);
    return true;
  }
  if (process.platform === 'darwin') {
    await launchMacOSUninstaller(postQuitDirs);
    return true;
  }
  showUnsupportedPlatform(win);
  return false;
}

/**
 * Windows: 通过延迟脚本启动 Inno Setup unins000.exe /SILENT。
 *
 * 🌟 修复残留文件问题：之前直接 spawn unins000.exe 时，Electron 进程尚未退出，
 * Inno Setup 尝试删除安装目录中的文件时会因文件被占用而跳过，导致
 * _internal/runtimes 等目录残留。改用延迟脚本：等 Electron 完全退出后再执行
 * 卸载，确保所有文件句柄已释放。
 */
async function launchWindowsUninstaller(win: BrowserWindow, postQuitDirs: string[]): Promise<boolean> {
  const appDir = path.dirname(path.dirname(process.execPath));
  const uninstaller = path.join(appDir, 'unins000.exe');

  if (!fs.existsSync(uninstaller)) {
    showUninstallError(
      win,
      '卸载失败',
      '找不到卸载程序',
      `预期路径：${uninstaller}\n请通过系统「控制面板」或「设置 → 应用」卸载。`,
    );
    return false;
  }

  // 生成延迟卸载批处理脚本：
  // 1. 等待 3 秒让 Electron 完全退出（释放所有文件句柄）
  // 2. 删除 postQuitDirs（如 userData，运行时被 Electron 持有，须退出后删）
  // 3. 执行 Inno Setup 卸载器
  // 4. 卸载完成后自删除脚本
  const scriptPath = path.join(os.tmpdir(), `crew-uninstall-${Date.now()}.bat`);
  const lines = [
    '@echo off',
    'REM 等待 Electron 完全退出，释放安装目录与 userData 中的文件句柄',
    'timeout /t 3 /nobreak >nul',
  ];
  for (const dir of postQuitDirs) {
    // rmdir /s /q 静默递归删除；路径含空格需引号。2>nul 抑制「找不到目录」错误。
    lines.push(`if exist "${dir}" rmdir /s /q "${dir}" 2>nul`);
  }
  lines.push(`"${uninstaller}" /SILENT`);
  lines.push(`del "%~f0"`);
  const scriptContent = lines.join('\r\n');

  fs.writeFileSync(scriptPath, scriptContent, { mode: 0o755 });
  await launchDetachedInstaller('cmd.exe', ['/c', scriptPath], { windowsHide: true });
  return true;
}

/**
 * Linux: 创建临时 shell 脚本，延迟 3 秒后执行 pkexec apt remove -y crew-desktop。
 * 延迟是为了确保 Electron 完全退出，避免 dpkg 锁冲突。
 * 脚本最后自删除（rm -f）。
 */
async function launchLinuxUninstaller(postQuitDirs: string[]): Promise<void> {
  const tmpScript = path.join(os.tmpdir(), `crew-uninstall-${Date.now()}.sh`);
  const lines = [
    '#!/bin/sh',
    '# 等待 Electron 退出后再执行卸载（避免 dpkg 锁冲突）',
    'sleep 3',
  ];
  // Electron 退出后删除 userData 等运行时目录
  for (const dir of postQuitDirs) {
    lines.push(`rm -rf "${dir}"`);
  }
  lines.push('# pkexec 弹窗让用户输入密码，执行 apt remove');
  lines.push('pkexec apt remove -y crew-desktop');
  lines.push('# 清理临时脚本');
  lines.push(`rm -f "${tmpScript}"`);
  const scriptContent = lines.join('\n');

  fs.writeFileSync(tmpScript, scriptContent, { mode: 0o755 });
  await launchDetachedInstaller(tmpScript, []);
  console.log('[uninstall] Linux 卸载脚本已生成:', tmpScript);
}

/**
 * macOS: 创建临时 shell 脚本，延迟 3 秒后删除 .app bundle。
 * macOS 无系统级卸载机制，DMG 安装的应用只需删除 .app 目录即可。
 * 脚本最后自删除。
 */
async function launchMacOSUninstaller(postQuitDirs: string[]): Promise<void> {
  // macOS .app 结构: Crew.app/Contents/MacOS/crew-desktop (process.execPath)
  // appDir 指向 .app 根目录
  const appDir = path.dirname(path.dirname(app.getPath('exe')));
  const tmpScript = path.join(os.tmpdir(), `crew-uninstall-${Date.now()}.sh`);
  const lines = [
    '#!/bin/bash',
    '# 等待 Electron 完全退出后再删除 .app bundle',
    'sleep 3',
  ];
  // Electron 退出后删除 userData 等运行时目录
  for (const dir of postQuitDirs) {
    lines.push(`rm -rf "${dir}"`);
  }
  lines.push(`rm -rf "${appDir}"`);
  lines.push('# 清理临时脚本');
  lines.push('rm -f "$0"');
  const scriptContent = lines.join('\n');

  fs.writeFileSync(tmpScript, scriptContent, { mode: 0o755 });
  await launchDetachedInstaller('bash', [tmpScript]);
  console.log('[uninstall] macOS 卸载脚本已生成:', tmpScript);
}

// ─── 主入口 ─────────────────────────────────────────────────────────────────

/**
 * 托盘「卸载 Crew」完整流程入口。
 *
 * 前置条件：setUninstallDeps() 已被调用。
 */
export async function handleUninstall(): Promise<void> {
  const shutdown = deps;
  if (!shutdown) {
    console.error('[uninstall] 依赖未注入，请先调用 setUninstallDeps()');
    return;
  }

  const win = shutdown.getMainWindow();
  if (!win || win.isDestroyed()) {
    console.error('[uninstall] 主窗口不存在，无法执行卸载');
    return;
  }

  if (!confirmUninstall(win)) return;
  const keepUserData = askKeepUserData(win);
  console.log(`[uninstall] 用户选择: ${keepUserData ? '保留用户数据' : '删除所有数据'}`);

  const preparation = shutdown.beginHandoff();
  if (!preparation) return;
  let launched = false;
  try {
    // Drain rejection is still handled if the overlay notification fails.
    try {
      shutdown.suppressBackendOverlay();
    } catch (error) {
      console.warn('[uninstall] 无法隐藏后端遮罩:', error);
    }
    try {
      await preparation;
    } catch (error) {
      console.error('[uninstall] 关闭后台资源失败:', error);
      showUninstallError(win, '后台资源未关闭', '未继续卸载 Crew', '请关闭应用后再重试。');
      return;
    }

    if (shutdown.cleanupSecurity && !await shutdown.cleanupSecurity()) {
      showUninstallError(
        win,
        '安全组件清理失败',
        '未继续卸载 Crew',
        'Windows UAC 被拒绝或安全组件清理失败。请重新启动应用后再试，避免遗留技术账号和网络规则。',
      );
      return;
    }

    const postQuitDirs = keepUserData ? [] : [getCrewHome(), app.getPath('userData')];
    launched = await launchPlatformUninstaller(win, postQuitDirs);
  } catch (error) {
    console.error('[uninstall] 卸载交接失败:', error);
    showUninstallError(
      win,
      '卸载失败',
      '未继续卸载 Crew',
      error instanceof Error ? error.message : String(error),
    );
  } finally {
    shutdown.finishHandoff(launched);
  }
}
