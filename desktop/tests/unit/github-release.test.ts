import { describe, expect, it } from 'vitest';
import {
  detectLinuxDistro,
  GITHUB_REPO,
  pickGithubReleaseAsset,
  resolveGithubReleaseDownloadUrl,
  resolveGithubReleaseTarget,
  type GithubReleaseAsset,
} from '../../src/main/update/github-release';

const asset = (name: string): GithubReleaseAsset => ({
  name,
  browser_download_url: `https://github.com/${GITHUB_REPO}/releases/download/v1.2.0/${name}`,
});

const ASSETS: GithubReleaseAsset[] = [
  asset('crew-desktop_1.2.0_arm64.dmg'),
  asset('crew-desktop_1.2.0_x64.dmg'),
  asset('crew-desktop_1.2.0_uos_amd64.deb'),
  asset('crew-desktop_1.2.0_kylin_amd64.deb'),
  asset('Crew_Setup_v1.2.0.exe'),
];

describe('detectLinuxDistro', () => {
  it('麒麟 Kylin 识别', () => {
    expect(detectLinuxDistro('ID=kylin\nNAME="Kylin"\n')).toBe('kylin');
    expect(detectLinuxDistro('ID=linux\nID_LIKE="kylin debian"\n')).toBe('kylin');
  });
  it('统信 UOS / deepin / uniontech 识别', () => {
    expect(detectLinuxDistro('ID=uos\nNAME="UOS"\n')).toBe('uos');
    expect(detectLinuxDistro('ID=deepin\n')).toBe('uos');
    expect(detectLinuxDistro('ID=uniontech\n')).toBe('uos');
  });
  it('其他/读取失败默认统信包', () => {
    expect(detectLinuxDistro('ID=ubuntu\n')).toBe('uos');
    expect(detectLinuxDistro(null)).toBe('uos');
    expect(detectLinuxDistro('')).toBe('uos');
  });
});

describe('resolveGithubReleaseTarget', () => {
  it('Windows → windows', () => {
    expect(resolveGithubReleaseTarget('win32', 'x64', 'uos')).toBe('windows');
  });
  it('macOS 按架构区分 arm64 / x64', () => {
    expect(resolveGithubReleaseTarget('darwin', 'arm64', 'uos')).toBe('mac-arm64');
    expect(resolveGithubReleaseTarget('darwin', 'x64', 'uos')).toBe('mac-x64');
  });
  it('Linux 按发行版区分麒麟 / 统信', () => {
    expect(resolveGithubReleaseTarget('linux', 'x64', 'kylin')).toBe('linux-kylin');
    expect(resolveGithubReleaseTarget('linux', 'x64', 'uos')).toBe('linux-uos');
  });
  it('其他平台 → unsupported', () => {
    expect(resolveGithubReleaseTarget('freebsd', 'x64', 'uos')).toBe('unsupported');
  });
});

describe('pickGithubReleaseAsset / resolveGithubReleaseDownloadUrl', () => {
  it('各平台匹配到正确安装包', () => {
    expect(pickGithubReleaseAsset(ASSETS, 'windows')?.name).toBe('Crew_Setup_v1.2.0.exe');
    expect(pickGithubReleaseAsset(ASSETS, 'mac-arm64')?.name).toBe('crew-desktop_1.2.0_arm64.dmg');
    expect(pickGithubReleaseAsset(ASSETS, 'mac-x64')?.name).toBe('crew-desktop_1.2.0_x64.dmg');
    expect(pickGithubReleaseAsset(ASSETS, 'linux-uos')?.name).toBe('crew-desktop_1.2.0_uos_amd64.deb');
    expect(pickGithubReleaseAsset(ASSETS, 'linux-kylin')?.name).toBe('crew-desktop_1.2.0_kylin_amd64.deb');
  });
  it('返回对应下载 URL', () => {
    expect(resolveGithubReleaseDownloadUrl(ASSETS, 'mac-arm64')).toBe(
      `https://github.com/${GITHUB_REPO}/releases/download/v1.2.0/crew-desktop_1.2.0_arm64.dmg`,
    );
  });
  it('unsupported / 找不到资产 → null', () => {
    expect(pickGithubReleaseAsset(ASSETS, 'unsupported')).toBeNull();
    expect(resolveGithubReleaseDownloadUrl([asset('crew-desktop_0.9.0_arm64.dmg')], 'windows')).toBeNull();
  });
  it('资产名大小写不敏感（Windows exe）', () => {
    expect(pickGithubReleaseAsset([asset('CREW_SETUP_V1.2.0.EXE')], 'windows')?.name).toBe(
      'CREW_SETUP_V1.2.0.EXE',
    );
  });
});
