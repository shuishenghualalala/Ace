/** Checks package size and format before the independent signature verification. */
import * as fs from 'fs';
import * as path from 'path';
import { createPublicKey, verify } from 'crypto';

/** PE 可执行文件 magic（前 2 字节 `MZ`）。 */
const EXE_MAGIC = Buffer.from('MZ', 'ascii');
/** Debian .deb 实际是 ar 归档，magic 为 `!<arch>\n`（8 字节）。 */
const DEB_MAGIC = Buffer.from('!<arch>\n', 'ascii');

const UDIF_TRAILER_BYTES = 512;

export type PackageKind = 'exe' | 'deb' | 'dmg' | 'unknown';

export function packageKindFromPath(filePath: string): PackageKind {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === '.exe') return 'exe';
  if (ext === '.deb') return 'deb';
  if (ext === '.dmg') return 'dmg';
  return 'unknown';
}

export interface IntegrityResult {
  ok: boolean;
  message?: string;
}

export function configuredUpdatePublicKey(): string {
  return String(process.env['ACE_UPDATE_PUBLIC_KEY'] ?? '').trim();
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
    return { ok: false, message: '不支持的安装包格式（仅支持 exe / deb / dmg）' };
  }

  try {
    const fd = fs.openSync(filePath, 'r');
    try {
      if (kind === 'dmg') {
        // The UDZO images emitted by hdiutil have a version-4 UDIF trailer.
        const trailer = Buffer.alloc(12);
        const position = stat.size - UDIF_TRAILER_BYTES;
        if (position > 0 && fs.readSync(fd, trailer, 0, trailer.length, position) === trailer.length
          && trailer.toString('ascii', 0, 4) === 'koly'
          && trailer.readUInt32BE(4) === 4 && trailer.readUInt32BE(8) === UDIF_TRAILER_BYTES) {
          return { ok: true };
        }
        return { ok: false, message: '安装包缺少有效的 UDIF 磁盘映像尾部' };
      }
      const expected = kind === 'exe' ? EXE_MAGIC : DEB_MAGIC;
      const header = Buffer.alloc(expected.length);
      if (fs.readSync(fd, header, 0, header.length, 0) === header.length && header.equals(expected)) {
        return { ok: true };
      }
      return { ok: false, message: kind === 'exe' ? '安装包缺少 Windows MZ 头' : '安装包缺少 deb !<arch> 头' };
    } finally {
      fs.closeSync(fd);
    }
  } catch (err) {
    return { ok: false, message: `读取安装包失败：${(err as Error).message}` };
  }
}

/** Verify a detached base64 Ed25519 signature against the exact package bytes. */
export function verifyPackageSignature(
  filePath: string,
  signaturePath: string,
  publicKeyValue: string,
): IntegrityResult {
  try {
    const publicKey = publicKeyValue.includes('BEGIN PUBLIC KEY')
      ? createPublicKey(publicKeyValue)
      : createPublicKey({
          key: Buffer.from(publicKeyValue, 'base64'),
          format: 'der',
          type: 'spki',
        });
    const signatureText = fs.readFileSync(signaturePath, 'utf8').trim();
    const signature = Buffer.from(signatureText, 'base64');
    if (!signatureText || signature.length === 0) {
      return { ok: false, message: '更新包签名为空' };
    }
    const valid = verify(null, fs.readFileSync(filePath), publicKey, signature);
    return valid
      ? { ok: true }
      : { ok: false, message: '更新包签名无效，文件可能已被篡改' };
  } catch (error) {
    return { ok: false, message: `更新包签名校验失败：${(error as Error).message}` };
  }
}
