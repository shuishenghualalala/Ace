import * as fs from 'fs';
import * as path from 'path';
import {
  spawn,
  type ChildProcess,
  type SpawnOptions,
} from 'child_process';
import { hardenedChildProcessOptions } from '../process-environment';
import { openVerifiedUpdateArtifact } from './update-integrity';
import type { DownloadedUpdateRecord } from '../../shared/types';

const SPAWN_CONFIRM_TIMEOUT_MS = 5_000;
const INSTALL_COMPLETION_TIMEOUT_MS = 15 * 60 * 1000;
const DMG_ATTACH_TIMEOUT_MS = 2 * 60 * 1000;
const WINDOWS_LAUNCH_HELPER_TIMEOUT_MS = 30 * 1000;
const WINDOWS_INSTALLER_STARTED_SENTINEL = 'ACE_UPDATE_INSTALLER_STARTED';

export const TRUSTED_UPDATE_HELPERS = Object.freeze({
  windows: Object.freeze({
    powershell: path.win32.join(
      'C:\\Windows',
      'System32',
      'WindowsPowerShell',
      'v1.0',
      'powershell.exe',
    ),
  }),
  linux: Object.freeze({
    dpkg: '/usr/bin/dpkg',
    pkexec: '/usr/bin/pkexec',
    timeout: '/usr/bin/timeout',
  }),
  mac: Object.freeze({
    hdiutil: '/usr/bin/hdiutil',
  }),
});

export type UpdateInstallKind = 'windows-exe' | 'linux-deb' | 'mac-dmg';

export interface UpdateInstallPlan {
  kind: UpdateInstallKind;
  executable: string;
  packagePath: string;
  requiresCompletion: boolean;
  consumesRecord: boolean;
  helperPaths: readonly string[];
}

export interface UpdateInstallLaunchResult {
  message: string;
  consumesRecord: boolean;
}

export class UpdateInstallError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'UpdateInstallError';
    this.code = code;
  }
}

export function buildUpdateInstallPlan(
  packagePath: string,
  platform: string = process.platform,
  uid: number | null = typeof process.getuid === 'function' ? process.getuid() : null,
  architecture: string = process.arch,
): UpdateInstallPlan {
  const resolved = path.resolve(packagePath);
  if (resolved !== packagePath || path.basename(resolved) !== path.basename(packagePath)) {
    throw new UpdateInstallError(
      'invalid-package-path',
      '更新已阻止：安装包路径必须是规范绝对路径',
    );
  }
  const extension = path.extname(resolved).toLowerCase();
  if (platform === 'win32' && architecture === 'x64' && extension === '.exe') {
    return {
      kind: 'windows-exe',
      executable: TRUSTED_UPDATE_HELPERS.windows.powershell,
      packagePath: resolved,
      requiresCompletion: false,
      // Keep the verified record until the next process proves the installed
      // version advanced; a detached installer can still fail after spawn.
      consumesRecord: false,
      helperPaths: [TRUSTED_UPDATE_HELPERS.windows.powershell],
    };
  }
  if (platform === 'linux' && architecture === 'x64' && extension === '.deb') {
    const usePkexec = uid !== 0;
    return {
      kind: 'linux-deb',
      executable: usePkexec
        ? TRUSTED_UPDATE_HELPERS.linux.pkexec
        : TRUSTED_UPDATE_HELPERS.linux.timeout,
      packagePath: resolved,
      requiresCompletion: true,
      consumesRecord: true,
      helperPaths: usePkexec
        ? [
            TRUSTED_UPDATE_HELPERS.linux.pkexec,
            TRUSTED_UPDATE_HELPERS.linux.timeout,
            TRUSTED_UPDATE_HELPERS.linux.dpkg,
          ]
        : [
            TRUSTED_UPDATE_HELPERS.linux.timeout,
            TRUSTED_UPDATE_HELPERS.linux.dpkg,
          ],
    };
  }
  if (platform === 'darwin' && architecture === 'arm64' && extension === '.dmg') {
    return {
      kind: 'mac-dmg',
      executable: TRUSTED_UPDATE_HELPERS.mac.hdiutil,
      packagePath: resolved,
      requiresCompletion: true,
      consumesRecord: false,
      helperPaths: [TRUSTED_UPDATE_HELPERS.mac.hdiutil],
    };
  }
  throw new UpdateInstallError(
    'unsupported-platform-package',
    '更新已阻止：当前平台与安装包类型不匹配',
  );
}

