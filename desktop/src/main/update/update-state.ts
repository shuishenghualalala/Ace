/**
 * 版本更新持久化状态（update-state.json）。
 *
 * 仿 desktop-prefs.ts 的整文件读写范式，单独成文件（与 closeBehavior / JWT 等
 * prefs 隔离，避免高频写放大）。承载两份跨重启状态：
 *   downloaded —— 已完整下载、可随时安装的更新包（含 magic-byte 校验所需 size）。
 *   forceLock  —— force 策略阻断锁，更新成功并重启、本机版本达标后清除。
 */
import * as fs from 'fs';
import * as path from 'path';
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
  try {
    fs.mkdirSync(path.dirname(updateStatePath()), { recursive: true });
    fs.writeFileSync(updateStatePath(), JSON.stringify(next, null, 2), 'utf8');
  } catch (err) {
    console.warn('[update-state] write failed:', (err as Error).message);
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
