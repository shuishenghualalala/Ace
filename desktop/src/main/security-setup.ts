import { spawn, spawnSync, type ChildProcess } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

export type SecuritySetupAction = 'install' | 'repair' | 'uninstall';
export type SecuritySetupResult = {
  ok: boolean;
  exitCode: number | null;
  detail?: string;
  code?: 'uac_cancelled' | 'uac_disabled' | 'uac_restart_required' | 'windows_setup_owner_mismatch' | 'windows_setup_retry_deferred' | 'windows_sandbox_account_repair_required';
};
export type UacEnableResult = SecuritySetupResult & { restartRequired?: boolean };

const UAC_REGISTRY_KEY = 'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System';
const POWERSHELL_UAC_REGISTRY_PATH = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System';
const ERROR_CANCELLED = 1223;
// Exit status from the bundled native helper's --windows-setup command.
const SETUP_OWNER_MISMATCH = 4;
const SETUP_ACCOUNT_REPAIR_REQUIRED = 5;
const DEFERRED_CONSENT_FILE = 'windows-setup-consent-deferred';
const OWNER_MISMATCH_FILE = 'windows-setup-owner-mismatch';
const SETUP_FAILURE_FILE = 'windows-setup-failure-deferred';
let uacRestartRequired = false;

export function securityRuntimePlatformKey(platform: string, arch: string): string | undefined {
  const platformName = platform.trim().toLowerCase();
  const archName = arch.trim().toLowerCase();
  const targetPlatform = ({ windows: 'win32', macos: 'darwin', linux2: 'linux' } as Record<string, string>)[platformName] ?? platformName;
  const targetArch = ({ amd64: 'x64', x86_64: 'x64', aarch64: 'arm64' } as Record<string, string>)[archName] ?? archName;
  if (['win32', 'linux', 'darwin'].includes(targetPlatform) && ['x64', 'arm64'].includes(targetArch)) {
    return `${targetPlatform}-${targetArch}`;
  }
  return undefined;
}

export function windowsPackagedGatewaySecurityEnv(
  inherited: NodeJS.ProcessEnv,
  selected: Record<string, string>,
  setupDirectory: string | undefined,
): NodeJS.ProcessEnv {
  const owned = new Set([
    'ACE_SECURITY_RUNTIME',
    'ACE_SECURITY_STATE_DIR',
    'ACE_STRICT_SECURITY',
    'ACE_SECURITY_RUNTIME_BUNDLED_ONLY',
    'ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR',
  ]);
  const base = Object.fromEntries(
    Object.entries(inherited).filter(([key]) => !owned.has(key.toUpperCase())),
  );
  return {
    ...base,
    ...selected,
    ACE_SECURITY_RUNTIME_BUNDLED_ONLY: '1',
    ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR: selected.ACE_SECURITY_RUNTIME ? setupDirectory : undefined,
  };
}