function noFollowFlag(): number {
  if (process.platform === 'win32') return 0;
  return (fs.constants as unknown as Record<string, number>).O_NOFOLLOW ?? 0;
}

export function assertTrustedSystemHelper(helperPath: string): void {
  const allowlisted = new Set<string>([
    TRUSTED_UPDATE_HELPERS.windows.powershell,
    TRUSTED_UPDATE_HELPERS.linux.dpkg,
    TRUSTED_UPDATE_HELPERS.linux.pkexec,
    TRUSTED_UPDATE_HELPERS.linux.timeout,
    TRUSTED_UPDATE_HELPERS.mac.hdiutil,
  ]);
  if (!path.isAbsolute(helperPath) || !allowlisted.has(helperPath)) {
    throw new UpdateInstallError(
      'helper-not-absolute',
      '更新已阻止：系统安装工具路径不可信',
    );
  }
  const before = fs.lstatSync(helperPath);
  if (before.isSymbolicLink() || !before.isFile()) {
    throw new UpdateInstallError(
      'helper-not-regular',
      '更新已阻止：系统安装工具不是可信普通文件',
    );
  }
  if (
    process.platform !== 'win32'
    && (before.uid !== 0 || (before.mode & 0o022) !== 0 || (before.mode & 0o111) === 0)
  ) {
    throw new UpdateInstallError(
      'helper-metadata-untrusted',
      '更新已阻止：系统安装工具权限或所有者不可信',
    );
  }

  const descriptor = fs.openSync(
    helperPath,
    fs.constants.O_RDONLY | noFollowFlag(),
  );
  try {
    const opened = fs.fstatSync(descriptor);
    if (
      !opened.isFile()
      || opened.dev !== before.dev
      || opened.ino !== before.ino
      || opened.size !== before.size
      || opened.mtimeMs !== before.mtimeMs
      || opened.ctimeMs !== before.ctimeMs
    ) {
      throw new UpdateInstallError(
        'helper-identity-changed',
        '更新已阻止：系统安装工具在启动前发生变化',
      );
    }
  } finally {
    fs.closeSync(descriptor);
  }
}

function waitForSpawn(child: ChildProcess): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // The stable timeout rejection remains the caller-visible result.
      }
      reject(new UpdateInstallError(
        'spawn-timeout',
        '更新已阻止：安装程序启动确认超时',
      ));
    }, SPAWN_CONFIRM_TIMEOUT_MS);
    const onError = () => {
      clearTimeout(timer);
      reject(new UpdateInstallError(
        'spawn-failed',
        '更新已阻止：安装程序启动失败',
      ));
    };
    child.once('error', onError);
    child.once('spawn', () => {
      clearTimeout(timer);
      child.off('error', onError);
      resolve();
    });
  });
}

