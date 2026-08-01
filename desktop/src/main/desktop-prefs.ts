/**
 * Desktop preference helpers shared by Electron main-process modules.
 */
import * as fs from 'fs';
import * as path from 'path';
import { app } from 'electron';

export type CloseBehavior = 'tray' | 'quit' | 'ask';

export interface DesktopPrefs {
  closeBehavior?: CloseBehavior;
  themeMode?: 'system' | 'light' | 'dark';
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
  fs.mkdirSync(path.dirname(desktopPrefsPath()), { recursive: true });
  fs.writeFileSync(desktopPrefsPath(), JSON.stringify(next, null, 2), 'utf8');
}

export function normalizeCloseBehavior(value: unknown): CloseBehavior {
  return value === 'quit' || value === 'ask' || value === 'tray' ? value : DEFAULT_CLOSE_BEHAVIOR;
}

/**
 * Update close behavior with read-modify-write semantics so other preference
 * namespaces sharing desktop-prefs.json are preserved.
 */
export function saveCloseBehaviorPreference(behavior: unknown): { closeBehavior: CloseBehavior } {
  const parsed = readDesktopPrefsFile();
  const closeBehavior = normalizeCloseBehavior(behavior);
  writeDesktopPrefsFile({ ...parsed, closeBehavior });
  return { closeBehavior };
}
