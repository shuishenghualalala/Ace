/**
 * 更新包格式与签名校验。
 *
 * 不需要服务端改动，只抓最常见的"下载坏了"——截断 / 后缀错配。
 * - 完整性：落盘字节数与下载记录的 size 一致。
 * - 格式 magic bytes：exe 必须是 PE（`MZ`）；deb 必须是 ar 归档（`!<arch>\n`）。
 *
 * 防篡改由构建时嵌入的 Ed25519 公钥和签名 metadata envelope 提供；
 * envelope 绑定版本、文件名、长度和 SHA-256。没有嵌入信任根时 fail closed。
 */
import * as fs from 'fs';
import * as path from 'path';
import { createHash, createPublicKey, verify } from 'crypto';
import { normalizeVersion } from './update-url';
import {
  MAX_UPDATE_PACKAGE_BYTES,
  MAX_UPDATE_SIGNATURE_BYTES,
  hashSecureOpenFile,
  openSecureReadFile,
  readSecureOpenFile,
  revalidateOpenFile,
  sameFileIdentity,
} from './update-file-security';
import type {
  DownloadedUpdateRecord,
  PersistedUpdateSignatureMetadata,
  UpdateFileIdentity,
} from '../../shared/types';

declare const __ACE_UPDATE_PUBLIC_KEY__: string;

/** PE 可执行文件 magic（前 2 字节 `MZ`）。 */
const EXE_MAGIC = Buffer.from('MZ', 'ascii');
/** Debian .deb 实际是 ar 归档，magic 为 `!<arch>\n`（8 字节）。 */
const DEB_MAGIC = Buffer.from('!<arch>\n', 'ascii');
/** UDIF disk images store `koly` at the start of their final 512-byte trailer. */
const DMG_TRAILER_MAGIC = Buffer.from('koly', 'ascii');
const DMG_TRAILER_SIZE = 512;

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

export type UpdateSignatureMetadata = PersistedUpdateSignatureMetadata;

interface UpdateSignatureEnvelope extends UpdateSignatureMetadata {
  signature: string;
}

export interface VerifiedUpdateArtifact {
  metadata: UpdateSignatureMetadata;
  packageSha256: string;
  signatureSha256: string;
  packageIdentity: UpdateFileIdentity;
  signatureIdentity: UpdateFileIdentity;
}

export interface ArtifactVerificationResult extends IntegrityResult {
  artifact?: VerifiedUpdateArtifact;
}

export interface VerifiedUpdateLease {
  artifact: VerifiedUpdateArtifact;
  packageDescriptor: number;
  revalidate: () => void;
  close: () => void;
}

export function canonicalUpdateSignaturePayload(
  metadata: UpdateSignatureMetadata,
): Buffer {
  return Buffer.from(JSON.stringify({
    filename: metadata.filename,
    package_sha256: metadata.package_sha256,
    package_size: metadata.package_size,
    schema: metadata.schema,
    version: metadata.version,
  }), 'utf8');
}

export function configuredUpdatePublicKey(): string {
  return typeof __ACE_UPDATE_PUBLIC_KEY__ === 'string'
    ? __ACE_UPDATE_PUBLIC_KEY__.trim()
    : '';
}

/**
 * 校验落盘文件：存在 + size 一致 + magic bytes 匹配后缀。
 * expectedSize <= 0 时跳过 size 校验（未知 Content-Length）。
 */
export function verifyPackageIntegrity(filePath: string, expectedSize: number): IntegrityResult {
  let opened: ReturnType<typeof openSecureReadFile> | null = null;
  try {
    opened = openSecureReadFile(
      filePath,
      MAX_UPDATE_PACKAGE_BYTES,
      'update package',
    );
    return verifyOpenPackageIntegrity(
      opened.fd,
      opened.identity,
      filePath,
      expectedSize,
    );
  } catch {
    return { ok: false, message: '安装包完整性校验失败' };
  } finally {
    if (opened) fs.closeSync(opened.fd);
  }
}

