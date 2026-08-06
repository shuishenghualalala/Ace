import { afterEach, describe, expect, it } from 'vitest';
import {
  buildUpdateUrl,
  DEFAULT_DOWNLOAD_BASE_URL,
  detectUpdatePlatform,
  downloadBaseUrl,
  normalizeVersion,
} from '../../src/main/update/update-url';

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
  it('win32/linux/darwin 分类', () => {
    expect(detectUpdatePlatform('win32')).toBe('windows');
    expect(detectUpdatePlatform('linux')).toBe('linux');
    expect(detectUpdatePlatform('darwin')).toBe('unsupported');
  });
});

describe('buildUpdateUrl', () => {
  afterEach(() => {
    delete process.env.ACE_DOWNLOAD_BASE_URL;
  });

  it('Windows → Inno Setup exe 模板', () => {
    expect(buildUpdateUrl('0.23.59', 'windows')).toBe(
      `${DEFAULT_DOWNLOAD_BASE_URL}Crew_Setup_v0.23.59.exe`,
    );
  });

  it('Linux → deb 模板（UOS/麒麟/标准 Debian 共用）', () => {
    expect(buildUpdateUrl('0.23.59', 'linux')).toBe(
      `${DEFAULT_DOWNLOAD_BASE_URL}crew-desktop_0.23.59_amd64.deb`,
    );
  });

  it('支持带前缀的版本号', () => {
    expect(buildUpdateUrl('v0.23.59', 'windows')).toBe(
      `${DEFAULT_DOWNLOAD_BASE_URL}Crew_Setup_v0.23.59.exe`,
    );
  });

  it('不支持的平台抛错', () => {
    expect(() => buildUpdateUrl('0.23.59', 'unsupported')).toThrow();
  });

  it('自定义下载基址（自动补斜杠）', () => {
    process.env.ACE_DOWNLOAD_BASE_URL = 'http://example.test/downloads';
    expect(downloadBaseUrl()).toBe('http://example.test/downloads/');
    expect(buildUpdateUrl('1.2.3', 'linux')).toBe('http://example.test/downloads/crew-desktop_1.2.3_amd64.deb');
  });
});
