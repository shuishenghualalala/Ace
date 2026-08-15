import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  MAX_UPDATE_PACKAGE_BYTES,
  createSecureExclusiveFile,
  ensurePrivateUpdateDirectory,
  finalizeOpenFile,
  openSecureResumeFile,
  publishOpenFileExclusive,
  snapshotSecureFile,
  writeAllToFile,
} from '../../src/main/update/update-file-security';

const roots: string[] = [];

function temporaryRoot(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'update-file-security-'));
  roots.push(root);
  return root;
}

afterEach(() => {
  for (const root of roots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

describe('secure update files', () => {
  it('creates private update directories and exclusive files', () => {
    const root = temporaryRoot();
    const directory = path.join(root, 'updates');
    ensurePrivateUpdateDirectory(directory);
    const file = path.join(directory, 'package.part');
    const opened = createSecureExclusiveFile(file);
    fs.closeSync(opened.fd);

    expect(() => createSecureExclusiveFile(file)).toThrow();
    if (process.platform !== 'win32') {
      expect(fs.statSync(directory).mode & 0o777).toBe(0o700);
      expect(fs.statSync(file).mode & 0o777).toBe(0o600);
    }
  });

  it('rejects symlink and hardlink artifacts', () => {
    if (process.platform === 'win32') return;
    const root = temporaryRoot();
    const realDirectory = path.join(root, 'real-updates');
    const linkedDirectory = path.join(root, 'updates');
    fs.mkdirSync(realDirectory);
    fs.symlinkSync(realDirectory, linkedDirectory, 'dir');
    expect(() => ensurePrivateUpdateDirectory(linkedDirectory)).toThrow(/real directory/);

    const victim = path.join(root, 'victim.exe');
    const symlink = path.join(root, 'symlink.exe');
    const hardlink = path.join(root, 'hardlink.exe');
    fs.writeFileSync(victim, Buffer.from('MZpayload'));
    fs.symlinkSync(victim, symlink);

    expect(() => snapshotSecureFile(
      symlink,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    )).toThrow(/regular file/);
    expect(() => createSecureExclusiveFile(symlink)).toThrow();

    fs.linkSync(victim, hardlink);
    expect(() => snapshotSecureFile(
      victim,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    )).toThrow(/exactly 1 link/);
    expect(fs.readFileSync(victim, 'utf8')).toBe('MZpayload');
  });

  it('detects replacement between open and publication', () => {
    const root = temporaryRoot();
    const partial = path.join(root, 'package.part');
    const displaced = path.join(root, 'original.part');
    const opened = createSecureExclusiveFile(partial);
    writeAllToFile(opened.fd, Buffer.from('MZverified'));
    try {
      fs.renameSync(partial, displaced);
    } catch (error) {
      // Windows commonly denies replacement while the verified descriptor is open.
      expect((error as NodeJS.ErrnoException).code).toMatch(/^(?:EACCES|EPERM)$/);
      fs.closeSync(opened.fd);
      return;
    }
    fs.writeFileSync(partial, Buffer.from('MZattacker'));

    expect(() => finalizeOpenFile(
      partial,
      opened.fd,
      opened.identity,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    )).toThrow(/replaced/);
    fs.closeSync(opened.fd);
  });

  it('publishes only the open inode and refuses an occupied destination', () => {
    const root = temporaryRoot();
    const partial = path.join(root, 'package.part');
    const target = path.join(root, 'package.exe');
    const opened = createSecureExclusiveFile(partial);
    writeAllToFile(opened.fd, Buffer.from('MZverified'));
    fs.writeFileSync(target, Buffer.from('MZattacker'));

    expect(() => publishOpenFileExclusive(
      partial,
      target,
      opened.fd,
      opened.identity,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    )).toThrow(/already exists/);
    expect(fs.readFileSync(target, 'utf8')).toBe('MZattacker');
    fs.closeSync(opened.fd);
  });

  it('publishes an exclusive target while retaining the verified descriptor', () => {
    const root = temporaryRoot();
    const partial = path.join(root, 'safe.part');
    const target = path.join(root, 'safe.exe');
    const opened = createSecureExclusiveFile(partial);
    writeAllToFile(opened.fd, Buffer.from('MZverified'));

    const published = publishOpenFileExclusive(
      partial,
      target,
      opened.fd,
      opened.identity,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    );

    expect(fs.existsSync(partial)).toBe(false);
    expect(fs.readFileSync(target, 'utf8')).toBe('MZverified');
    expect(snapshotSecureFile(
      target,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    ).identity).toEqual(published);
    fs.closeSync(opened.fd);
  });

  it('rejects resume after content or inode tampering', () => {
    const root = temporaryRoot();
    const partial = path.join(root, 'package.part');
    const opened = createSecureExclusiveFile(partial);
    writeAllToFile(opened.fd, Buffer.from('MZprefix'));
    finalizeOpenFile(
      partial,
      opened.fd,
      opened.identity,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    );
    fs.closeSync(opened.fd);
    const snapshot = snapshotSecureFile(
      partial,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    );

    fs.appendFileSync(partial, Buffer.from('tamper'));
    expect(() => openSecureResumeFile(
      partial,
      snapshot,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    )).toThrow(/identity changed/);

    fs.unlinkSync(partial);
    fs.writeFileSync(partial, Buffer.from('MZprefix'));
    expect(() => openSecureResumeFile(
      partial,
      snapshot,
      MAX_UPDATE_PACKAGE_BYTES,
      'package',
    )).toThrow(/identity changed/);
  });
});