function verifyOpenPackageIntegrity(
  descriptor: number,
  identity: UpdateFileIdentity,
  filePath: string,
  expectedSize: number,
): IntegrityResult {
  if (expectedSize > 0 && identity.size !== expectedSize) {
    return {
      ok: false,
      message: `安装包大小不符（期望 ${expectedSize}，实际 ${identity.size}），可能下载不完整`,
    };
  }

  const kind = packageKindFromPath(filePath);
  if (kind === 'unknown') {
    return { ok: false, message: '不支持的安装包格式（仅支持 exe / deb / dmg）' };
  }
  if (kind === 'dmg') {
    if (identity.size < DMG_TRAILER_SIZE) {
      return { ok: false, message: '安装包不是有效的 DMG（缺少 koly trailer）' };
    }
    const trailer = Buffer.alloc(DMG_TRAILER_MAGIC.length);
    const read = fs.readSync(
      descriptor,
      trailer,
      0,
      trailer.length,
      identity.size - DMG_TRAILER_SIZE,
    );
    return read === trailer.length && trailer.equals(DMG_TRAILER_MAGIC)
      ? { ok: true }
      : { ok: false, message: '安装包不是有效的 DMG（缺少 koly trailer）' };
  }

  const header = Buffer.alloc(8);
  const read = fs.readSync(descriptor, header, 0, header.length, 0);
  const expected = kind === 'exe' ? EXE_MAGIC : DEB_MAGIC;
  if (read >= expected.length && header.subarray(0, expected.length).equals(expected)) {
    return { ok: true };
  }
  return {
    ok: false,
    message: kind === 'exe'
      ? '安装包不是有效的 Windows 程序（缺少 MZ 头）'
      : '安装包不是有效的 deb 归档（缺少 !<arch> 头）',
  };
}

function parseSignatureEnvelope(value: string): UpdateSignatureEnvelope {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('更新包签名封装格式无效');
  }
  const envelope = parsed as Record<string, unknown>;
  if (
    Object.keys(envelope).sort().join(',') !==
      'filename,package_sha256,package_size,schema,signature,version'
    || envelope.schema !== 1
    || typeof envelope.version !== 'string'
    || normalizeVersion(envelope.version) !== envelope.version
    || typeof envelope.filename !== 'string'
    || !envelope.filename
    || envelope.filename.length > 255
    || path.basename(envelope.filename) !== envelope.filename
    || typeof envelope.package_sha256 !== 'string'
    || !/^[0-9a-f]{64}$/.test(envelope.package_sha256)
    || typeof envelope.package_size !== 'number'
    || !Number.isSafeInteger(envelope.package_size)
    || envelope.package_size <= 0
    || envelope.package_size > MAX_UPDATE_PACKAGE_BYTES
    || typeof envelope.signature !== 'string'
    || !/^[A-Za-z0-9+/]+={0,2}$/.test(envelope.signature)
  ) {
    throw new Error('更新包签名封装字段无效');
  }
  const signature = Buffer.from(envelope.signature, 'base64');
  if (signature.length !== 64 || signature.toString('base64') !== envelope.signature) {
    throw new Error('更新包签名编码无效');
  }
  return envelope as unknown as UpdateSignatureEnvelope;
}

function metadataFromEnvelope(
  envelope: UpdateSignatureEnvelope,
): UpdateSignatureMetadata {
  return {
    schema: envelope.schema,
    version: envelope.version,
    filename: envelope.filename,
    package_sha256: envelope.package_sha256,
    package_size: envelope.package_size,
  };
}

function sameMetadata(
  left: UpdateSignatureMetadata,
  right: UpdateSignatureMetadata,
): boolean {
  return canonicalUpdateSignaturePayload(left).equals(
    canonicalUpdateSignaturePayload(right),
  );
}

function assertPersistedBinding(
  expected: DownloadedUpdateRecord,
  artifact: VerifiedUpdateArtifact,
): void {
  if (
    expected.schema !== 1
    || expected.size !== artifact.packageIdentity.size
    || expected.packageSha256 !== artifact.packageSha256
    || expected.signatureSha256 !== artifact.signatureSha256
    || !sameMetadata(expected.signatureMetadata, artifact.metadata)
    || !sameFileIdentity(expected.packageIdentity, artifact.packageIdentity)
    || !sameFileIdentity(expected.signatureIdentity, artifact.signatureIdentity)
  ) {
    throw new Error('持久化更新记录与当前安装包不匹配');
  }
}

/**
 * Open and verify both package and signature, retaining descriptors until the
 * caller has performed its final path-identity check immediately before spawn.
 */
