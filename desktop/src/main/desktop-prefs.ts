/**
 * Desktop preference helpers shared by Electron main-process modules.
 */
import * as fs from 'fs';
import * as path from 'path';
import { app } from 'electron';
import type { UserInfoSnapshot } from '../shared/types';

export type CloseBehavior = 'tray' | 'quit' | 'ask';

export interface DesktopPrefs {
  closeBehavior?: CloseBehavior;
  themeMode?: 'system' | 'light' | 'dark';
  strictSecurityEnabled?: boolean;
  encryptedJwt?: string;
  userInfo?: UserInfoSnapshot;
  [key: string]: unknown;
}

export const DEFAULT_CLOSE_BEHAVIOR: CloseBehavior = 'tray';

export function desktopPrefsPath(): string {
  return path.join(app.getPath('userData'), 'desktop-prefs.json');
}

export function readDesktopPrefsFile(): DesktopPrefs {
  try {
    return JSON.parse(fs.readFileSync(desktopPrefsPath(), 'utf8')) as DesktopPrefs;
  } catch {
    return {};
  }
}

export function writeDesktopPrefsFile(next: DesktopPrefs): void {
  const file = desktopPrefsPath();
  const temporaryFile = `${file}.tmp`;
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  try {
    fs.unlinkSync(temporaryFile);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
  }
  try {
    fs.writeFileSync(temporaryFile, JSON.stringify(next, null, 2), {
      encoding: 'utf8',
      flag: 'wx',
      mode: 0o600,
    });
    fs.renameSync(temporaryFile, file);
  } catch (writeError) {
    try {
      fs.unlinkSync(temporaryFile);
    } catch {
      // Preserve the primary write/rename failure; the next write removes stale temp state first.
    }
    throw writeError;
  }
  try {
    fs.unlinkSync(temporaryFile);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
  }
}

export function normalizeCloseBehavior(value: unknown): CloseBehavior {
  return value === 'quit' || value === 'ask' || value === 'tray' ? value : DEFAULT_CLOSE_BEHAVIOR;
}

/**
 * Update close behavior with read-modify-write semantics so auth credentials
 * and other preference namespaces sharing desktop-prefs.json are preserved.
 */
export function saveCloseBehaviorPreference(behavior: unknown): { closeBehavior: CloseBehavior } {
  const parsed = readDesktopPrefsFile();
  const closeBehavior = normalizeCloseBehavior(behavior);
  writeDesktopPrefsFile({ ...parsed, closeBehavior });
  return { closeBehavior };
}

/** Production security is mandatory; legacy preference files cannot disable it. */
export function isStrictSecurityEnabled(): boolean {
  return true;
}

export function saveStrictSecurityPreference(enabled: boolean): { strictSecurityEnabled: boolean } {
  if (!enabled) {
    throw new Error('strict security cannot be disabled');
  }
  const parsed = readDesktopPrefsFile();
  writeDesktopPrefsFile({ ...parsed, strictSecurityEnabled: true });
  return { strictSecurityEnabled: true };
}
