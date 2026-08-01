import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  tmpDir: '',
}));

vi.mock('electron', () => ({ app: { getPath: () => state.tmpDir } }));

import { desktopPrefsPath, saveCloseBehaviorPreference } from '../../src/main/desktop-prefs';

beforeEach(() => {
  state.tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-prefs-'));
});

describe('desktop prefs', () => {
  it('preserves unrelated preferences when saving close behavior', () => {
    fs.writeFileSync(
      desktopPrefsPath(),
      JSON.stringify({
        customPreference: 'keep-me',
        themeMode: 'dark',
      }),
    );

    const result = saveCloseBehaviorPreference('quit');

    const raw = JSON.parse(fs.readFileSync(desktopPrefsPath(), 'utf8'));
    expect(result).toEqual({ closeBehavior: 'quit' });
    expect(raw.closeBehavior).toBe('quit');
    expect(raw.customPreference).toBe('keep-me');
    expect(raw.themeMode).toBe('dark');
  });

  it('normalizes invalid close behavior without removing existing prefs', () => {
    fs.writeFileSync(desktopPrefsPath(), JSON.stringify({ customPreference: 'keep-me' }));

    const result = saveCloseBehaviorPreference('bad');

    const raw = JSON.parse(fs.readFileSync(desktopPrefsPath(), 'utf8'));
    expect(result).toEqual({ closeBehavior: 'tray' });
    expect(raw.closeBehavior).toBe('tray');
    expect(raw.customPreference).toBe('keep-me');
  });
});
