import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { createHash } from 'crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ userData: '' }));

vi.mock('electron', () => ({
  app: {
    getPath: () => state.userData,
  },
}));

import {
  readUpdateState,
  updateStatePath,
  writeUpdateState,
} from '../../src/main/update/update-state';
import {
  MAX_UPDATE_PACKAGE_BYTES,
  ensurePrivateUpdateDirectory,
  snapshotSecureFile,
} from '../../src/main/update/update-file-security';
import type {
  DownloadedUpdateRecord,
  UpdateStateSnapshot,
} from '../../src/shared/types';

const roots: string[] = [];

function createRecord(): DownloadedUpdateRecord {
  const updates = path.join(state.userData, 'updates');
  ensurePrivateUpdateDirectory(updates);
  const packagePath = path.join(updates, 'Crew_Setup_v1.2.3.exe');
  const signaturePath = `${packagePath}.sig`;
  const packageBytes = Buffer.from('MZverified-package');
  const signatureBytes = Buffer.from('signed-envelope');
  fs.writeFileSync(packagePath, packageBytes, { mode: 0o600 });
  fs.writeFileSync(signaturePath, signatureBytes, { mode: 0o600 });
  const packageSnapshot = snapshotSecureFile(
    packagePath,
    MAX_UPDATE_PACKAGE_BYTES,
    'package',
  );
  const signatureSnapshot = snapshotSecureFile(
    signaturePath,
    MAX_UPDATE_PACKAGE_BYTES,
    'signature',
  );
  return {
    schema: 1,
    filePath: packagePath,
    version: '1.2.3',
    size: packageBytes.length,
    type: 'force',
    packageSha256: packageSnapshot.sha256,
    signatureSha256: signatureSnapshot.sha256,
    signatureMetadata: {
      schema: 1,
      version: '1.2.3',
      filename: path.basename(packagePath),
      package_sha256: packageSnapshot.sha256,
      package_size: packageBytes.length,
    },
    packageIdentity: packageSnapshot.identity,
    signatureIdentity: signatureSnapshot.identity,
  };
}

beforeEach(() => {
  state.userData = fs.mkdtempSync(path.join(os.tmpdir(), 'update-state-'));
  roots.push(state.userData);
});

afterEach(() => {
  for (const root of roots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

describe('update state persistence', () => {
  it('atomically round-trips a signature-bound downloaded record', () => {
    const downloaded = createRecord();
    const snapshot: UpdateStateSnapshot = {
      downloaded,
      forceLock: { requiredVersion: '1.2.3', message: 'required' },
    };

    writeUpdateState(snapshot);

    expect(readUpdateState()).toEqual(snapshot);
    const rewritten: UpdateStateSnapshot = {
      downloaded,
      forceLock: null,
    };
    writeUpdateState(rewritten);
    expect(readUpdateState()).toEqual(rewritten);
    expect(
      fs.readdirSync(state.userData).filter((name) => name.endsWith('.tmp')),
    ).toEqual([]);
    if (process.platform !== 'win32') {
      expect(fs.statSync(updateStatePath()).mode & 0o777).toBe(0o600);
    }
  });

  it('fails closed for truncated and legacy path-only state', () => {
    fs.writeFileSync(updateStatePath(), '{"downloaded":', { mode: 0o600 });
    expect(readUpdateState()).toEqual({ downloaded: null, forceLock: null });

    fs.writeFileSync(updateStatePath(), JSON.stringify({
      downloaded: {
        filePath: path.join(state.userData, 'updates', 'legacy.exe'),
        version: '1.2.3',
        size: 10,
        type: 'force',
      },
      forceLock: { requiredVersion: '1.2.3' },
    }), { mode: 0o600 });
    expect(readUpdateState()).toEqual({
      downloaded: null,
      forceLock: { requiredVersion: '1.2.3' },
    });
  });

  it('rejects metadata that no longer binds the package digest', () => {
    const downloaded = createRecord();
    const corrupted = {
      downloaded: {
        ...downloaded,
        packageSha256: createHash('sha256').update('other').digest('hex'),
      },
      forceLock: null,
    };
    fs.writeFileSync(updateStatePath(), JSON.stringify(corrupted), { mode: 0o600 });

    expect(readUpdateState()).toEqual({ downloaded: null, forceLock: null });
  });

  it('does not follow a symlink state file during an atomic write', () => {
    const victim = path.join(state.userData, 'victim.json');
    fs.writeFileSync(victim, 'victim', { mode: 0o600 });
    try {
      fs.symlinkSync(victim, updateStatePath());
    } catch (error) {
      if (
        process.platform === 'win32'
        && /^(?:EPERM|EACCES)$/.test((error as NodeJS.ErrnoException).code || '')
      ) {
        return;
      }
      throw error;
    }

    expect(() => writeUpdateState({
      downloaded: null,
      forceLock: null,
    })).toThrow(/private regular file/);
    expect(fs.readFileSync(victim, 'utf8')).toBe('victim');
  });

  it('rejects hardlinked state instead of trusting shared storage', () => {
    writeUpdateState({ downloaded: null, forceLock: null });
    fs.linkSync(updateStatePath(), path.join(state.userData, 'state-hardlink.json'));

    expect(readUpdateState()).toEqual({ downloaded: null, forceLock: null });
  });
});