function waitForWindowsInstallerConfirmation(child: ChildProcess): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!child.stdout) {
      reject(new UpdateInstallError(
        'installer-confirmation-unavailable',
        '更新已阻止：安装程序启动确认通道不可用',
      ));
      return;
    }
    let output = '';
    let settled = false;
    const cleanup = () => {
      clearTimeout(timer);
      child.off('error', onError);
      child.off('exit', onExit);
      child.stdout?.removeAllListeners('data');
    };
    const fail = () => {
      if (settled) return;
      settled = true;
      cleanup();
      try { child.kill(); } catch { /* already dead */ }
      reject(new UpdateInstallError(
        'installer-confirmation-failed',
        '更新已阻止：安装程序未通过锁定启动确认',
      ));
    };
    const onError = () => fail();
    const onExit = () => fail();
    const timer = setTimeout(fail, WINDOWS_LAUNCH_HELPER_TIMEOUT_MS);
    child.once('error', onError);
    child.once('exit', onExit);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      output += chunk;
      if (output.length > 256) {
        fail();
        return;
      }
      if (output.split(/\r?\n/u).includes(WINDOWS_INSTALLER_STARTED_SENTINEL)) {
        if (settled) return;
        settled = true;
        cleanup();
        child.stdout?.destroy();
        resolve();
      }
    });
  });
}

function waitForSuccessfulExit(
  child: ChildProcess,
  timeoutMs: number,
): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return child.exitCode === 0 && child.signalCode === null
      ? Promise.resolve()
      : Promise.reject(new UpdateInstallError(
          'install-nonzero-exit',
          '更新已阻止：安装工具未成功完成',
        ));
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // The stable timeout rejection remains the caller-visible result.
      }
      reject(new UpdateInstallError(
        'install-timeout',
        '更新已阻止：安装工具运行超时',
      ));
    }, timeoutMs);
    child.once('error', () => {
      clearTimeout(timer);
      reject(new UpdateInstallError(
        'install-process-failed',
        '更新已阻止：安装工具运行失败',
      ));
    });
    child.once('exit', (code, signal) => {
      clearTimeout(timer);
      if (code === 0 && signal === null) {
        resolve();
      } else {
        reject(new UpdateInstallError(
          'install-nonzero-exit',
          '更新已阻止：安装工具未成功完成',
        ));
      }
    });
  });
}

function spawnVerifiedPlan(
  plan: UpdateInstallPlan,
  packageDescriptor: number,
  expectedPackageSha256: string,
): { child: ChildProcess; completionTimeoutMs: number | null } {
  if (plan.kind === 'windows-exe') {
    if (!/^[a-f0-9]{64}$/.test(expectedPackageSha256)) {
      throw new UpdateInstallError(
        'invalid-package-digest',
        '更新已阻止：安装包摘要无效',
      );
    }
    const payload = Buffer.from(JSON.stringify({
      packagePath: plan.packagePath,
      packageSha256: expectedPackageSha256,
    }), 'utf8').toString('base64');
    const script = [
      "$ErrorActionPreference = 'Stop'",
      "$json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:ACE_UPDATE_INSTALL_PAYLOAD_B64))",
      '$payload = ConvertFrom-Json -InputObject $json',
      'Remove-Item Env:ACE_UPDATE_INSTALL_PAYLOAD_B64 -ErrorAction SilentlyContinue',
      '$stream = [IO.File]::Open([string]$payload.packagePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)',
      `try { $sha = [Security.Cryptography.SHA256]::Create(); try { $actual = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }; if ($actual -cne [string]$payload.packageSha256) { throw "update package digest changed" }; $process = Start-Process -FilePath ([string]$payload.packagePath) -ArgumentList @("/SILENT", "/NORESTART") -PassThru; if ($null -eq $process) { throw "installer did not start" }; [Console]::Out.WriteLine("${WINDOWS_INSTALLER_STARTED_SENTINEL}"); [Console]::Out.Flush(); $process.WaitForExit(); if ($process.ExitCode -ne 0) { throw "installer failed" } } finally { $stream.Dispose() }`,
    ].join('; ');
    return {
      child: spawn(
        plan.executable,
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
            stdio: ['ignore', 'pipe', 'ignore'],
            windowsHide: true,
          },
          { ACE_UPDATE_INSTALL_PAYLOAD_B64: payload },
        ),
      ),
      completionTimeoutMs: null,
    };
  }

  const inheritedStdio: SpawnOptions['stdio'] = [
    'ignore',
    'ignore',
    'ignore',
    packageDescriptor,
  ];
  if (plan.kind === 'linux-deb') {
    if (plan.executable === TRUSTED_UPDATE_HELPERS.linux.pkexec) {
      const stablePackagePath = `/proc/${process.pid}/fd/${packageDescriptor}`;
      return {
        child: spawn(
          plan.executable,
          [
            TRUSTED_UPDATE_HELPERS.linux.timeout,
            '--signal=KILL',
            '900',
            TRUSTED_UPDATE_HELPERS.linux.dpkg,
            '-i',
            stablePackagePath,
          ],
          hardenedChildProcessOptions({ detached: false, stdio: 'ignore' }),
        ),
        completionTimeoutMs: INSTALL_COMPLETION_TIMEOUT_MS,
      };
    }
    return {
      child: spawn(
        plan.executable,
        [
          '--signal=KILL',
          '900',
          TRUSTED_UPDATE_HELPERS.linux.dpkg,
          '-i',
          '/proc/self/fd/3',
        ],
        hardenedChildProcessOptions({ detached: false, stdio: inheritedStdio }),
      ),
      completionTimeoutMs: INSTALL_COMPLETION_TIMEOUT_MS,
    };
  }

  return {
    child: spawn(
      plan.executable,
      ['attach', '-readonly', '-autoopen', '/dev/fd/3'],
      hardenedChildProcessOptions({ detached: false, stdio: inheritedStdio }),
    ),
    completionTimeoutMs: DMG_ATTACH_TIMEOUT_MS,
  };
}

