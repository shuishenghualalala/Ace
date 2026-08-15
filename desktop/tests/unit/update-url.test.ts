import { afterEach, describe, expect, it } from 'vitest';
import {
  buildUpdateUrl,
  DEFAULT_DOWNLOAD_BASE_URL,
  detectUpdatePlatform,
  downloadBaseUrl,
  isExpectedUpdateUrl,
  normalizeVersion,
} from '../../src/main/update/update-url';

const RELEASE_BASE = 'https://updates.example.test/releases/';

describe('normalizeVersion', () => {
  it('提取裸版本号', () => {
    expect(normalizeVersion('0.23.59')).toBe('0.23.59');
    expect(normalizeVersion('v0.23.59')).toBe('0.23.59');
    expect(normalizeVersion('Crew 0.23.59')).toBe('0.23.59');
  });
  it('空 / 无法识别 → null', () => {
    expect(normalizeVersion(undefined)).toBeNull();
    expect(normalizeVersion('')).toBeNull();
    expect(normalizeVersion('abc')).toBeNull();
  });
});

describe('detectUpdatePlatform', () => {
  it('accepts only architectures for which release artifacts exist', () => {
    expect(detectUpdatePlatform('win32', 'x64')).toBe('windows');
    expect(detectUpdatePlatform('linux', 'x64')).toBe('linux');
    expect(detectUpdatePlatform('darwin', 'arm64')).toBe('mac');
    expect(detectUpdatePlatform('linux', 'arm64')).toBe('unsupported');
    expect(detectUpdatePlatform('darwin', 'x64')).toBe('unsupported');
    expect(detectUpdatePlatform('freebsd', 'x64')).toBe('unsupported');
  });
});

describe('buildUpdateUrl', () => {
  afterEach(() => {
    delete process.env.ACE_DOWNLOAD_BASE_URL;
  });

  it.each([
    {
      name: 'Windows → Inno Setup exe 模板',
      version: '0.23.59',
      platform: 'windows',
      expected: `${RELEASE_BASE}Crew_Setup_v0.23.59.exe`,
    },
    {
      name: 'Linux → deb 模板（UOS/麒麟/标准 Debian 共用）',
      version: '0.23.59',
      platform: 'linux',
      expected: `${RELEASE_BASE}crew-desktop_0.23.59_amd64.deb`,
    },
    {
      name: 'macOS → dmg 模板（仅 Apple Silicon）',
      version: '0.23.59',
      platform: 'mac',
      expected: `${RELEASE_BASE}crew-desktop_0.23.59_arm64.dmg`,
    },
    {
      name: '支持带前缀的版本号',
      version: 'v0.23.59',
      platform: 'windows',
      expected: `${RELEASE_BASE}Crew_Setup_v0.23.59.exe`,
    },
  ])('$name', ({ version, platform, expected }) => {
    expect(buildUpdateUrl(version, platform, RELEASE_BASE)).toBe(expected);
  });

  it('不支持的平台抛错', () => {
    expect(() => buildUpdateUrl('0.23.59', 'unsupported')).toThrow();
  });

  it('ignores runtime environment overrides and disables an unconfigured source', () => {
    process.env.ACE_DOWNLOAD_BASE_URL = 'https://attacker.example/downloads';
    expect(downloadBaseUrl()).toBe(DEFAULT_DOWNLOAD_BASE_URL);
    expect(() => buildUpdateUrl('1.2.3', 'linux')).toThrow(/未配置/);
  });

  it.each([
    'http://example.test/downloads',
    'https://user:password@example.test/downloads',
    'https://example.test/downloads?token=secret',
    'https://example.test/downloads#fragment',
    'file:///tmp/releases',
    'ws://example.test/downloads',
    'https://example.test:0/downloads',
    'https://example.test:65536/downloads',
  ])('rejects an unsafe embedded download base: %s', (base) => {
    expect(() => downloadBaseUrl(base)).toThrow();
  });

  it('accepts only the exact version and platform URL under the embedded base', () => {
    const expected = `${RELEASE_BASE}crew-desktop_1.2.3_amd64.deb`;
    expect(isExpectedUpdateUrl(expected, '1.2.3', 'linux', RELEASE_BASE)).toBe(true);
    expect(isExpectedUpdateUrl('https://evil.example/crew-desktop_1.2.3_amd64.deb', '1.2.3', 'linux', RELEASE_BASE)).toBe(false);
    expect(isExpectedUpdateUrl(`${RELEASE_BASE}crew-desktop_9.9.9_amd64.deb`, '1.2.3', 'linux', RELEASE_BASE)).toBe(false);
  });
});
