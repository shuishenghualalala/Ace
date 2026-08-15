/**
 * Electron 主进程 – 托盘「卸载 Crew」完整流程。
 *
 * 流程：
 *   1. confirmUninstall()        → 弹窗确认是否卸载
 *   2. askKeepUserData()         → 弹窗确认是否保留用户数据
 *   3. stopManagedGateway()      → 停止 gateway 子进程，释放文件句柄
 *   4. 收集 postQuitDirs（~/.Crew + userData），交给延迟脚本 app 退出后删
 *   5. launchPlatformUninstaller() → Windows: Inno Setup unins000.exe /SILENT
 *                                      Linux:  pkexec apt remove -y crew-desktop
 *   6. app.quit() → 延迟脚本在 app 退出后 rm -rf postQuitDirs
 *
 * 关键设计决策：
 *   • 不弹「正在准备卸载」loading 窗口——早期版本加过，但 loading 窗口偶现不消失
 *     （app.quit 被卡时进度窗口残留）。改回无 loading 流程：确认后直接走停 gateway
 *     → 启动卸载器 → app.quit，主窗口短暂静止后随 app 退出。
 *   • 用户数据目录（~/.Crew + userData）不在主流程同步删除——同步 fs.rmSync
 *     删大目录会阻塞主线程数秒。改由延迟脚本在 app 退出后删除，此时所有文件句柄
 *     已释放，删得干净且不卡 UI。
 *   • 用户数据删除在 Electron 侧完成（而非 Inno Setup [UninstallDelete]），
 *     因为 Inno Setup 静态配置无法感知用户在弹窗中的「保留 / 删除」选择。
 */
import { app, BrowserWindow, dialog, nativeImage, type NativeImage } from 'electron';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { spawn } from 'child_process';
import { hardenedChildProcessOptions } from './process-environment';

// ─── 可注入的依赖接口 ────────────────────────────────────────────────────────
// 通过 setUninstallDeps() 从 index.ts 注入，解耦对主模块内部状态的直接依赖。

