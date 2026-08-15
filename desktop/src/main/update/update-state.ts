/**
 * 版本更新持久化状态（update-state.json）。
 *
 * 仿 desktop-prefs.ts 的整文件读写范式，单独成文件（与 closeBehavior / JWT 等
 * prefs 隔离，避免高频写放大）。承载两份跨重启状态：
 *   downloaded —— 已完整下载、可随时安装的更新包（含签名 metadata/digest 与文件 identity）。
 *   forceLock  —— force 策略阻断锁，更新成功并重启、本机版本达标后清除。
 */
import * as fs from 'fs';
import * as path from 'path';
import { randomBytes } from 'crypto';
import { app } from 'electron';
import {
  MAX_UPDATE_PACKAGE_BYTES,
  MAX_UPDATE_SIGNATURE_BYTES,
  MAX_UPDATE_STATE_BYTES,
  createSecureExclusiveFile,
  ensurePrivateUpdateDirectory,
  finalizeOpenFile,
  readSecureFile,
  writeAllToFile,
} from './update-file-security';
import { normalizeVersion } from './update-url';
import type {
  DownloadedUpdateRecord,
  ForceLockRecord,
  PersistedUpdateSignatureMetadata,
  UpdateFileIdentity,
  UpdateStateSnapshot,
} from '../../shared/types';

const FILE_NAME = 'update-state.json';

export function updateStatePath(): string {
  const userData = app.getPath('userData');
  fs.mkdirSync(userData, { recursive: true, mode: 0o700 });
  return path.join(fs.realpathSync.native(userData), FILE_NAME);
}

const EMPTY_STATE: UpdateStateSnapshot = { downloaded: null, forceLock: null };

export function readUpdateState(): UpdateStateSnapshot {
  try {
    const { bytes } = readSecureFile(
      updateStatePath(),
      MAX_UPDATE_STATE_BYTES,
      'update state',
    );
    const parsed: unknown = JSON.parse(
      new TextDecoder('utf-8', { fatal: true }).decode(bytes),
    );
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('state root must be an object');
    }
    const record = parsed as Record<string, unknown>;
    if (
      Object.keys(record).sort().join(',') !== 'downloaded,forceLock'
    ) {
      throw new Error('state root fields are invalid');
    }
    const downloaded = record['downloaded'] === null
      ? null
      : (isDownloadedRecord(record['downloaded']) ? record['downloaded'] : null);
    const forceLock = record['forceLock'] === null
      ? null
      : (isForceLockRecord(record['forceLock']) ? record['forceLock'] : null);
    if (record['downloaded'] !== null && downloaded === null) {
      console.warn('[update-security] state-downloaded-record-rejected');
    }
    if (record['forceLock'] !== null && forceLock === null) {
      console.warn('[update-security] state-force-lock-rejected');
    }
    return {
      downloaded,
      forceLock,
    };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
      console.warn('[update-security] state-read-rejected');
    }
    return { ...EMPTY_STATE };
  }
}

