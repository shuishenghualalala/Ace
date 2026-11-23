import { Buffer } from 'buffer';
import type { spawn } from 'child_process';
import { EventEmitter } from 'events';
import { existsSync, mkdtempSync, readdirSync, rmSync, rmdirSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { describe, expect, it, vi } from 'vitest';
import {
  buildElevatedSecuritySetup,
  buildElevatedUacEnable,
  createWindowsSandboxSetupCoordinator,
  parseEnableLuaOutput,
  provisionInstalledWindowsSandbox,
  publishWindowsSandboxSetup,
  securityRuntimePlatformKey,
  windowsPackagedGatewaySecurityEnv,
  type SecuritySetupResult,
} from '../../src/main/security-setup';

describe('Windows packaged Gateway security environment', () => {
  it('replaces inherited security keys regardless of their casing', () => {
    expect(windowsPackagedGatewaySecurityEnv({
      Ace_Security_Runtime: 'old helper',
      ACE_SECURITY_RUNTIME: 'another helper',
      ace_security_state_dir: 'old state',
      ace_strict_security: '0',
      ace_security_runtime_bundled_only: '0',
      ace_desktop_windows_sandbox_setup_dir: 'old attempt',
      Path: 'system path',
    }, {
      ACE_SECURITY_RUNTIME: 'bundled helper',
      ACE_SECURITY_STATE_DIR: 'current state',
      ACE_STRICT_SECURITY: '1',
    }, 'current attempt')).toEqual({
      Path: 'system path',
      ACE_SECURITY_RUNTIME: 'bundled helper',
      ACE_SECURITY_STATE_DIR: 'current state',
      ACE_STRICT_SECURITY: '1',
      ACE_SECURITY_RUNTIME_BUNDLED_ONLY: '1',
      ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR: 'current attempt',
    });
  });

  it('omits the runtime and preparation location when the bundle fails validation', () => {
    const env = windowsPackagedGatewaySecurityEnv({
      ACE_SECURITY_RUNTIME: 'old helper',
      ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR: 'old attempt',
    }, { ACE_SECURITY_STATE_DIR: 'current state' }, 'current attempt');
    expect(env).not.toHaveProperty('ACE_SECURITY_RUNTIME');
    expect(env.ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR).toBeUndefined();
    expect(env.ACE_SECURITY_RUNTIME_BUNDLED_ONLY).toBe('1');
  });
});

describe('Windows security setup elevation', () => {
  it('uses the ordinary native check to skip setup on an already prepared device', async () => {
    const spawnProcess = vi.fn(() => {
      const child = new EventEmitter();
      queueMicrotask(() => child.emit('close', 0));
      return child;
    });
    const runtime = 'C:\\Program Files\\Crew\\ace-security-runtime.exe';
    const stateDir = 'C:\\Users\\A\\AppData\\Roaming\\Crew\\security';
    await expect(provisionInstalledWindowsSandbox(
      runtime, stateDir, spawnProcess as unknown as typeof spawn,
    )).resolves.toMatchObject({ ok: true, exitCode: 0 });
    expect(spawnProcess).toHaveBeenCalledExactlyOnceWith(
      runtime, ['--windows-check-setup', stateDir], expect.objectContaining({ windowsHide: true }),
    );
  });

  it('does not elevate when the native check reports an invalid sandbox account', async () => {
    const spawnProcess = vi.fn(() => {
      const child = new EventEmitter();
      queueMicrotask(() => child.emit('close', 5));
      return child;
    });
    const result = await provisionInstalledWindowsSandbox(
      'C:\\Program Files\\Crew\\ace-security-runtime.exe',
      'C:\\Users\\A\\AppData\\Roaming\\Crew\\security',
      spawnProcess as unknown as typeof spawn,
      { automatic: true },
    );
    expect(result).toMatchObject({
      ok: false,
      exitCode: 5,
      code: 'windows_sandbox_account_repair_required',
      detail: expect.stringContaining('自动准备没有更改 Windows 账号'),
    });
    expect(spawnProcess).toHaveBeenCalledExactlyOnceWith(
      'C:\\Program Files\\Crew\\ace-security-runtime.exe',
      ['--windows-check-setup', 'C:\\Users\\A\\AppData\\Roaming\\Crew\\security'],
      expect.objectContaining({ windowsHide: true }),
    );
  });

  it.runIf(process.platform === 'win32').each([
    [1223, 'uac_cancelled'],
    [4, 'windows_setup_owner_mismatch'],
  ])('remembers exit %i across automatic attempts and permits a user retry', async (exitCode, code) => {
    const parent = mkdtempSync(join(tmpdir(), 'crew-setup-consent-'));
    const runtime = join(parent, 'ace-security-runtime.exe');
    const stateDir = join(parent, 'security');
    writeFileSync(runtime, '');
    const childExit = vi.fn()
      .mockReturnValueOnce(1).mockReturnValueOnce(exitCode)
      .mockReturnValueOnce(1)
      .mockReturnValueOnce(0)
      .mockReturnValueOnce(1).mockReturnValueOnce(exitCode)
      .mockReturnValueOnce(1).mockReturnValueOnce(0).mockReturnValueOnce(0)
      .mockReturnValueOnce(0)
      .mockReturnValueOnce(1).mockReturnValueOnce(0).mockReturnValueOnce(0);
    const spawnProcess = vi.fn(() => {
      const child = new EventEmitter();
      queueMicrotask(() => child.emit('close', childExit()));
      return child;
    }) as unknown as typeof spawn;

    try {
      const automatic = { automatic: true };
      const deferred = { ok: false, exitCode, code };
      await expect(provisionInstalledWindowsSandbox(runtime, stateDir, spawnProcess, automatic)).resolves.toMatchObject(deferred);
      await expect(provisionInstalledWindowsSandbox(runtime, stateDir, spawnProcess, automatic)).resolves.toMatchObject(deferred);
      expect(spawnProcess).toHaveBeenCalledTimes(3);
      await expect(provisionInstalledWindowsSandbox(runtime, stateDir, spawnProcess, automatic)).resolves.toMatchObject({ ok: true });
      expect(spawnProcess).toHaveBeenCalledTimes(4);
      await expect(provisionInstalledWindowsSandbox(runtime, stateDir, spawnProcess, automatic)).resolves.toMatchObject(deferred);
      expect(spawnProcess).toHaveBeenCalledTimes(6);
      await expect(provisionInstalledWindowsSandbox(runtime, stateDir, spawnProcess)).resolves.toMatchObject({ ok: true });
      expect(spawnProcess).toHaveBeenCalledTimes(9);
      await expect(provisionInstalledWindowsSandbox(runtime, stateDir, spawnProcess, automatic)).resolves.toMatchObject({ ok: true });
      expect(spawnProcess).toHaveBeenCalledTimes(10);
      await expect(provisionInstalledWindowsSandbox(runtime, stateDir, spawnProcess, automatic)).resolves.toMatchObject({ ok: true });
      expect(spawnProcess).toHaveBeenCalledTimes(13);
    } finally {
      rmSync(parent, { recursive: true });
    }
  });

  it.runIf(process.platform === 'win32')('defers automatic retries after a generic setup failure and allows an explicit retry', async () => {
    const parent = mkdtempSync(join(tmpdir(), 'crew-setup-retry-'));
    const runtime = join(parent, 'ace-security-runtime.exe');
    const stateDir = join(parent, 'security');
    const failureMarker = join(stateDir, 'windows-setup-failure-deferred');
    writeFileSync(runtime, '');
    const childExit = vi.fn()
      .mockReturnValueOnce(1).mockReturnValueOnce(5)
      .mockReturnValueOnce(1)
      .mockReturnValueOnce(1).mockReturnValueOnce(0).mockReturnValueOnce(0)
      .mockReturnValueOnce(0);
    const spawnProcess = vi.fn(() => {
      const child = new EventEmitter();
      queueMicrotask(() => child.emit('close', childExit()));
      return child;
    }) as unknown as typeof spawn;
    const provision = (runtimePath: string, targetStateDir: string, _spawnProcess: typeof spawn, options: { automatic: boolean }) =>
      provisionInstalledWindowsSandbox(runtimePath, targetStateDir, spawnProcess, options);
    const launch = { platform: 'win32', visible: true, ci: false };
    const location = () => ({ runtimePath: runtime, stateDir });

    try {
      const firstLaunch = createWindowsSandboxSetupCoordinator(provision).startAutomatically(launch, location);
      if (!firstLaunch) throw new Error('expected automatic Windows setup');
      await expect(firstLaunch).resolves.toMatchObject({ ok: false, exitCode: 5 });
      expect(existsSync(failureMarker)).toBe(true);

      const afterRestart = createWindowsSandboxSetupCoordinator(provision);
      const repeatedLaunch = afterRestart.startAutomatically(launch, location);
      if (!repeatedLaunch) throw new Error('expected automatic Windows setup after restart');
      await expect(repeatedLaunch).resolves.toMatchObject({
        ok: false,
        code: 'windows_setup_retry_deferred',
        detail: expect.stringContaining('显式重试'),
      });
      expect(spawnProcess).toHaveBeenCalledTimes(3);

      await expect(afterRestart.run('install', runtime, stateDir)).resolves.toMatchObject({ ok: true });
      expect(existsSync(failureMarker)).toBe(false);
      expect(spawnProcess).toHaveBeenCalledTimes(6);

      const afterRecovery = createWindowsSandboxSetupCoordinator(provision).startAutomatically(launch, location);
      if (!afterRecovery) throw new Error('expected an automatic native readiness check after recovery');
      await expect(afterRecovery).resolves.toMatchObject({ ok: true });
      expect(spawnProcess).toHaveBeenCalledTimes(7);
    } finally {
      rmSync(parent, { recursive: true });
    }
  });

  it('accepts native and Electron platform names for the same runtime architecture', () => {
    expect(securityRuntimePlatformKey('windows', 'AMD64')).toBe(securityRuntimePlatformKey('win32', 'x64'));
    expect(securityRuntimePlatformKey('Darwin', 'aarch64')).toBe('darwin-arm64');
    expect(securityRuntimePlatformKey('Linux', 'x86_64')).toBe('linux-x64');
    expect(securityRuntimePlatformKey('windows', '')).toBeUndefined();
    expect(securityRuntimePlatformKey('windows', 'arm64')).not.toBe(securityRuntimePlatformKey('windows', 'AMD64'));
  });

  it('uses fixed PowerShell and encoded trusted paths for repair', () => {
    const command = buildElevatedSecuritySetup(
      'C:\\Program Files\\Crew\\ace-security-runtime.exe',
      'C:\\Users\\A\\AppData\\Roaming\\Crew\\security',
      'repair',
    );
    expect(command.executable.toLowerCase()).toContain('powershell.exe');
    const encoded = command.argv.at(-1) ?? '';
    const script = Buffer.from(encoded, 'base64').toString('utf16le');
    expect(script).toContain('--windows-repair');
    expect(script).not.toContain('--windows-setup');
    expect(script).toContain('[Security.Principal.WindowsIdentity]::GetCurrent().User.Value');
    expect(script).toContain("'--expected-owner-sid', $ownerSid");
    expect(script).toContain("$start.Verb = 'runas'");
    expect(script).toContain('$start.UseShellExecute = $true');
    expect(script).toContain('[System.Diagnostics.Process]::Start($start)');
    expect(script).toContain('$cause.NativeErrorCode -eq 1223');
  });

  it('rejects a renamed or relative executable', () => {
    expect(() => buildElevatedSecuritySetup('runtime.exe', 'C:\\state', 'install')).toThrow();
  });

  it('builds an explicit elevated command that enables UAC', () => {
    const command = buildElevatedUacEnable();
    const encoded = command.argv.at(-1) ?? '';
    const script = Buffer.from(encoded, 'base64').toString('utf16le');
    expect(script).toMatch(/-PassThru;\s*exit/);
    const innerEncoded = script.match(/-EncodedCommand', '([^']+)'\)/)?.[1] ?? '';
    const innerScript = Buffer.from(innerEncoded, 'base64').toString('utf16le');
    expect(innerScript).toContain('EnableLUA');
    expect(innerScript).toContain('HKLM:\\SOFTWARE');
  });

  it('parses the machine UAC registry value without depending on locale text', () => {
    expect(parseEnableLuaOutput('EnableLUA    REG_DWORD    0x0')).toBe(false);
    expect(parseEnableLuaOutput('EnableLUA    REG_DWORD    0x1')).toBe(true);
    expect(parseEnableLuaOutput('not available')).toBeNull();
  });
});

describe('Windows sandbox setup coordinator', () => {
  const runtimePath = 'C:\\Program Files\\Crew\\ace-security-runtime.exe';
  const stateDir = 'C:\\Users\\A\\AppData\\Roaming\\Crew\\security';
  // Source and packaged launches share this shape; setup must not require app.isPackaged.
  const visibleWindows = { platform: 'win32', visible: true, ci: false };
  const ready = { ok: true, exitCode: 0 };
  const location = () => ({ runtimePath, stateDir });

  it.each([
    { platform: 'darwin' },
    { platform: 'linux' },
    { ci: true },
  ])('does not inspect the runtime or provision for an ineligible launch (%j)', async (condition) => {
    const provision = vi.fn(async () => ready);
    const locate = vi.fn(location);
    const coordinator = createWindowsSandboxSetupCoordinator(provision);
    expect(coordinator.startAutomatically({ ...visibleWindows, ...condition }, locate)).toBeUndefined();
    expect(locate).not.toHaveBeenCalled();
    expect(provision).not.toHaveBeenCalled();

    await expect(coordinator.startAutomatically(visibleWindows, locate)).resolves.toEqual(ready);
    expect(provision).toHaveBeenCalledExactlyOnceWith(runtimePath, stateDir, undefined, { automatic: true });
  });

  it('skips provisioning when no verified runtime is available', () => {
    const provision = vi.fn(async () => ready);
    const locate = vi.fn(() => undefined);
    const coordinator = createWindowsSandboxSetupCoordinator(provision);

    expect(coordinator.startAutomatically(visibleWindows, locate)).toBeUndefined();
    expect(locate).toHaveBeenCalledTimes(1);
    expect(provision).not.toHaveBeenCalled();
    expect(coordinator.startAutomatically(visibleWindows, locate)).toBeUndefined();
  });

  it('defers checking a hidden window until it is opened and checks only once', async () => {
    const provision = vi.fn(async () => ready);
    const locate = vi.fn(location);
    const coordinator = createWindowsSandboxSetupCoordinator(provision);
    const hidden = { ...visibleWindows, visible: false };

    expect(coordinator.startAutomatically(hidden, locate)).toBeUndefined();
    expect(locate).not.toHaveBeenCalled();
    await expect(coordinator.startAutomatically(visibleWindows, locate)).resolves.toEqual(ready);
    expect(coordinator.startAutomatically(hidden, locate)).toBeUndefined();
    expect(coordinator.startAutomatically(visibleWindows, locate)).toBeUndefined();
    expect(locate).toHaveBeenCalledTimes(1);
    expect(provision).toHaveBeenCalledExactlyOnceWith(runtimePath, stateDir, undefined, { automatic: true });
  });

  it('shares one install between a visible startup and the settings action', async () => {
    let resolveProvision: (value: SecuritySetupResult) => void = () => {};
    const provision = vi.fn(() => new Promise<SecuritySetupResult>((resolve) => { resolveProvision = resolve; }));
    const elevated = vi.fn(async () => ready);
    const coordinator = createWindowsSandboxSetupCoordinator(provision, elevated);
    const first = coordinator.startAutomatically(visibleWindows, location);
    expect(first).toBeDefined();
    expect(coordinator.run('install', runtimePath, stateDir)).toBe(first);
    expect(coordinator.startAutomatically(visibleWindows, location)).toBeUndefined();
    await expect(coordinator.run('uninstall', runtimePath, stateDir)).resolves.toMatchObject({ ok: false, exitCode: null });
    expect(elevated).not.toHaveBeenCalled();

    resolveProvision(ready);
    await expect(first).resolves.toEqual(ready);
    expect(provision).toHaveBeenCalledExactlyOnceWith(runtimePath, stateDir, undefined, { automatic: true });
    await expect(coordinator.run('repair', runtimePath, stateDir)).resolves.toEqual(ready);
    expect(elevated).toHaveBeenCalledExactlyOnceWith(runtimePath, stateDir, 'repair');
  });

  it('asks before an elevated repair and returns a local cancellation', async () => {
    const provision = vi.fn(async () => ready);
    const elevated = vi.fn(async () => ready);
    const confirm = vi.fn(async () => false);
    const coordinator = createWindowsSandboxSetupCoordinator(provision, elevated, confirm);

    await expect(coordinator.run('repair', runtimePath, stateDir)).resolves.toMatchObject({
      ok: false,
      code: 'uac_cancelled',
    });
    expect(elevated).not.toHaveBeenCalled();

    confirm.mockResolvedValueOnce(true);
    await expect(coordinator.run('repair', runtimePath, stateDir)).resolves.toEqual(ready);
    expect(elevated).toHaveBeenCalledExactlyOnceWith(runtimePath, stateDir, 'repair');
    expect(confirm).toHaveBeenCalledTimes(2);
  });

  it('passes the confirmation gate to the install provisioner', async () => {
    const provision = vi.fn(async (
      _runtimePath: string,
      _stateDir: string,
      _spawn: typeof spawn,
      options: { automatic?: boolean; confirm?: () => Promise<boolean> },
    ) => {
      if (!options.confirm) throw new Error('expected a confirmation gate');
      return (await options.confirm()) ? ready : { ok: false, exitCode: 1 };
    });
    const confirm = vi.fn(async () => false);
    const coordinator = createWindowsSandboxSetupCoordinator(provision, undefined, confirm);

    await expect(coordinator.run('install', runtimePath, stateDir)).resolves.toMatchObject({ ok: false });
    expect(confirm).toHaveBeenCalledExactlyOnceWith('install');

    confirm.mockResolvedValueOnce(true);
    await expect(coordinator.run('install', runtimePath, stateDir)).resolves.toEqual(ready);
  });

  it('keeps cancellation from triggering another automatic attempt and permits an explicit retry', async () => {
    const cancelled = { ok: false, exitCode: 1 };
    const provision = vi.fn<() => Promise<SecuritySetupResult>>()
      .mockResolvedValueOnce(cancelled)
      .mockResolvedValueOnce(ready);
    const coordinator = createWindowsSandboxSetupCoordinator(provision);

    await expect(coordinator.startAutomatically(visibleWindows, location)).resolves.toEqual(cancelled);
    expect(coordinator.startAutomatically(visibleWindows, location)).toBeUndefined();
    await expect(coordinator.run('install', runtimePath, stateDir)).resolves.toEqual(ready);
    expect(provision).toHaveBeenCalledTimes(2);
  });
});

describe('Windows automatic sandbox setup status', () => {
  it.each([
    { ok: true, marker: 'ready' },
    { ok: false, marker: 'failed' },
  ])('publishes pending synchronously and completes with $marker', async ({ ok, marker }) => {
    const parent = mkdtempSync(join(tmpdir(), 'crew-setup-test-'));
    const directory = join(parent, 'operation');
    let finish: (result: SecuritySetupResult) => void = () => {};
    const setup = new Promise<SecuritySetupResult>((resolve) => { finish = resolve; });
    const onError = vi.fn();
    const dispose = publishWindowsSandboxSetup(directory, setup, onError);
    try {
      expect(readdirSync(directory)).toEqual([]);
      finish({ ok, exitCode: ok ? 0 : 1 });
      await setup;
      expect(readdirSync(directory)).toEqual([marker]);
    } finally {
      dispose();
      rmdirSync(parent);
    }
    expect(existsSync(directory)).toBe(false);
    expect(onError).not.toHaveBeenCalled();
  });
});