function quotePowerShell(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

function powershellPath(): string {
  return path.join(
    process.env['SystemRoot'] || 'C:\\Windows',
    'System32',
    'WindowsPowerShell',
    'v1.0',
    'powershell.exe',
  );
}

function encodePowerShell(script: string): string {
  return Buffer.from(script, 'utf16le').toString('base64');
}

export function buildElevatedSecuritySetup(
  runtimePath: string,
  stateDir: string,
  action: SecuritySetupAction,
): { executable: string; argv: string[] } {
  // 该模块只服务 Windows 宿主，校验必须与宿主平台解耦：path.win32 无论运行在哪个
  // 平台（如 macOS 上跑单元测试）都按 Windows 规则判断绝对路径与 basename。
  if (!path.win32.isAbsolute(runtimePath) || path.win32.basename(runtimePath) !== 'ace-security-runtime.exe') {
    throw new Error('invalid packaged security runtime path');
  }
  if (!path.win32.isAbsolute(stateDir)) throw new Error('security state directory must be absolute');
  const runtimeArg = action === 'uninstall' ? '--windows-uninstall'
    : action === 'repair' ? '--windows-repair' : '--windows-setup';
  const originalUser = action === 'uninstall'
    ? ''
    : '$ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;';
  const ownerArg = action === 'uninstall' ? '' : ", '--expected-owner-sid', $ownerSid";
  const script = [
    originalUser,
    '$start = [System.Diagnostics.ProcessStartInfo]::new();',
    `$start.FileName = ${quotePowerShell(runtimePath)};`,
    `$start.Arguments = @(${quotePowerShell(runtimeArg)}, ${quotePowerShell(`"${stateDir}"`)}${ownerArg}) -join ' ';`,
    '$start.UseShellExecute = $true;',
    "$start.Verb = 'runas';",
    // PowerShell's Start-Process discards the native error code from ShellExecute.
    'try {',
    '$process = [System.Diagnostics.Process]::Start($start);',
    'try { $process.WaitForExit(); exit $process.ExitCode } finally { $process.Dispose() }',
    '} catch {',
    'for ($cause = $_.Exception; $null -ne $cause; $cause = $cause.InnerException) {',
    `if ($cause -is [System.ComponentModel.Win32Exception] -and $cause.NativeErrorCode -eq ${ERROR_CANCELLED}) { exit ${ERROR_CANCELLED} }`,
    '}',
    'throw',
    '}',
  ].join(' ');
  return {
    executable: powershellPath(),
    argv: ['-NoProfile', '-NonInteractive', '-EncodedCommand', encodePowerShell(script)],
  };
}

export function buildElevatedUacEnable(): { executable: string; argv: string[] } {
  const executable = powershellPath();
  const innerScript = [
    '$ErrorActionPreference = \'Stop\'',
    `$path = ${quotePowerShell(POWERSHELL_UAC_REGISTRY_PATH)}`,
    "New-ItemProperty -Path $path -Name 'EnableLUA' -PropertyType DWord -Value 1 -Force | Out-Null",
  ].join('; ');
  const script = [
    `$process = Start-Process -FilePath ${quotePowerShell(executable)}`,
    `-ArgumentList @(${quotePowerShell('-NoProfile')}, ${quotePowerShell('-NonInteractive')}, ${quotePowerShell('-EncodedCommand')}, ${quotePowerShell(encodePowerShell(innerScript))})`,
    '-Verb RunAs -Wait -PassThru;',
    'exit $process.ExitCode',
  ].join(' ');
  return {
    executable,
    argv: ['-NoProfile', '-NonInteractive', '-EncodedCommand', encodePowerShell(script)],
  };
}

export function parseEnableLuaOutput(output: string): boolean | null {
  const match = output.match(/^\s*EnableLUA\s+REG_DWORD\s+0x([0-9a-f]+)\s*$/im);
  if (!match) return null;
  return Number.parseInt(match[1], 16) !== 0;
}

function readWindowsUacEnabled(): boolean | null {
  const result = spawnSync('reg.exe', ['query', UAC_REGISTRY_KEY, '/v', 'EnableLUA'], {
    encoding: 'utf8',
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'ignore'],
  });
  if (result.error || result.status !== 0) return null;
  return parseEnableLuaOutput(String(result.stdout ?? ''));
}

export function getWindowsUacStatus(): { enabled: boolean | null; restartRequired?: boolean; detail?: string } {
  const enabled = readWindowsUacEnabled();
  if (uacRestartRequired) return { enabled, restartRequired: true };
  return enabled === null
    ? { enabled: null, detail: '无法读取系统安全设置状态' }
    : { enabled };
}

