import { spawn, spawnSync, type ChildProcess } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

export type SecuritySetupAction = 'install' | 'repair' | 'uninstall';
export type SecuritySetupResult = {
  ok: boolean;
  exitCode: number | null;
  detail?: string;
  code?: 'uac_disabled' | 'uac_restart_required';
};
export type UacEnableResult = SecuritySetupResult & { restartRequired?: boolean };

const UAC_REGISTRY_KEY = 'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System';
const POWERSHELL_UAC_REGISTRY_PATH = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System';
let uacRestartRequired = false;

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
  const runtimeArg = action === 'uninstall' ? '--windows-uninstall' : '--windows-setup';
  const script = [
    `$process = Start-Process -FilePath ${quotePowerShell(runtimePath)}`,
    `-ArgumentList @(${quotePowerShell(runtimeArg)}, ${quotePowerShell(`"${stateDir}"`)})`,
    '-Verb RunAs -Wait -PassThru;',
    'exit $process.ExitCode',
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

async function runElevatedCommand(
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
    child.once('exit', (code) => {
      const detail = output.join('').trim();
      finish({
        ok: code === 0,
        exitCode: code,
        ...(detail ? { detail } : {}),
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
  const result = await runElevatedCommand(buildElevatedUacEnable(), spawnProcess);
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
  // Create under the interactive user's profile before elevation so inherited ACLs remain
  // readable by that user even when UAC uses an over-the-shoulder administrator credential.
  fs.mkdirSync(stateDir, { recursive: true });
  return runElevatedCommand(buildElevatedSecuritySetup(runtimePath, stateDir, action), spawnProcess);
}