export interface UninstallDeps {
  /** 获取主窗口引用（用于弹窗父窗口） */
  getMainWindow(): BrowserWindow | null;
  /** 停止 Electron 管理的 gateway 子进程并等待退出 */
  stopManagedGateway(timeoutMs?: number): Promise<void>;
  /** 标记正在退出（防止 close 事件拦截） */
  setQuittingFlag(): void;
  /** 重置退出标记（卸载失败回退时恢复，允许用户继续操作主窗口） */
  resetQuittingFlag?(): void;
  /** 停止后端健康检查定时器（防止杀 gateway 后触发断连遮罩） */
  stopBackendHealthMonitor(): void;
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
  if (!win) return;
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
  if (!win) return;
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
// app 退出后执行（见 handleUninstall 第 6 步收集 postQuitDirs）。不在主流程同步删除：
//   • userData 被 Electron 主进程持有句柄，运行时删不掉
//   • ~/.Crew 含 db/缓存，同步 fs.rmSync 阻塞主线程导致 UI 卡死。
// app 退出后所有句柄释放，延迟脚本 rm -rf 删得干净且不卡 UI。

// ─── 平台卸载器 ──────────────────────────────────────────────────────────────

/**
 * 启动平台特定的卸载程序。
 *
 * Windows: Inno Setup 生成的 unins000.exe（位于安装根目录）。
 *          process.execPath → .../Crew/crew-desktop/crew-desktop.exe
 *          unins000.exe 在 .../Crew/
 *
 * Linux:   使用固定的 /bin/sh 程序与结构化 argv，延迟执行 pkexec。
 *
 * @param postQuitDirs Electron 退出后需删除的目录（如 userData）。
 *   这些目录在 app 运行时被 Electron 持有句柄，无法在 cleanupUserData 中安全删除，
 *   故交给延迟脚本在 app 完全退出后清理。
 * @returns true 表示卸载器已成功启动，false 表示启动失败。
 */
function launchPlatformUninstaller(win: BrowserWindow | null, postQuitDirs: string[] = []): boolean {
  try {
    const normalizeForComparison = (target: string): string => (
      process.platform === 'win32' ? target.toLowerCase() : target
    );
    const isStrictChild = (target: string, parent: string): boolean => {
      const relative = path.relative(parent, target);
      return (
        relative !== ''
        && relative !== '..'
        && !relative.startsWith(`..${path.sep}`)
        && !path.isAbsolute(relative)
      );
    };
    const home = path.resolve(os.homedir());
    const appData = path.resolve(app.getPath('appData'));
    const crewHome = path.resolve(getCrewHome());
    const userData = path.resolve(app.getPath('userData'));
    if (
      path.basename(crewHome).toLowerCase() !== '.crew'
      || !isStrictChild(
        normalizeForComparison(crewHome),
        normalizeForComparison(home),
      )
      || !isStrictChild(
        normalizeForComparison(userData),
        normalizeForComparison(appData),
      )
    ) {
      throw new Error('application data directories are outside their owned roots');
    }
    const approved = [crewHome, userData];
    if (postQuitDirs.length > approved.length) {
      throw new Error('too many post-quit deletion targets');
    }
    const targets = postQuitDirs.map((target) => {
      const resolved = path.resolve(target);
      const equalsApproved = approved.some((candidate) => (
        normalizeForComparison(candidate) === normalizeForComparison(resolved)
      ));
      if (
        !path.isAbsolute(target)
        || target.includes('\0')
        || resolved === path.parse(resolved).root
        || !equalsApproved
      ) {
        throw new Error('post-quit deletion target is outside the approved set');
      }
      const info = fs.lstatSync(resolved);
      if (info.isSymbolicLink() || !info.isDirectory()) {
        throw new Error('post-quit deletion target is not a real directory');
      }
      return resolved;
    });
    if (process.platform === 'win32') {
      return launchWindowsUninstaller(win, targets);
    }
    if (process.platform === 'linux') {
      launchLinuxUninstaller(targets);
      return true;
    }
    if (process.platform === 'darwin') {
      launchMacOSUninstaller(targets);
      return true;
    }
  } catch (error) {
    console.error('[uninstall] 安全卸载启动失败:', error);
    showUninstallError(
      win,
      '卸载失败',
      '无法安全启动卸载程序',
      '卸载目标或系统卸载工具未通过安全校验。',
    );
  }
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
function launchWindowsUninstaller(win: BrowserWindow | null, postQuitDirs: string[] = []): boolean {
  const appDir = path.dirname(path.dirname(process.execPath));
  const uninstaller = path.join(appDir, 'unins000.exe');

  let uninstallerInfo: fs.Stats;
  try {
    uninstallerInfo = fs.lstatSync(uninstaller);
  } catch {
    showUninstallError(
      win,
      '卸载失败',
      '找不到卸载程序',
      `预期路径：${uninstaller}\n请通过系统「控制面板」或「设置 → 应用」卸载。`,
    );
    return false;
  }
  if (uninstallerInfo.isSymbolicLink() || !uninstallerInfo.isFile()) {
    showUninstallError(win, '卸载失败', '卸载程序不可信', '卸载程序必须是安装目录中的普通文件。');
    return false;
  }

  try {
    const powershell = path.join(
      'C:\\Windows',
      'System32',
      'WindowsPowerShell',
      'v1.0',
      'powershell.exe',
    );
    const powershellInfo = fs.lstatSync(powershell);
    if (powershellInfo.isSymbolicLink() || !powershellInfo.isFile()) {
      throw new Error('trusted PowerShell executable is unavailable');
    }
    const payload = Buffer.from(
      JSON.stringify({ uninstaller, targets: postQuitDirs }),
      'utf8',
    ).toString('base64');
    const script = [
      "$ErrorActionPreference = 'Stop'",
      "$json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:ACE_UNINSTALL_PAYLOAD_B64))",
      '$payload = ConvertFrom-Json -InputObject $json',
      'Remove-Item Env:ACE_UNINSTALL_PAYLOAD_B64 -ErrorAction SilentlyContinue',
      'Start-Sleep -Seconds 3',
      'foreach ($target in @($payload.targets)) { if (-not [IO.Path]::IsPathFullyQualified([string]$target)) { throw "invalid deletion target" }; $item = Get-Item -LiteralPath ([string]$target) -Force -ErrorAction SilentlyContinue; if ($null -ne $item -and (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not $item.PSIsContainer)) { throw "invalid deletion target type" }; if ($null -ne $item) { Remove-Item -LiteralPath ([string]$target) -Recurse -Force -ErrorAction Stop } }',
      '$process = Start-Process -FilePath ([string]$payload.uninstaller) -ArgumentList @("/SILENT") -PassThru',
      'if ($null -eq $process) { throw "failed to launch uninstaller" }',
    ].join('; ');
    spawn(
      powershell,
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-EncodedCommand',
        Buffer.from(script, 'utf16le').toString('base64'),
      ],
      hardenedChildProcessOptions(
        {
          detached: true,
          stdio: 'ignore',
          windowsHide: true,
        },
        { ACE_UNINSTALL_PAYLOAD_B64: payload },
      ),
    ).unref();
    return true;
  } catch (err) {
    console.error('[uninstall] 启动延迟卸载进程失败:', err);
    showUninstallError(
      win,
      '卸载失败',
      '无法安全启动卸载程序',
      '请通过系统「设置 → 应用」卸载 Crew。',
    );
    return false;
  }
}

/**
 * Linux: 创建临时 shell 脚本，延迟 3 秒后执行 pkexec apt remove -y crew-desktop。
 * 延迟是为了确保 Electron 完全退出，避免 dpkg 锁冲突。
 * 脚本最后自删除（rm -f）。
 */
function launchLinuxUninstaller(postQuitDirs: string[] = []): void {
  const script = [
    '/bin/sleep 3',
    'for target in "$@"; do /bin/rm -rf -- "$target"; done',
    'exec /usr/bin/pkexec /usr/bin/apt-get remove -y -- crew-desktop',
  ].join('\n');
  spawn(
    '/bin/sh',
    ['-c', script, 'crew-uninstall', ...postQuitDirs],
    hardenedChildProcessOptions({ detached: true, stdio: 'ignore' }),
  ).unref();
}

/**
 * macOS: 创建临时 shell 脚本，延迟 3 秒后删除 .app bundle。
 * macOS 无系统级卸载机制，DMG 安装的应用只需删除 .app 目录即可。
 * 脚本最后自删除。
 */
function launchMacOSUninstaller(postQuitDirs: string[] = []): void {
  // macOS .app 结构: Crew.app/Contents/MacOS/crew-desktop (process.execPath)
  const executable = path.resolve(app.getPath('exe'));
  const appDir = path.dirname(path.dirname(path.dirname(executable)));
  if (
    path.extname(appDir).toLowerCase() !== '.app'
    || !executable.startsWith(`${path.join(appDir, 'Contents', 'MacOS')}${path.sep}`)
  ) {
    throw new Error('macOS executable is not inside the expected app bundle');
  }
  const script = [
    '/bin/sleep 3',
    'for target in "$@"; do /bin/rm -rf -- "$target"; done',
  ].join('\n');
  spawn(
    '/bin/bash',
    ['-c', script, 'crew-uninstall', ...postQuitDirs, appDir],
    hardenedChildProcessOptions({ detached: true, stdio: 'ignore' }),
  ).unref();
}

// ─── 主入口 ─────────────────────────────────────────────────────────────────

/**
 * 托盘「卸载 Crew」完整流程入口。
 *
 * 前置条件：setUninstallDeps() 已被调用。
 */
export async function handleUninstall(): Promise<void> {
  if (!deps) {
    console.error('[uninstall] 依赖未注入，请先调用 setUninstallDeps()');
    return;
  }

  const win = deps.getMainWindow();
  if (!win) {
    console.error('[uninstall] 主窗口不存在，无法执行卸载');
    return;
  }

  // ── 第 1 步：确认卸载 ─────────────────────────────────────────────────
  if (!confirmUninstall(win)) return;

  // ── 第 2 步：确认是否保留用户数据 ─────────────────────────────────────
  const keepUserData = askKeepUserData(win);
  console.log(`[uninstall] 用户选择: ${keepUserData ? '保留用户数据' : '删除所有数据'}`);

  // ── 标记退出，防止 close 事件拦截 ─────────────────────────────────────
  deps.setQuittingFlag();

  // ── 停止健康检查 + 隐藏断连遮罩（必须在杀 gateway 之前执行）─────────
  // 健康检查每 3s 轮询 /api/health，一旦 gateway 被杀就会推送
  // backend:status { connected: false }，导致渲染进程弹出
  // "智能体运行环境准备中，请稍等" 全局遮罩，阻断卸载流程。
  deps.stopBackendHealthMonitor();
  deps.suppressBackendOverlay();

  // ── 第 3 步：停止 gateway 子进程，释放 ~/.Crew 文件句柄 ────────
  console.log('[uninstall] 停止 managed gateway...');
  await deps.stopManagedGateway();

  if (deps.cleanupSecurity && !await deps.cleanupSecurity()) {
    showUninstallError(
      win,
      '安全组件清理失败',
      '未继续卸载 Crew',
      'Windows UAC 被拒绝或安全组件清理失败。请重新启动应用后再试，避免遗留技术账号和网络规则。',
    );
    deps.resetQuittingFlag?.();
    return;
  }

  // ── 第 6 步：按用户选择清理用户数据 ───────────────────────────────────
  // 用户数据目录（~/.Crew + userData）都交给延迟脚本在 app 退出后删除：
  //   • userData 被 Electron 主进程持有句柄，运行时删不掉
  //   • ~/.Crew 虽 gateway 已停，但同步 fs.rmSync 删大目录（含 db/缓存）会
  //     阻塞主线程数秒，导致进度窗口动画卡死、app.quit() 迟迟不执行（用户感知"卡住"）。
  // 改为 app 退出后由延迟脚本 rm -rf，此时所有句柄释放，删得干净且不卡 UI。
  const postQuitDirs: string[] = [];
  if (!keepUserData) {
    postQuitDirs.push(getCrewHome());
    postQuitDirs.push(app.getPath('userData'));
  } else {
    console.log('[uninstall] 用户选择保留数据，跳过清理');
  }

  // ── 第 7 步：启动平台卸载器 ──────────────────────────────────────────
  const launched = launchPlatformUninstaller(win, postQuitDirs);
  if (!launched) {
    // 卸载器启动失败：恢复主窗口、重置退出标记，让用户可重试
    try { win.show(); } catch { /* ignore */ }
    deps.resetQuittingFlag?.();
    if (process.platform !== 'win32' && process.platform !== 'linux' && process.platform !== 'darwin') {
      showUnsupportedPlatform(win);
    }
    // Windows 场景下 launchWindowsUninstaller 已弹出错误提示
    // 不退出应用，让用户手动通过系统卸载
    return;
  }

  // ── 第 8 步：退出 Electron ───────────────────────────────────────────
  console.log('[uninstall] 卸载流程完成，退出 Electron');
  app.quit();
}
