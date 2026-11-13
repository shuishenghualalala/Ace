/**
 * GitHub Releases 下载解析：按当前系统 / 架构匹配对应安装包资产。
 *
 * 开源版不内置自动下载站（见 update-url.ts），"下载最新版本"按钮改为直接
 * 打开 GitHub Releases 里与当前平台匹配的安装包：
 *   macOS arm64 → crew-desktop_{ver}_arm64.dmg
 *   macOS x64   → crew-desktop_{ver}_x64.dmg
 *   Linux 麒麟  → crew-desktop_{ver}_kylin_amd64.deb
 *   Linux 统信/其他 → crew-desktop_{ver}_uos_amd64.deb
 *   Windows     → Crew_Setup_v{ver}.exe
 *
 * 纯函数模块（不 import electron），便于单测。
 */
import * as fs from 'node:fs';

/** GitHub 仓库 owner/name。开源版从 README / release workflow 同步。 */
export const GITHUB_REPO = 'shuishenghualalala/Ace';

/** 当前平台分类：'windows' | 'mac-arm64' | 'mac-x64' | 'linux-kylin' | 'linux-uos' | 'unsupported'。 */
export type GithubReleaseTarget =
  | 'windows'
  | 'mac-arm64'
  | 'mac-x64'
  | 'linux-kylin'
  | 'linux-uos'
  | 'unsupported';

export interface GithubReleaseAsset {
  name: string;
  browser_download_url: string;
}

/**
 * 读取 /etc/os-release 判断 Linux 发行版。
 * 麒麟 Kylin → 'kylin'；统信 UOS（ID/ID_LIKE 含 uos/deepin/uniontech）→ 'uos'；
 * 其他 / 读取失败 → 'uos'（默认统信包）。
 * 纯函数：osRelease 参数供单测注入，缺省读取真实文件。
 */
export function detectLinuxDistro(
  osRelease: string | null = readOsRelease(),
): 'kylin' | 'uos' {
  const raw = (osRelease ?? '').toLowerCase();
  if (/(^|\n)id\s*=\s*["']?kylin|id_like\s*=.*kylin/.test(raw)) return 'kylin';
  if (/(^|\n)id\s*=\s*["']?(uos|deepin|uniontech)/.test(raw)) return 'uos';
  return 'uos';
}

function readOsRelease(): string | null {
  try {
    return fs.readFileSync('/etc/os-release', 'utf8');
  } catch {
    return null;
  }
}

/** 按平台 + 架构 + 发行版解析目标。process 参数供单测注入。 */
export function resolveGithubReleaseTarget(
  platform: string = process.platform,
  arch: string = process.arch,
  distro?: 'kylin' | 'uos',
): GithubReleaseTarget {
  if (platform === 'win32') return 'windows';
  if (platform === 'linux') {
    // 仅 Linux 需要读发行版；惰性求值，避免其他平台做无意义文件访问
    return (distro ?? detectLinuxDistro()) === 'kylin' ? 'linux-kylin' : 'linux-uos';
  }
  if (platform === 'darwin') return arch === 'arm64' ? 'mac-arm64' : 'mac-x64';
  return 'unsupported';
}

/** 从 GitHub 资产列表中挑出当前平台对应的安装包；找不到返回 null。 */
export function pickGithubReleaseAsset(
  assets: readonly GithubReleaseAsset[],
  target: GithubReleaseTarget,
): GithubReleaseAsset | null {
  const pattern = assetPatternFor(target);
  if (!pattern) return null;
  for (const asset of assets) {
    if (pattern.test(asset.name)) return asset;
  }
  return null;
}

/** 各平台安装包文件名匹配规则（版本号用通配，兼容不同版本）。 */
function assetPatternFor(target: GithubReleaseTarget): RegExp | null {
  switch (target) {
    case 'windows':
      return /^Crew_Setup_v\d+(?:\.\d+)+\.exe$/i;
    case 'mac-arm64':
      return /^crew-desktop_.*_arm64\.dmg$/i;
    case 'mac-x64':
      return /^crew-desktop_.*_x64\.dmg$/i;
    case 'linux-kylin':
      return /^crew-desktop_.*_kylin_amd64\.deb$/i;
    case 'linux-uos':
      return /^crew-desktop_.*_uos_amd64\.deb$/i;
    default:
      return null;
  }
}

/** 当前系统应下载的 GitHub Releases 页面地址（无匹配资产时的兜底）。 */
export function githubReleasesPageUrl(): string {
  return `https://github.com/${GITHUB_REPO}/releases`;
}

/** 当前系统应下载的资产 URL；无匹配时返回 null。 */
export function resolveGithubReleaseDownloadUrl(
  assets: readonly GithubReleaseAsset[],
  target: GithubReleaseTarget,
): string | null {
  const asset = pickGithubReleaseAsset(assets, target);
  return asset?.browser_download_url ?? null;
}
