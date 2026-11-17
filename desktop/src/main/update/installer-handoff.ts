import { spawn, type ChildProcess, type SpawnOptions } from 'node:child_process';
import { chmod } from 'node:fs/promises';
import path from 'node:path';

const HANDOFF_TIMEOUT_MS = 3_000;

type SpawnCommand = (command: string, args: string[], options: SpawnOptions) => ChildProcess;

interface InstallerHandoffOptions {
  openPath: (absolutePath: string) => Promise<string>;
  platform?: NodeJS.Platform;
  spawn?: SpawnCommand;
  getuid?: () => number;
  chmod?: typeof chmod;
  timeoutMs?: number;
}

export type InstallerHandoff =
  | { kind: 'installer-started'; restartScheduled?: boolean }
  | { kind: 'package-opened' };

// The helper observes only the installer and Desktop passed to it. If either
// still owns its process slot at its deadline, leave restarting to the user.
const LINUX_RESTART_SCRIPT = [
  'n=0',
  'while kill -0 "$1" 2>/dev/null; do [ "$n" -lt 120 ] || exit 1; sleep 0.5; n=$((n+1)); done',
  'n=0',
  'while kill -0 "$2" 2>/dev/null; do [ "$n" -lt 40 ] || exit 1; sleep 0.5; n=$((n+1)); done',
  'exec crew-desktop',
].join('\n');

export function launchDetachedInstaller(
  command: string,
  args: string[],
  {
    spawn: spawnCommand = spawn,
    timeoutMs = HANDOFF_TIMEOUT_MS,
    windowsHide = false,
  }: { spawn?: SpawnCommand; timeoutMs?: number; windowsHide?: boolean } = {},
): Promise<ChildProcess> {
  return new Promise((resolve, reject) => {
    const child = spawnCommand(command, args, {
      detached: true,
      stdio: 'ignore',
      ...(windowsHide ? { windowsHide: true } : {}),
    });
    let timedOut = false;
    const cancelUnconfirmedChild = () => {
      if (child.pid === undefined || child.killed) return;
      try {
        child.kill();
      } catch {
        // The failure to acknowledge still controls the handoff outcome.
      }
    };
    const timer = setTimeout(() => {
      timedOut = true;
      cancelUnconfirmedChild();
      reject(new Error('等待安装程序启动超时'));
    }, timeoutMs);
    child.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once('spawn', () => {
      clearTimeout(timer);
      if (timedOut) {
        cancelUnconfirmedChild();
        return;
      }
      child.unref();
      resolve(child);
    });
  });
}

async function openPackage(
  targetPath: string,
  openPath: InstallerHandoffOptions['openPath'],
  timeoutMs: number,
): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const detail = await Promise.race([
      openPath(targetPath),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error('等待系统打开安装包超时')), timeoutMs);
      }),
    ]);
    if (detail) throw new Error(detail);
  } finally {
    clearTimeout(timer);
  }
}

/** Hand off a validated update package after Desktop's resource owners have drained. */
export async function launchUpdateInstaller(
  packagePath: string,
  {
    openPath,
    platform = process.platform,
    spawn: spawnCommand = spawn,
    getuid = process.getuid,
    chmod: makeReadable = chmod,
    timeoutMs = HANDOFF_TIMEOUT_MS,
  }: InstallerHandoffOptions,
): Promise<InstallerHandoff> {
  const targetPath = path.resolve(packagePath);
  const extension = path.extname(targetPath).toLowerCase();
  const launchOptions = { spawn: spawnCommand, timeoutMs };
  if (platform === 'win32' && extension === '.exe') {
    await launchDetachedInstaller(targetPath, ['/SILENT', '/NORESTART'], { ...launchOptions, windowsHide: true });
    return { kind: 'installer-started' };
  }

  if (platform === 'linux' && extension === '.deb') {
    await makeReadable(targetPath, 0o644);
    const needsElevation = getuid !== undefined && getuid() !== 0;
    const command = needsElevation ? 'pkexec' : 'dpkg';
    const args = needsElevation ? ['dpkg', '-i', targetPath] : ['-i', targetPath];
    const installer = await launchDetachedInstaller(command, args, launchOptions);
    if (installer.pid === undefined) return { kind: 'installer-started', restartScheduled: false };
    try {
      await launchDetachedInstaller('sh', [
        '-c', LINUX_RESTART_SCRIPT, 'crew-restarter', String(installer.pid), String(process.pid),
      ], launchOptions);
      return { kind: 'installer-started', restartScheduled: true };
    } catch (error) {
      console.warn('[update-install] Linux restart helper could not be started:', error);
      return { kind: 'installer-started', restartScheduled: false };
    }
  }

  if (platform === 'darwin' && extension === '.dmg') {
    await launchDetachedInstaller('open', [targetPath], launchOptions);
  } else {
    await openPackage(targetPath, openPath, timeoutMs);
  }
  return { kind: 'package-opened' };
}