export function writeUpdateState(next: UpdateStateSnapshot): void {
  if (
    (next.downloaded !== null && !isDownloadedRecord(next.downloaded))
    || (next.forceLock !== null && !isForceLockRecord(next.forceLock))
  ) {
    throw new Error('refusing to persist an invalid update state');
  }

  const file = updateStatePath();
  const directory = path.dirname(file);
  const directoryBefore = fs.lstatSync(directory, { bigint: true });
  if (directoryBefore.isSymbolicLink() || !directoryBefore.isDirectory()) {
    throw new Error('update state directory must be a real directory');
  }
  assertExistingStatePathSafe(file);

  const serialized = Buffer.from(JSON.stringify(next, null, 2), 'utf8');
  if (serialized.length > MAX_UPDATE_STATE_BYTES) {
    throw new Error('update state exceeds its size limit');
  }
  const temporaryFile = `${file}.${process.pid}.${randomBytes(12).toString('hex')}.tmp`;
  const temporary = createSecureExclusiveFile(temporaryFile);
  try {
    writeAllToFile(temporary.fd, serialized);
    finalizeOpenFile(
      temporaryFile,
      temporary.fd,
      temporary.identity,
      MAX_UPDATE_STATE_BYTES,
      'temporary update state',
    );
    fs.closeSync(temporary.fd);
    fs.renameSync(temporaryFile, file);

    const directoryAfter = fs.lstatSync(directory, { bigint: true });
    if (
      directoryAfter.isSymbolicLink()
      || !directoryAfter.isDirectory()
      || directoryAfter.dev !== directoryBefore.dev
      || directoryAfter.ino !== directoryBefore.ino
    ) {
      throw new Error('update state directory changed during atomic write');
    }
    if (process.platform !== 'win32') {
      const directoryFd = fs.openSync(directory, fs.constants.O_RDONLY);
      try {
        fs.fsyncSync(directoryFd);
      } finally {
        fs.closeSync(directoryFd);
      }
    }

    const written = readSecureFile(file, MAX_UPDATE_STATE_BYTES, 'update state');
    if (!written.bytes.equals(serialized)) {
      throw new Error('update state changed immediately after atomic write');
    }
  } catch (error) {
    try {
      fs.closeSync(temporary.fd);
    } catch {
      // The descriptor may already have been closed before rename.
    }
    try {
      fs.unlinkSync(temporaryFile);
    } catch {
      // Preserve the primary write failure.
    }
    console.warn('[update-security] state-write-rejected');
    throw error;
  }
}

function assertExistingStatePathSafe(file: string): void {
  try {
    const info = fs.lstatSync(file);
    if (info.isSymbolicLink() || !info.isFile() || info.nlink !== 1) {
      throw new Error('existing update state is not a private regular file');
    }
    if (
      process.platform !== 'win32'
      && typeof process.getuid === 'function'
      && info.uid !== process.getuid()
    ) {
      throw new Error('existing update state has an unexpected owner');
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
}

/** 读-改-写：只更新 downloaded，保留 forceLock。传 null 清除。 */
export function setDownloadedRecord(record: DownloadedUpdateRecord | null): UpdateStateSnapshot {
  const current = readUpdateState();
  const next: UpdateStateSnapshot = { ...current, downloaded: record };
  writeUpdateState(next);
  return next;
}

/** 读-改-写：只更新 forceLock，保留 downloaded。传 null 清除。 */
export function setForceLock(record: ForceLockRecord | null): UpdateStateSnapshot {
  const current = readUpdateState();
  const next: UpdateStateSnapshot = { ...current, forceLock: record };
  writeUpdateState(next);
  return next;
}

export function clearUpdateState(): void {
  writeUpdateState({ ...EMPTY_STATE });
}

function isDownloadedRecord(value: unknown): value is DownloadedUpdateRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const r = value as Record<string, unknown>;
  const allowedKeys = [
    'filePath',
    'message',
    'packageIdentity',
    'packageSha256',
    'schema',
    'signatureIdentity',
    'signatureMetadata',
    'signatureSha256',
    'size',
    'type',
    'version',
  ];
  const keys = Object.keys(r).sort();
  if (
    keys.some((key) => !allowedKeys.includes(key))
    || allowedKeys.filter((key) => key !== 'message').some((key) => !keys.includes(key))
  ) {
    return false;
  }
  return r['schema'] === 1
    && typeof r['filePath'] === 'string'
    && isManagedUpdatePath(r['filePath'])
    && typeof r['version'] === 'string'
    && normalizeVersion(r['version']) === r['version']
    && typeof r['size'] === 'number'
    && Number.isSafeInteger(r['size'])
    && r['size'] > 0
    && r['size'] <= MAX_UPDATE_PACKAGE_BYTES
    && (r['type'] === 'force' || r['type'] === 'reminder')
    && (r['message'] === undefined
      || (typeof r['message'] === 'string' && r['message'].length <= 2_000))
    && typeof r['packageSha256'] === 'string'
    && /^[0-9a-f]{64}$/.test(r['packageSha256'])
    && typeof r['signatureSha256'] === 'string'
    && /^[0-9a-f]{64}$/.test(r['signatureSha256'])
    && isSignatureMetadata(r['signatureMetadata'])
    && r['signatureMetadata'].version === r['version']
    && r['signatureMetadata'].filename === path.basename(r['filePath'])
    && r['signatureMetadata'].package_size === r['size']
    && r['signatureMetadata'].package_sha256 === r['packageSha256']
    && isUpdateFileIdentity(r['packageIdentity'])
    && r['packageIdentity'].size === r['size']
    && isUpdateFileIdentity(r['signatureIdentity'])
    && r['signatureIdentity'].size > 0
    && r['signatureIdentity'].size <= MAX_UPDATE_SIGNATURE_BYTES;
}

function isForceLockRecord(value: unknown): value is ForceLockRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const r = value as Record<string, unknown>;
  const keys = Object.keys(r).sort();
  if (
    keys.some((key) => key !== 'requiredVersion' && key !== 'message')
    || !keys.includes('requiredVersion')
  ) {
    return false;
  }
  return typeof r['requiredVersion'] === 'string'
    && r['requiredVersion'].length <= 128
    && normalizeVersion(r['requiredVersion']) !== null
    && (r['message'] === undefined
      || (typeof r['message'] === 'string' && r['message'].length <= 2_000));
}

