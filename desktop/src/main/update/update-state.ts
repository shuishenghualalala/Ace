import * as fs from 'fs';
import * as path from 'path';
import { randomUUID } from 'node:crypto';
import { evaluateVersionUpdate } from '../version-compare';
import { verifyPackageIntegrity } from './update-integrity';
import { app } from 'electron';
import type { DownloadedUpdateRecord, ForceLockRecord, UpdateStateSnapshot } from '../../shared/types';

const FILE_NAME = 'update-state.json';

let cachedStatePath: string | null = null;

export function updateStatePath(): string {
  if (cachedStatePath) return cachedStatePath;
  cachedStatePath = path.join(app.getPath('userData'), FILE_NAME);
  return cachedStatePath;
}

const EMPTY_STATE: UpdateStateSnapshot = { downloaded: null, forceLock: null };

export function readUpdateState(): UpdateStateSnapshot {
  try {
    const parsed = JSON.parse(fs.readFileSync(updateStatePath(), 'utf8')) as Partial<UpdateStateSnapshot>;
    return {
      downloaded: isDownloadedRecord(parsed.downloaded) ? parsed.downloaded : null,
      forceLock: isForceLockRecord(parsed.forceLock) ? parsed.forceLock : null,
    };
  } catch {
    return { ...EMPTY_STATE };
  }
}

export function writeUpdateState(next: UpdateStateSnapshot): void {
  const file = updateStatePath();
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${randomUUID()}.tmp`;
  const fd = fs.openSync(temporary, 'wx', 0o600);
  try {
    try {
      fs.writeFileSync(fd, JSON.stringify(next, null, 2), 'utf8');
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(temporary, file);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

/** Project a saved package against the running app and the current minimum version. */
export function installableUpdateState(localVersion: string): UpdateStateSnapshot {
  const saved = readUpdateState();
  let { downloaded, forceLock } = saved;
  if (forceLock && !evaluateVersionUpdate(forceLock.requiredVersion, localVersion).shouldProcess) {
    forceLock = null;
  }
  if (downloaded && (!evaluateVersionUpdate(downloaded.version, localVersion).shouldProcess
    || !verifyPackageIntegrity(downloaded.filePath, downloaded.size).ok)) {
    downloaded = null;
  }
  if (downloaded !== saved.downloaded || forceLock !== saved.forceLock) writeUpdateState({ downloaded, forceLock });
  if (downloaded && forceLock && evaluateVersionUpdate(forceLock.requiredVersion, downloaded.version).shouldProcess) {
    return { downloaded: null, forceLock };
  }
  return { downloaded, forceLock };
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
  if (!value || typeof value !== 'object') return false;
  const r = value as Record<string, unknown>;
  return typeof r['filePath'] === 'string'
    && typeof r['version'] === 'string'
    && typeof r['size'] === 'number'
    && (r['type'] === 'force' || r['type'] === 'reminder');
}

function isForceLockRecord(value: unknown): value is ForceLockRecord {
  if (!value || typeof value !== 'object') return false;
  const r = value as Record<string, unknown>;
  return typeof r['requiredVersion'] === 'string';
}
