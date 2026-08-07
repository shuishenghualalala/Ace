/**
 * 客户端按 OS 拼接更新包下载 URL（不改服务端）。
 *
 * 服务端心跳只回 `version`；本机据此 + 当前系统选模板拼出下载地址：
 *   Windows → {base}/Crew_Setup_v{ver}.exe   （Inno Setup，见 deb-package/pack_exe.ps1）
 *   Linux   → {base}/crew-desktop_{ver}_amd64.deb     （UOS / 麒麟 / 标准 Debian 共用同一 deb）
 *   macOS   → {base}/crew-desktop_{ver}_arm64.dmg       （仅 Apple Silicon）
 *
 * base 取自环境变量 `ACE_DOWNLOAD_BASE_URL`；开源版缺省为空，避免指向内部下载站。
 * 纯函数模块（不 import electron），便于单测。
 */

/** 更新包下载站。开源版未配置发布源，留空以禁用自动下载（后续可注入 GitHub Releases 等）。 */
export const DEFAULT_DOWNLOAD_BASE_URL = '';

export function downloadBaseUrl(): string {
  const fromEnv = process.env['ACE_DOWNLOAD_BASE_URL']?.trim();
  if (fromEnv) return fromEnv.endsWith('/') ? fromEnv : `${fromEnv}/`;
  return DEFAULT_DOWNLOAD_BASE_URL;
}

export type UpdatePlatform = 'windows' | 'linux' | 'mac' | 'unsupported';

/** 检索当前系统（process.platform 总可用，无需持久化）。 */
export function detectUpdatePlatform(platform: string = process.platform): UpdatePlatform {
  if (platform === 'win32') return 'windows';
  if (platform === 'linux') return 'linux';
  if (platform === 'darwin') return 'mac';
  return 'unsupported';
}

/**
 * 归一化版本号：从 `0.23.59` / `v0.23.59` / `Crew 0.23.59` 中取 `X.Y.Z` 数字核心。
 * 服务端按约定回裸 `0.23.59`；带前缀/标签时也防御性提取。
 */
export function normalizeVersion(raw?: string): string | null {
  if (!raw) return null;
  const match = raw.trim().match(/(\d+(?:\.\d+){1,3})/);
  return match?.[1] ?? null;
}

/**
 * 拼接下载 URL。version 必须能归一化为数字版本；platform 不支持时抛错。
 */
export function buildUpdateUrl(version: string, platform?: UpdatePlatform): string {
  const normalized = normalizeVersion(version);
  if (!normalized) {
    throw new Error(`无法识别的版本号: ${version}`);
  }
  const target = platform ?? detectUpdatePlatform();
  const base = downloadBaseUrl();
  switch (target) {
    case 'windows':
      return `${base}Crew_Setup_v${normalized}.exe`;
    case 'linux':
      return `${base}crew-desktop_${normalized}_amd64.deb`;
    case 'mac':
      return `${base}crew-desktop_${normalized}_arm64.dmg`;
    default:
      throw new Error(`当前系统暂不支持自动更新: ${process.platform}`);
  }
}
