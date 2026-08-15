/**
 * 客户端按 OS 拼接更新包下载 URL（不改服务端）。
 *
 * 服务端心跳只回 `version`；本机据此 + 当前系统选模板拼出下载地址：
 *   Windows → {base}/Crew_Setup_v{ver}.exe   （Inno Setup，见 deb-package/pack_exe.ps1）
 *   Linux   → {base}/crew-desktop_{ver}_amd64.deb     （UOS / 麒麟 / 标准 Debian 共用同一 deb）
 *   macOS   → {base}/crew-desktop_{ver}_arm64.dmg       （仅 Apple Silicon）
 *
 * base 在构建时注入；运行时环境变量不能改写更新信任边界。
 * 纯函数模块（不 import electron），便于单测。
 */

/** 更新包下载站。开源版未配置发布源，留空以禁用自动下载（后续可注入 GitHub Releases 等）。 */
export const DEFAULT_DOWNLOAD_BASE_URL = '';

declare const __ACE_DOWNLOAD_BASE_URL__: string;

function embeddedDownloadBaseUrl(): string {
  return typeof __ACE_DOWNLOAD_BASE_URL__ === 'string'
    ? __ACE_DOWNLOAD_BASE_URL__
    : DEFAULT_DOWNLOAD_BASE_URL;
}

export function downloadBaseUrl(value = embeddedDownloadBaseUrl()): string {
  const raw = value.trim();
  if (!raw) return '';

  const parsed = new URL(raw);
  if (parsed.protocol !== 'https:') {
    throw new Error('更新下载源必须使用 HTTPS');
  }
  if (parsed.port === '0') {
    throw new Error('更新下载源端口无效');
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('更新下载源不得包含凭据、查询参数或片段');
  }
  if (!parsed.pathname.endsWith('/')) parsed.pathname += '/';
  return parsed.href;
}

export type UpdatePlatform = 'windows' | 'linux' | 'mac' | 'unsupported';

/** 检索当前系统（process.platform 总可用，无需持久化）。 */
export function detectUpdatePlatform(
  platform: string = process.platform,
  architecture: string = process.arch,
): UpdatePlatform {
  if (platform === 'win32' && architecture === 'x64') return 'windows';
  if (platform === 'linux' && architecture === 'x64') return 'linux';
  if (platform === 'darwin' && architecture === 'arm64') return 'mac';
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
export function buildUpdateUrl(
  version: string,
  platform?: UpdatePlatform,
  baseUrl = downloadBaseUrl(),
): string {
  const normalized = normalizeVersion(version);
  if (!normalized) {
    throw new Error(`无法识别的版本号: ${version}`);
  }
  const target = platform ?? detectUpdatePlatform();
  const base = downloadBaseUrl(baseUrl);
  if (!base) throw new Error('更新下载源未配置');
  let filename: string;
  switch (target) {
    case 'windows':
      filename = `Crew_Setup_v${normalized}.exe`;
      break;
    case 'linux':
      filename = `crew-desktop_${normalized}_amd64.deb`;
      break;
    case 'mac':
      filename = `crew-desktop_${normalized}_arm64.dmg`;
      break;
    default:
      throw new Error(`当前系统暂不支持自动更新: ${process.platform}/${process.arch}`);
  }
  return new URL(filename, base).href;
}

export function isExpectedUpdateUrl(
  value: string,
  version: string,
  platform?: UpdatePlatform,
  baseUrl = downloadBaseUrl(),
): boolean {
  try {
    return new URL(value).href === buildUpdateUrl(version, platform, baseUrl);
  } catch {
    return false;
  }
}
