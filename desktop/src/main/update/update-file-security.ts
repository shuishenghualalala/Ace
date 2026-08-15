import * as fs from 'fs';
import { createHash } from 'crypto';
import type { UpdateFileIdentity } from '../../shared/types';

export const MAX_UPDATE_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024;
export const MAX_UPDATE_SIGNATURE_BYTES = 16 * 1024;
export const MAX_UPDATE_STATE_BYTES = 64 * 1024;

export interface SecureFileSnapshot {
  identity: UpdateFileIdentity;
  sha256: string;
}

export interface SecureOpenFile {
  fd: number;
  identity: UpdateFileIdentity;
}

function noFollowFlag(): number {
  if (process.platform === 'win32') return 0;
  return (fs.constants as unknown as Record<string, number>).O_NOFOLLOW ?? 0;
}

function assertOwnedByCurrentUser(info: fs.BigIntStats, label: string): void {
  if (process.platform === 'win32' || typeof process.getuid !== 'function') return;
  if (info.uid !== BigInt(process.getuid())) {
    throw new Error(`${label} owner is not the current user`);
  }
}

function assertBoundedRegularFile(
  info: fs.BigIntStats,
  maxBytes: number,
  label: string,
  allowedLinkCount = 1n,
): void {
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new Error(`${label} must be a regular file`);
  }
  if (info.nlink !== allowedLinkCount) {
    throw new Error(`${label} must have exactly ${allowedLinkCount.toString()} link`);
  }
  if (info.size < 0n || info.size > BigInt(maxBytes)) {
    throw new Error(`${label} exceeds its size limit`);
  }
  assertOwnedByCurrentUser(info, label);
}

export function identityFromStats(info: fs.BigIntStats): UpdateFileIdentity {
  if (info.size > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new Error('file size cannot be represented safely');
  }
  return {
    device: info.dev.toString(),
    inode: info.ino.toString(),
    size: Number(info.size),
    mtimeNs: info.mtimeNs.toString(),
    ctimeNs: info.ctimeNs.toString(),
  };
}

export function sameFileObject(
  left: UpdateFileIdentity,
  right: UpdateFileIdentity,
): boolean {
  return left.device === right.device && left.inode === right.inode;
}