export function openVerifiedUpdateArtifact(
  filePath: string,
  signaturePath: string,
  publicKeyValue: string,
  expectedVersion: string,
  expected?: DownloadedUpdateRecord,
): VerifiedUpdateLease {
  const packageFile = openSecureReadFile(
    filePath,
    MAX_UPDATE_PACKAGE_BYTES,
    'update package',
    expected?.packageIdentity,
  );
  let signatureFile: ReturnType<typeof openSecureReadFile> | null = null;
  try {
    signatureFile = openSecureReadFile(
      signaturePath,
      MAX_UPDATE_SIGNATURE_BYTES,
      'update signature',
      expected?.signatureIdentity,
    );
    const integrity = verifyOpenPackageIntegrity(
      packageFile.fd,
      packageFile.identity,
      filePath,
      expected?.size ?? 0,
    );
    if (!integrity.ok) throw new Error(integrity.message || '安装包格式无效');

    const publicKey = publicKeyValue.includes('BEGIN PUBLIC KEY')
      ? createPublicKey(publicKeyValue)
      : createPublicKey({
          key: Buffer.from(publicKeyValue, 'base64'),
          format: 'der',
          type: 'spki',
        });
    if (publicKey.asymmetricKeyType !== 'ed25519') {
      throw new Error('更新包签名公钥不是 Ed25519');
    }
    const signatureBytes = readSecureOpenFile(
      signatureFile.fd,
      signatureFile.identity,
      MAX_UPDATE_SIGNATURE_BYTES,
      'update signature',
    );
    const envelope = parseSignatureEnvelope(
      new TextDecoder('utf-8', { fatal: true }).decode(signatureBytes).trim(),
    );
    const normalizedVersion = normalizeVersion(expectedVersion);
    const packageSha256 = hashSecureOpenFile(
      packageFile.fd,
      packageFile.identity,
      MAX_UPDATE_PACKAGE_BYTES,
      'update package',
    );
    if (
      !normalizedVersion ||
      envelope.version !== normalizedVersion ||
      envelope.filename !== path.basename(filePath) ||
      envelope.package_sha256 !== packageSha256 ||
      envelope.package_size !== packageFile.identity.size
    ) {
      throw new Error('更新包签名封装与请求版本或文件不匹配');
    }
    const signature = Buffer.from(envelope.signature, 'base64');
    if (!verify(
      null,
      canonicalUpdateSignaturePayload(envelope),
      publicKey,
      signature,
    )) {
      throw new Error('更新包签名无效，文件可能已被篡改');
    }

    revalidateOpenFile(
      filePath,
      packageFile.fd,
      packageFile.identity,
      MAX_UPDATE_PACKAGE_BYTES,
      'update package',
    );
    revalidateOpenFile(
      signaturePath,
      signatureFile.fd,
      signatureFile.identity,
      MAX_UPDATE_SIGNATURE_BYTES,
      'update signature',
    );

    const artifact: VerifiedUpdateArtifact = {
      metadata: metadataFromEnvelope(envelope),
      packageSha256,
      signatureSha256: createHash('sha256').update(signatureBytes).digest('hex'),
      packageIdentity: packageFile.identity,
      signatureIdentity: signatureFile.identity,
    };
    if (expected) assertPersistedBinding(expected, artifact);

    let closed = false;
    const close = (): void => {
      if (closed) return;
      closed = true;
      let closeFailed = false;
      try {
        fs.closeSync(signatureFile!.fd);
      } catch {
        closeFailed = true;
      }
      try {
        fs.closeSync(packageFile.fd);
      } catch {
        closeFailed = true;
      }
      if (closeFailed) {
        console.warn('[update-security] verification-descriptor-close-failed');
      }
    };
    return {
      artifact,
      packageDescriptor: packageFile.fd,
      revalidate: () => {
        if (closed) throw new Error('更新包校验租约已关闭');
        revalidateOpenFile(
          filePath,
          packageFile.fd,
          artifact.packageIdentity,
          MAX_UPDATE_PACKAGE_BYTES,
          'update package',
        );
        revalidateOpenFile(
          signaturePath,
          signatureFile!.fd,
          artifact.signatureIdentity,
          MAX_UPDATE_SIGNATURE_BYTES,
          'update signature',
        );
      },
      close,
    };
  } catch (error) {
    if (signatureFile) {
      try {
        fs.closeSync(signatureFile.fd);
      } catch {
        // Preserve the verification failure.
      }
    }
    try {
      fs.closeSync(packageFile.fd);
    } catch {
      // Preserve the verification failure.
    }
    throw error;
  }
}

export function verifyUpdateArtifact(
  filePath: string,
  signaturePath: string,
  publicKeyValue: string,
  expectedVersion: string,
  expected?: DownloadedUpdateRecord,
): ArtifactVerificationResult {
  try {
    const lease = openVerifiedUpdateArtifact(
      filePath,
      signaturePath,
      publicKeyValue,
      expectedVersion,
      expected,
    );
    try {
      return { ok: true, artifact: lease.artifact };
    } finally {
      lease.close();
    }
  } catch {
    return { ok: false, message: '更新包签名校验失败' };
  }
}

/** Verify a signed metadata envelope bound to the requested version and exact package bytes. */
export function verifyPackageSignature(
  filePath: string,
  signaturePath: string,
  publicKeyValue: string,
  expectedVersion: string,
): IntegrityResult {
  const result = verifyUpdateArtifact(
    filePath,
    signaturePath,
    publicKeyValue,
    expectedVersion,
  );
  return result.ok
    ? { ok: true }
    : { ok: false, ...(result.message ? { message: result.message } : {}) };
}
