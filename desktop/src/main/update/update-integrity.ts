/**
 * 更新包完整性校验（纯客户端，非防篡改）。
 *
 * 不需要服务端改动，只抓最常见的"下载坏了"——截断 / 后缀错配。
 * - 完整性：落盘字节数与下载记录的 size 一致。
 * - 格式 magic bytes：exe 必须是 PE（`MZ`）；deb 必须是 ar 归档（`!<arch>\n`）。
 *
 * 不做防篡改哈希（需服务端签名/哈希，本次范围外）。
 */
import * as fs from 'fs';
import * as path from 'path';

/** PE 可执行文件 magic（前 2 字节 `MZ`）。 */
const EXE_MAGIC = Buffer.from('MZ', 'ascii');
/** Debian .deb 实际是 ar 归档，magic 为 `!<arch>\n`（8 字节）。 */
const DEB_MAGIC = Buffer.from('!<arch>\n', 'ascii');

export type PackageKind = 'exe' | 'deb' | 'unknown';

export function packageKindFromPath(filePath: string): PackageKind {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === '.exe') return 'exe';
  if (ext === '.deb') return 'deb';
  return 'unknown';
}

export interface IntegrityResult {
  ok: boolean;
  message?: string;
}

/**
 * 校验落盘文件：存在 + size 一致 + magic bytes 匹配后缀。
 * expectedSize <= 0 时跳过 size 校验（未知 Content-Length）。
 */
export function verifyPackageIntegrity(filePath: string, expectedSize: number): IntegrityResult {
  let stat: fs.Stats;
  try {
    stat = fs.statSync(filePath);
  } catch {
    return { ok: false, message: '安装包文件不存在' };
  }
  if (!stat.isFile()) {
    return { ok: false, message: '安装包路径不是文件' };
  }
  if (expectedSize > 0 && stat.size !== expectedSize) {
    return { ok: false, message: `安装包大小不符（期望 ${expectedSize}，实际 ${stat.size}），可能下载不完整` };
  }

  const kind = packageKindFromPath(filePath);
  if (kind === 'unknown') {
    return { ok: false, message: '不支持的安装包格式（仅支持 exe / deb）' };
  }

  let header: Buffer;
  try {
    const fd = fs.openSync(filePath, 'r');
    header = Buffer.alloc(8);
    fs.readSync(fd, header, 0, 8, 0);
    fs.closeSync(fd);
  } catch (err) {
    return { ok: false, message: `读取安装包头部失败：${(err as Error).message}` };
  }

  const expected = kind === 'exe' ? EXE_MAGIC : DEB_MAGIC;
  if (header.slice(0, expected.length).equals(expected)) {
    return { ok: true };
  }
  return {
    ok: false,
    message: kind === 'exe' ? '安装包不是有效的 Windows 程序（缺少 MZ 头）' : '安装包不是有效的 deb 归档（缺少 !<arch> 头）',
  };
}
