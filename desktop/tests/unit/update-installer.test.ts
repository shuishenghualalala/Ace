import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  TRUSTED_UPDATE_HELPERS,
  assertTrustedSystemHelper,
  buildUpdateInstallPlan,
} from '../../src/main/update/update-installer';

const originalPath = process.env['PATH'];

afterEach(() => {
  if (originalPath === undefined) delete process.env['PATH'];
  else process.env['PATH'] = originalPath;
});

function packagePath(extension: string): string {
  return path.resolve(path.join(os.tmpdir(), `crew-update${extension}`));
}

describe('update installer plans', () => {
  it('uses fixed absolute Linux helpers even when PATH is hijacked', () => {
    process.env['PATH'] = path.join(os.tmpdir(), 'attacker-bin');

    const userPlan = buildUpdateInstallPlan(packagePath('.deb'), 'linux', 1000, 'x64');
    expect(userPlan.executable).toBe(TRUSTED_UPDATE_HELPERS.linux.pkexec);
    expect(userPlan.helperPaths).toEqual([
      TRUSTED_UPDATE_HELPERS.linux.pkexec,
      TRUSTED_UPDATE_HELPERS.linux.timeout,
      TRUSTED_UPDATE_HELPERS.linux.dpkg,
    ]);
    expect(path.isAbsolute(userPlan.executable)).toBe(true);

    const rootPlan = buildUpdateInstallPlan(packagePath('.deb'), 'linux', 0, 'x64');
    expect(rootPlan.executable).toBe(TRUSTED_UPDATE_HELPERS.linux.timeout);
    expect(rootPlan.helperPaths).toEqual([
      TRUSTED_UPDATE_HELPERS.linux.timeout,
      TRUSTED_UPDATE_HELPERS.linux.dpkg,
    ]);
  });

  it('uses the fixed hdiutil path for macOS without shell or open fallback', () => {
    process.env['PATH'] = path.join(os.tmpdir(), 'attacker-bin');
    const plan = buildUpdateInstallPlan(packagePath('.dmg'), 'darwin', 501, 'arm64');

    expect(plan.executable).toBe(TRUSTED_UPDATE_HELPERS.mac.hdiutil);
    expect(plan.helperPaths).toEqual([TRUSTED_UPDATE_HELPERS.mac.hdiutil]);
    expect(plan.kind).toBe('mac-dmg');
  });

  it('launches only the exact platform package type', () => {
    const windowsPlan = buildUpdateInstallPlan(
      packagePath('.exe'),
      'win32',
      null,
      'x64',
    );
    expect(windowsPlan.kind).toBe('windows-exe');
    expect(windowsPlan.executable).toBe(TRUSTED_UPDATE_HELPERS.windows.powershell);
    expect(windowsPlan.helperPaths).toEqual([
      TRUSTED_UPDATE_HELPERS.windows.powershell,
    ]);
    expect(() => buildUpdateInstallPlan(
      packagePath('.deb'),
      'win32',
      null,
      'x64',
    )).toThrow(
      /平台与安装包类型不匹配/,
    );
    expect(() => buildUpdateInstallPlan(
      packagePath('.exe'),
      'linux',
      1000,
      'x64',
    )).toThrow(
      /平台与安装包类型不匹配/,
    );
    expect(() => buildUpdateInstallPlan(
      packagePath('.dmg'),
      'freebsd',
      null,
      'x64',
    )).toThrow(
      /平台与安装包类型不匹配/,
    );
    expect(() => buildUpdateInstallPlan(
      packagePath('.zip'),
      'darwin',
      501,
      'arm64',
    )).toThrow(
      /平台与安装包类型不匹配/,
    );
    expect(() => buildUpdateInstallPlan(
      packagePath('.deb'),
      'linux',
      1000,
      'arm64',
    )).toThrow(/平台与安装包类型不匹配/);
  });

  it('rejects relative package paths and non-allowlisted helpers', () => {
    expect(() => buildUpdateInstallPlan(
      'relative-update.exe',
      'win32',
      null,
      'x64',
    )).toThrow(
      /规范绝对路径/,
    );
    expect(() => assertTrustedSystemHelper(
      path.resolve(path.join(os.tmpdir(), 'attacker-bin', 'pkexec')),
    )).toThrow(/路径不可信/);
  });

  it('holds a deny-write lease while the Windows installer runs', () => {
    const source = fs.readFileSync('src/main/update/update-installer.ts', 'utf8');
    expect(source).toContain('[IO.FileShare]::Read');
    expect(source).toContain('packageSha256');
    expect(source).toContain('ACE_UPDATE_INSTALLER_STARTED');
    expect(source).toContain('$process.WaitForExit()');
    expect(source).not.toContain("spawn(\n        plan.packagePath");
  });
});
