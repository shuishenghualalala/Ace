import { Buffer } from 'buffer';
import { describe, expect, it } from 'vitest';
import { buildElevatedSecuritySetup } from '../../src/main/security-setup';

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
  });

  it('rejects a renamed or relative executable', () => {
    expect(() => buildElevatedSecuritySetup('runtime.exe', 'C:\\state', 'install')).toThrow();
  });
});
