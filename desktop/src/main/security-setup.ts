import { spawn, type ChildProcess } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

export type SecuritySetupAction = 'install' | 'repair' | 'uninstall';

function quotePowerShell(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

export function buildElevatedSecuritySetup(
  runtimePath: string,
  stateDir: string,
  action: SecuritySetupAction,
): { executable: string; argv: string[] } {
  if (!path.isAbsolute(runtimePath) || path.basename(runtimePath) !== 'ace-security-runtime.exe') {
    throw new Error('invalid packaged security runtime path');
  }
  if (!path.isAbsolute(stateDir)) throw new Error('security state directory must be absolute');
  const runtimeArg = action === 'uninstall' ? '--windows-uninstall' : '--windows-setup';
  const script = [
    `$process = Start-Process -FilePath ${quotePowerShell(runtimePath)}`,
    `-ArgumentList @(${quotePowerShell(runtimeArg)}, ${quotePowerShell(`"${stateDir}"`)})`,
    '-Verb RunAs -Wait -PassThru',
    'exit $process.ExitCode',
  ].join(' ');
  return {
    executable: path.join(process.env['SystemRoot'] || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    argv: ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
  };
}

export async function runElevatedSecuritySetup(
  runtimePath: string,
  stateDir: string,
  action: SecuritySetupAction,
  spawnProcess: typeof spawn = spawn,
): Promise<{ ok: boolean; exitCode: number | null }> {
  if (!fs.existsSync(runtimePath)) return { ok: false, exitCode: null };
  // Create under the interactive user's profile before elevation so inherited ACLs remain
  // readable by that user even when UAC uses an over-the-shoulder administrator credential.
  fs.mkdirSync(stateDir, { recursive: true });
  const command = buildElevatedSecuritySetup(runtimePath, stateDir, action);
  return new Promise((resolve) => {
    const child: ChildProcess = spawnProcess(command.executable, command.argv, {
      windowsHide: true,
      stdio: 'ignore',
    });
    child.once('error', () => resolve({ ok: false, exitCode: null }));
    child.once('exit', (code) => resolve({ ok: code === 0, exitCode: code }));
  });
}
