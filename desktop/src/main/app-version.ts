import * as fs from 'fs';
import * as path from 'path';

const FALLBACK_VERSION = '2.0.0';

type AppVersionSource = {
  getVersion?: () => string;
};

interface PackageJson {
  version?: string;
  platform?: string;
}

function readPackageJson(): PackageJson {
  try {
    const packagePath = path.resolve(__dirname, '../../package.json');
    return JSON.parse(fs.readFileSync(packagePath, 'utf8')) as PackageJson;
  } catch {
    return {};
  }
}

function packageVersion(): string {
  return readPackageJson().version || FALLBACK_VERSION;
}

/**
 * 读取 package.json 中预制注入的平台标识（如 "Win amd64"、"UOS amd64"、"Kylin amd64"）。
 * 下游发行流水线可在构建安装包时写入该字段。
 * 开发环境无此字段时，回退为运行时自动检测。
 */
function packagePlatform(): string | null {
  const fromPkg = readPackageJson().platform;
  if (fromPkg) return fromPkg;
  // 开发环境回退：按 process.platform + arch 自动检测
  const arch = process.arch === 'x64' ? 'amd64' : process.arch;
  if (process.platform === 'win32') return `Win ${arch}`;
  if (process.platform === 'linux') return `Linux ${arch}`;
  if (process.platform === 'darwin') return `macOS ${arch}`;
  return `${process.platform} ${arch}`;
}

export function currentAppVersion(appSource?: AppVersionSource): string {
  try {
    const fromApp = appSource?.getVersion?.();
    if (fromApp) return fromApp;
  } catch {
    // App version is best-effort; package.json remains the stable fallback.
  }
  return packageVersion();
}

export function currentAppVersionLabel(appSource?: AppVersionSource): string {
  const version = currentAppVersion(appSource);
  const platform = packagePlatform();
  return platform
    ? `Crew Desktop ${version} (${platform})`
    : `Crew Desktop ${version}`;
}