export async function launchVerifiedDownloadedUpdate(
  record: DownloadedUpdateRecord,
  publicKeyValue: string,
  platform: string = process.platform,
  uid: number | null = typeof process.getuid === 'function' ? process.getuid() : null,
  architecture: string = process.arch,
): Promise<UpdateInstallLaunchResult> {
  let failureCode = 'verification-failed';
  try {
    if (!publicKeyValue) {
      throw new UpdateInstallError(
        'missing-update-key',
        '更新已阻止：构建未嵌入更新签名公钥',
      );
    }
    const plan = buildUpdateInstallPlan(record.filePath, platform, uid, architecture);
    const lease = openVerifiedUpdateArtifact(
      record.filePath,
      `${record.filePath}.sig`,
      publicKeyValue,
      record.version,
      record,
    );
    try {
      for (const helperPath of plan.helperPaths) {
        assertTrustedSystemHelper(helperPath);
      }
      // This is intentionally the final synchronous operation before spawn.
      // The descriptors stay open until the child has consumed the package.
      lease.revalidate();
      const launched = spawnVerifiedPlan(
        plan,
        lease.packageDescriptor,
        record.packageSha256,
      );
      await waitForSpawn(launched.child);
      if (plan.kind === 'windows-exe') {
        await waitForWindowsInstallerConfirmation(launched.child);
      }
      if (launched.completionTimeoutMs !== null) {
        await waitForSuccessfulExit(launched.child, launched.completionTimeoutMs);
      } else {
        launched.child.unref();
      }
    } finally {
      lease.close();
    }

    return {
      message: plan.requiresCompletion
        ? (plan.kind === 'mac-dmg' ? '安装镜像已安全挂载' : '安装已安全完成')
        : '安装程序已启动',
      consumesRecord: plan.consumesRecord,
    };
  } catch (error) {
    failureCode = error instanceof UpdateInstallError ? error.code : failureCode;
    console.warn(
      '[update-security] installer-launch-rejected',
      failureCode,
      error instanceof Error ? error.name : 'unknown',
    );
    if (error instanceof UpdateInstallError) throw error;
    throw new UpdateInstallError(
      failureCode,
      '更新已阻止：安装包校验或启动失败',
    );
  }
}
