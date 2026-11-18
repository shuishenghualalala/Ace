import { Buffer } from 'buffer';
import type { spawn } from 'child_process';
import { EventEmitter } from 'events';
import { describe, expect, it, vi } from 'vitest';
import {
  buildElevatedSecuritySetup,
  buildElevatedUacEnable,
  parseEnableLuaOutput,
  provisionInstalledWindowsSandbox,
  securityRuntimePlatformKey,
} from '../../src/main/security-setup';

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
    expect(script).toContain('--windows-setup');
    expect(script).toContain('[Security.Principal.WindowsIdentity]::GetCurrent().User.Value');
    expect(script).toContain("'--expected-owner-sid', $ownerSid");
    expect(script).toContain('-Verb RunAs -Wait');
    expect(script).toMatch(/-PassThru -ErrorAction Stop;\s*exit/);
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