export function sameFileIdentity(
  left: UpdateFileIdentity,
  right: UpdateFileIdentity,
): boolean {
  return sameFileObject(left, right)
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

function assertExpectedIdentity(
  actual: UpdateFileIdentity,
  expected: UpdateFileIdentity | undefined,
  label: string,
  exact: boolean,
): void {
  if (!expected) return;
  const matches = exact
    ? sameFileIdentity(actual, expected)
    : sameFileObject(actual, expected);
  if (!matches) throw new Error(`${label} identity changed`);
}

function lstatSecureFile(
  filePath: string,
  maxBytes: number,
  label: string,
  expected?: UpdateFileIdentity,
): fs.BigIntStats {
  const before = fs.lstatSync(filePath, { bigint: true });
  assertBoundedRegularFile(before, maxBytes, label);
  assertExpectedIdentity(identityFromStats(before), expected, label, true);
  return before;
}

function assertPathReferencesOpenFile(
  filePath: string,
  fd: number,
  maxBytes: number,
  label: string,
  expectedObject?: UpdateFileIdentity,
): UpdateFileIdentity {
  const opened = fs.fstatSync(fd, { bigint: true });
  assertBoundedRegularFile(opened, maxBytes, label);
  const openedIdentity = identityFromStats(opened);
  assertExpectedIdentity(openedIdentity, expectedObject, label, false);

  const current = fs.lstatSync(filePath, { bigint: true });
  assertBoundedRegularFile(current, maxBytes, label);
  const currentIdentity = identityFromStats(current);
  if (!sameFileIdentity(openedIdentity, currentIdentity)) {
    throw new Error(`${label} path was replaced`);
  }
  return openedIdentity;
}

function openSecureRegularFile(
  filePath: string,
  flags: number,
  maxBytes: number,
  label: string,
  expected?: UpdateFileIdentity,
): SecureOpenFile {
  const before = lstatSecureFile(filePath, maxBytes, label, expected);
  const fd = fs.openSync(filePath, flags | noFollowFlag());
  try {
    const opened = fs.fstatSync(fd, { bigint: true });
    assertBoundedRegularFile(opened, maxBytes, label);
    const beforeIdentity = identityFromStats(before);
    const openedIdentity = identityFromStats(opened);
    if (!sameFileIdentity(beforeIdentity, openedIdentity)) {
      throw new Error(`${label} changed while opening`);
    }
    assertExpectedIdentity(openedIdentity, expected, label, true);
    return { fd, identity: openedIdentity };
  } catch (error) {
    fs.closeSync(fd);
    throw error;
  }
}

export function openSecureReadFile(
  filePath: string,
  maxBytes: number,
  label: string,
  expected?: UpdateFileIdentity,
): SecureOpenFile {
  return openSecureRegularFile(
    filePath,
    fs.constants.O_RDONLY,
    maxBytes,
    label,
    expected,
  );
}

function hashOpenFile(
  fd: number,
  expectedSize: number,
  maxBytes: number,
  label: string,
): string {
  if (expectedSize < 0 || expectedSize > maxBytes) {
    throw new Error(`${label} exceeds its size limit`);
  }
  const digest = createHash('sha256');
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  let offset = 0;
  while (offset < expectedSize) {
    const read = fs.readSync(
      fd,
      buffer,
      0,
      Math.min(buffer.length, expectedSize - offset),
      offset,
    );
    if (read <= 0) throw new Error(`${label} ended before its recorded size`);
    digest.update(buffer.subarray(0, read));
    offset += read;
  }
  const extra = Buffer.allocUnsafe(1);
  if (fs.readSync(fd, extra, 0, 1, expectedSize) !== 0) {
    throw new Error(`${label} grew while being read`);
  }
  return digest.digest('hex');
}

export function hashSecureOpenFile(
  fd: number,
  identity: UpdateFileIdentity,
  maxBytes: number,
  label: string,
): string {
  const current = fs.fstatSync(fd, { bigint: true });
  assertBoundedRegularFile(current, maxBytes, label);
  if (!sameFileIdentity(identityFromStats(current), identity)) {
    throw new Error(`${label} identity changed before hashing`);
  }
  return hashOpenFile(fd, identity.size, maxBytes, label);
}

export function readSecureOpenFile(
  fd: number,
  identity: UpdateFileIdentity,
  maxBytes: number,
  label: string,
): Buffer {
  if (identity.size > maxBytes) throw new Error(`${label} exceeds its size limit`);
  const bytes = Buffer.alloc(identity.size);
  let offset = 0;
  while (offset < bytes.length) {
    const read = fs.readSync(
      fd,
      bytes,
      offset,
      bytes.length - offset,
      offset,
    );
    if (read <= 0) throw new Error(`${label} ended before its recorded size`);
    offset += read;
  }
  const extra = Buffer.allocUnsafe(1);
  if (fs.readSync(fd, extra, 0, 1, bytes.length) !== 0) {
    throw new Error(`${label} grew while being read`);
  }
  const current = fs.fstatSync(fd, { bigint: true });
  assertBoundedRegularFile(current, maxBytes, label);
  if (!sameFileIdentity(identityFromStats(current), identity)) {
    throw new Error(`${label} changed while being read`);
  }
  return bytes;
}

export function ensurePrivateUpdateDirectory(directory: string): void {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const before = fs.lstatSync(directory, { bigint: true });
  if (before.isSymbolicLink() || !before.isDirectory()) {
    throw new Error('update directory must be a real directory');
  }
  assertOwnedByCurrentUser(before, 'update directory');
  if (process.platform !== 'win32' && (Number(before.mode) & 0o777) !== 0o700) {
    fs.chmodSync(directory, 0o700);
  }
  const after = fs.lstatSync(directory, { bigint: true });
  if (
    after.isSymbolicLink()
    || !after.isDirectory()
    || after.dev !== before.dev
    || after.ino !== before.ino
  ) {
    throw new Error('update directory changed during validation');
  }
  assertOwnedByCurrentUser(after, 'update directory');
  if (process.platform !== 'win32' && (Number(after.mode) & 0o777) !== 0o700) {
    throw new Error('update directory permissions must be 0700');
  }
}

export function createSecureExclusiveFile(
  filePath: string,
  mode = 0o600,
): SecureOpenFile {
  const fd = fs.openSync(
    filePath,
    fs.constants.O_RDWR
      | fs.constants.O_CREAT
      | fs.constants.O_EXCL
      | noFollowFlag(),
    mode,
  );
  try {
    if (process.platform !== 'win32') fs.fchmodSync(fd, mode);
    const info = fs.fstatSync(fd, { bigint: true });
    assertBoundedRegularFile(info, 0, 'temporary update file');
    return { fd, identity: identityFromStats(info) };
  } catch (error) {
    fs.closeSync(fd);
    throw error;
  }
}

export function writeAllToFile(fd: number, value: Buffer): void {
  let offset = 0;
  while (offset < value.length) {
    const written = fs.writeSync(fd, value, offset, value.length - offset);
    if (written <= 0) throw new Error('update file write made no progress');
    offset += written;
  }
}

export function snapshotSecureFile(
  filePath: string,
  maxBytes: number,
  label: string,
  expected?: UpdateFileIdentity,
): SecureFileSnapshot {
  const opened = openSecureRegularFile(
    filePath,
    fs.constants.O_RDONLY,
    maxBytes,
    label,
    expected,
  );
  try {
    const sha256 = hashOpenFile(
      opened.fd,
      opened.identity.size,
      maxBytes,
      label,
    );
    const identity = assertPathReferencesOpenFile(
      filePath,
      opened.fd,
      maxBytes,
      label,
      opened.identity,
    );
    if (!sameFileIdentity(identity, opened.identity)) {
      throw new Error(`${label} changed while hashing`);
    }
    return { identity, sha256 };
  } finally {
    fs.closeSync(opened.fd);
  }
}

export function openSecureResumeFile(
  filePath: string,
  expected: SecureFileSnapshot,
  maxBytes: number,
  label: string,
): SecureOpenFile {
  const opened = openSecureRegularFile(
    filePath,
    fs.constants.O_RDWR | fs.constants.O_APPEND,
    maxBytes,
    label,
    expected.identity,
  );
  try {
    const actualDigest = hashOpenFile(
      opened.fd,
      opened.identity.size,
      maxBytes,
      label,
    );
    if (actualDigest !== expected.sha256) {
      throw new Error(`${label} digest changed before resume`);
    }
    return opened;
  } catch (error) {
    fs.closeSync(opened.fd);
    throw error;
  }
}

export function revalidateOpenFile(
  filePath: string,
  fd: number,
  expected: UpdateFileIdentity,
  maxBytes: number,
  label: string,
): UpdateFileIdentity {
  const identity = assertPathReferencesOpenFile(
    filePath,
    fd,
    maxBytes,
    label,
    expected,
  );
  assertExpectedIdentity(identity, expected, label, true);
  return identity;
}

export function finalizeOpenFile(
  filePath: string,
  fd: number,
  expectedObject: UpdateFileIdentity,
  maxBytes: number,
  label: string,
): UpdateFileIdentity {
  fs.fsyncSync(fd);
  return assertPathReferencesOpenFile(
    filePath,
    fd,
    maxBytes,
    label,
    expectedObject,
  );
}

export function publishOpenFileExclusive(
  sourcePath: string,
  targetPath: string,
  fd: number,
  expectedObject: UpdateFileIdentity,
  maxBytes: number,
  label: string,
): UpdateFileIdentity {
  const before = finalizeOpenFile(
    sourcePath,
    fd,
    expectedObject,
    maxBytes,
    label,
  );
  try {
    fs.lstatSync(targetPath);
    throw new Error(`${label} destination already exists`);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }

  let targetCreated = false;
  try {
    fs.linkSync(sourcePath, targetPath);
    targetCreated = true;
    const opened = fs.fstatSync(fd, { bigint: true });
    assertBoundedRegularFile(opened, maxBytes, label, 2n);
    const target = fs.lstatSync(targetPath, { bigint: true });
    assertBoundedRegularFile(target, maxBytes, label, 2n);
    if (!sameFileObject(identityFromStats(opened), identityFromStats(target))) {
      throw new Error(`${label} destination does not reference the verified file`);
    }
    fs.unlinkSync(sourcePath);
    const published = assertPathReferencesOpenFile(
      targetPath,
      fd,
      maxBytes,
      label,
      before,
    );
    if (!sameFileObject(published, before)) {
      throw new Error(`${label} changed while publishing`);
    }
    return published;
  } catch (error) {
    if (targetCreated) {
      try {
        const target = fs.lstatSync(targetPath, { bigint: true });
        const opened = fs.fstatSync(fd, { bigint: true });
        if (target.dev === opened.dev && target.ino === opened.ino) {
          fs.unlinkSync(targetPath);
        }
      } catch {
        // Preserve the primary publication failure.
      }
    }
    throw error;
  }
}

export function readSecureFile(
  filePath: string,
  maxBytes: number,
  label: string,
): { bytes: Buffer; identity: UpdateFileIdentity } {
  const opened = openSecureRegularFile(
    filePath,
    fs.constants.O_RDONLY,
    maxBytes,
    label,
  );
  try {
    const bytes = Buffer.alloc(opened.identity.size);
    let offset = 0;
    while (offset < bytes.length) {
      const read = fs.readSync(
        opened.fd,
        bytes,
        offset,
        bytes.length - offset,
        offset,
      );
      if (read <= 0) throw new Error(`${label} ended before its recorded size`);
      offset += read;
    }
    const extra = Buffer.allocUnsafe(1);
    if (fs.readSync(opened.fd, extra, 0, 1, bytes.length) !== 0) {
      throw new Error(`${label} grew while being read`);
    }
    revalidateOpenFile(
      filePath,
      opened.fd,
      opened.identity,
      maxBytes,
      label,
    );
    return { bytes, identity: opened.identity };
  } finally {
    fs.closeSync(opened.fd);
  }
}

export function removeManagedUpdateFile(
  filePath: string,
  expected?: UpdateFileIdentity,
): void {
  if (expected) {
    const opened = openSecureRegularFile(
      filePath,
      fs.constants.O_RDONLY,
      MAX_UPDATE_PACKAGE_BYTES,
      'update file',
      expected,
    );
    try {
      revalidateOpenFile(
        filePath,
        opened.fd,
        expected,
        MAX_UPDATE_PACKAGE_BYTES,
        'update file',
      );
      fs.unlinkSync(filePath);
    } finally {
      fs.closeSync(opened.fd);
    }
    return;
  }

  try {
    const info = fs.lstatSync(filePath);
    if (!info.isFile() && !info.isSymbolicLink()) {
      throw new Error('managed update path is not a file');
    }
    fs.unlinkSync(filePath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
}