async function runSetupCommand(
  command: { executable: string; argv: string[] },
  spawnProcess: typeof spawn,
): Promise<SecuritySetupResult> {
  return new Promise((resolve) => {
    const child: ChildProcess = spawnProcess(command.executable, command.argv, {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const output: string[] = [];
    let settled = false;
    const finish = (result: SecuritySetupResult): void => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    child.stdout?.on('data', (chunk) => output.push(String(chunk)));
    child.stderr?.on('data', (chunk) => output.push(String(chunk)));
    child.once('error', (error) => finish({
      ok: false,
      exitCode: null,
      detail: `无法启动安全防护安装程序：${error.message}`,
    }));
    child.once('close', (code) => {
      const detail = output.join('').trim();
      finish({
        ok: code === 0,
        exitCode: code,
        ...(detail ? { detail } : code === 0 ? {} : { detail: `安装程序退出码 ${code ?? '未知'}` }),
      });
    });
  });
}

export async function runElevatedUacEnable(
  spawnProcess: typeof spawn = spawn,
): Promise<UacEnableResult> {
  if (uacRestartRequired) {
    return { ok: true, exitCode: 0, restartRequired: true };
  }
  if (readWindowsUacEnabled() === true) {
    return { ok: true, exitCode: 0, restartRequired: false };
  }
  const result = await runSetupCommand(buildElevatedUacEnable(), spawnProcess);
  if (!result.ok) return result;
  uacRestartRequired = true;
  return { ...result, restartRequired: true };
}

export async function runElevatedSecuritySetup(
  runtimePath: string,
  stateDir: string,
  action: SecuritySetupAction,
  spawnProcess: typeof spawn = spawn,
): Promise<SecuritySetupResult> {
  if (!fs.existsSync(runtimePath)) return { ok: false, exitCode: null, detail: 'security runtime not found' };
  if (process.platform === 'win32' && action !== 'uninstall') {
    if (uacRestartRequired) {
      return {
        ok: false,
        exitCode: null,
        code: 'uac_restart_required',
        detail: 'UAC 已启用，但需要重启电脑后生效',
      };
    }
    if (readWindowsUacEnabled() === false) {
      return {
        ok: false,
        exitCode: null,
        code: 'uac_disabled',
        detail: '系统安全设置未启用',
      };
    }
  }
  // Native install and repair verify that UAC kept the original user's Windows identity.
  fs.mkdirSync(stateDir, { recursive: true });
  const result = await runSetupCommand(buildElevatedSecuritySetup(runtimePath, stateDir, action), spawnProcess);
  if (result.exitCode === ERROR_CANCELLED) return cancelledSetup();
  if (action !== 'uninstall' && result.exitCode === SETUP_OWNER_MISMATCH) return ownerMismatchSetup();
  return result;
}

function ownerMismatchSetup(): SecuritySetupResult {
  return {
    ok: false,
    exitCode: SETUP_OWNER_MISMATCH,
    code: 'windows_setup_owner_mismatch',
    detail: 'Windows 沙箱须由启动 Ace 的同一 Windows 账号授权。当前安装需要有管理员权限的 Windows 账号完成设置。',
  };
}

function cancelledSetup(): SecuritySetupResult {
  return {
    ok: false,
    exitCode: ERROR_CANCELLED,
    code: 'uac_cancelled',
    detail: '已取消 Windows 管理员确认',
  };
}

function saveSetupDeferral(marker: string, deferred: boolean): void {
  try {
    if (deferred) {
      fs.mkdirSync(path.dirname(marker), { recursive: true });
      fs.writeFileSync(marker, '', { flag: 'wx' });
    }
    else fs.rmSync(marker, { force: true });
  } catch (error) {
    const { code } = error as NodeJS.ErrnoException;
    if (!(deferred && code === 'EEXIST')) console.warn('[security] Windows setup retry state:', code);
  }
}

function deferredSetupFailure(): SecuritySetupResult {
  return {
    ok: false,
    exitCode: 1,
    code: 'windows_setup_retry_deferred',
    detail: 'Windows 防护上次自动准备未完成。可在「设置 → 安全」查看状态并显式重试。',
  };
}

export async function provisionInstalledWindowsSandbox(
  runtimePath: string,
  stateDir: string,
  spawnProcess: typeof spawn = spawn,
  { automatic = false } = {},
): Promise<SecuritySetupResult> {
  const verify = () => runSetupCommand({
    executable: runtimePath,
    argv: ['--windows-check-setup', stateDir],
  }, spawnProcess);
  const consent = path.join(stateDir, DEFERRED_CONSENT_FILE);
  const ownerMismatch = path.join(stateDir, OWNER_MISMATCH_FILE);
  const setupFailure = path.join(stateDir, SETUP_FAILURE_FILE);
  const clearDeferrals = () => {
    for (const marker of [consent, ownerMismatch, setupFailure]) {
      if (fs.existsSync(marker)) saveSetupDeferral(marker, false);
    }
  };
  const before = await verify();
  // The native check remains authoritative even after automatic elevation was deferred.
  if (before.ok) {
    clearDeferrals();
    return before;
  }
  if (before.exitCode === SETUP_ACCOUNT_REPAIR_REQUIRED) {
    return {
      ...before,
      code: 'windows_sandbox_account_repair_required',
      detail: [
        before.detail,
        '自动准备没有更改 Windows 账号。可在「设置 → 安全」显式请求管理员修复；账号已删除时会按原身份名重建，并在网络规则核验后启用。',
      ].filter(Boolean).join('\n'),
    };
  }
  if (before.exitCode !== 1) return before;
  if (automatic && fs.existsSync(ownerMismatch)) return ownerMismatchSetup();
  if (automatic && fs.existsSync(consent)) return cancelledSetup();
  if (automatic && fs.existsSync(setupFailure)) return deferredSetupFailure();
  const setup = await runElevatedSecuritySetup(runtimePath, stateDir, 'install', spawnProcess);
  if (setup.code === 'uac_cancelled') saveSetupDeferral(consent, true);
  if (setup.code === 'windows_setup_owner_mismatch') saveSetupDeferral(ownerMismatch, true);
  if (!setup.ok && !setup.code) saveSetupDeferral(setupFailure, true);
  if (!setup.ok) return setup;
  const after = await verify();
  if (after.ok) clearDeferrals();
  else if (!after.code) saveSetupDeferral(setupFailure, true);
  return after;
}

/** Publish the automatic operation to this Desktop's owned Gateway processes. */
export function publishWindowsSandboxSetup(
  directory: string,
  setup: Promise<SecuritySetupResult>,
  onError: () => void,
): () => void {
  try {
    fs.mkdirSync(directory);
  } catch {
    onError();
    return () => {};
  }
  const publish = (ok: boolean): void => {
    try {
      fs.writeFileSync(path.join(directory, ok ? 'ready' : 'failed'), '', { flag: 'wx' });
    } catch {
      onError();
    }
  };
  void setup.then((result) => publish(result.ok), () => publish(false));

  return () => {
    try {
      for (const marker of ['ready', 'failed']) fs.rmSync(path.join(directory, marker), { force: true });
      fs.rmdirSync(directory);
    } catch {
      onError();
    }
  };
}

interface WindowsSandboxStartup {
  platform: string;
  visible: boolean;
  ci: boolean;
}

interface WindowsSandboxLocation {
  runtimePath: string;
  stateDir: string;
}

/** Share the host's one-time check and native consent flow across startup and Settings. */
export function createWindowsSandboxSetupCoordinator(
  provision = provisionInstalledWindowsSandbox,
  elevate = runElevatedSecuritySetup,
) {
  let automaticAttempted = false;
  let active: {
    action: SecuritySetupAction;
    runtimePath: string;
    stateDir: string;
    promise: Promise<SecuritySetupResult>;
  } | undefined;

  function run(
    action: SecuritySetupAction, runtimePath: string, stateDir: string, automatic = false,
  ): Promise<SecuritySetupResult> {
    if (active) {
      if (action === active.action && runtimePath === active.runtimePath && stateDir === active.stateDir) {
        return active.promise;
      }
      return Promise.resolve({ ok: false, exitCode: null, detail: '另一个 Windows 防护操作正在进行' });
    }
    const promise = Promise.resolve().then(() => (
      action === 'install'
        ? provision(runtimePath, stateDir, undefined, { automatic })
        : elevate(runtimePath, stateDir, action)
    )).finally(() => { active = undefined; });
    active = { action, runtimePath, stateDir, promise };
    return promise;
  }

  /** Prepare on the first visible window; source and packaged launches share the flow. */
  function startAutomatically(
    launch: WindowsSandboxStartup,
    resolveLocation: () => WindowsSandboxLocation | undefined,
  ): Promise<SecuritySetupResult> | undefined {
    if (automaticAttempted || launch.platform !== 'win32' || !launch.visible || launch.ci) return;
    automaticAttempted = true;
    const location = resolveLocation();
    return location ? run('install', location.runtimePath, location.stateDir, true) : undefined;
  }

  return { run, startAutomatically };
}
