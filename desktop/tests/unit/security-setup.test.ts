import { Buffer } from 'buffer';
import { describe, expect, it } from 'vitest';
import {
  buildElevatedSecuritySetup,
  buildElevatedUacEnable,
  parseEnableLuaOutput,
} from '../../src/main/security-setup';

describe('Windows security setup elevation', () => {
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
    expect(script).toContain('-Verb RunAs -Wait');
    expect(script).toMatch(/-PassThru;\s*exit/);
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