function isManagedUpdatePath(filePath: string): boolean {
  if (!path.isAbsolute(filePath) || filePath.includes('\0')) return false;
  const extension = path.extname(filePath).toLowerCase();
  if (extension !== '.exe' && extension !== '.deb' && extension !== '.dmg') return false;
  try {
    const directory = path.join(path.dirname(updateStatePath()), 'updates');
    ensurePrivateUpdateDirectory(directory);
    return path.dirname(path.resolve(filePath)) === path.resolve(directory)
      && path.basename(filePath) === path.basename(path.resolve(filePath));
  } catch {
    return false;
  }
}

function isSignatureMetadata(value: unknown): value is PersistedUpdateSignatureMetadata {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const metadata = value as Record<string, unknown>;
  return Object.keys(metadata).sort().join(',')
      === 'filename,package_sha256,package_size,schema,version'
    && metadata['schema'] === 1
    && typeof metadata['version'] === 'string'
    && normalizeVersion(metadata['version']) === metadata['version']
    && typeof metadata['filename'] === 'string'
    && metadata['filename'].length > 0
    && metadata['filename'].length <= 255
    && path.basename(metadata['filename']) === metadata['filename']
    && typeof metadata['package_sha256'] === 'string'
    && /^[0-9a-f]{64}$/.test(metadata['package_sha256'])
    && typeof metadata['package_size'] === 'number'
    && Number.isSafeInteger(metadata['package_size'])
    && metadata['package_size'] > 0
    && metadata['package_size'] <= MAX_UPDATE_PACKAGE_BYTES;
}

function isUpdateFileIdentity(value: unknown): value is UpdateFileIdentity {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const identity = value as Record<string, unknown>;
  return Object.keys(identity).sort().join(',')
      === 'ctimeNs,device,inode,mtimeNs,size'
    && typeof identity['device'] === 'string'
    && /^\d+$/.test(identity['device'])
    && typeof identity['inode'] === 'string'
    && /^\d+$/.test(identity['inode'])
    && typeof identity['size'] === 'number'
    && Number.isSafeInteger(identity['size'])
    && identity['size'] >= 0
    && typeof identity['mtimeNs'] === 'string'
    && /^\d+$/.test(identity['mtimeNs'])
    && typeof identity['ctimeNs'] === 'string'
    && /^\d+$/.test(identity['ctimeNs']);
}
