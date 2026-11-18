import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  tmpDir: '',
}));

vi.mock('electron', () => ({ app: { getPath: () => state.tmpDir } }));

import {
  desktopPrefsPath,
  isFullAccessChoiceEnabled,
  isStrictSecurityEnabled,
  readSecurityPermissionPreferences,
  saveCloseBehaviorPreference,
  saveDefaultConversationPermissionPreference,
  saveFullAccessChoicePreference,
  saveStrictSecurityPreference,
} from '../../src/main/desktop-prefs';

beforeEach(() => {
  state.tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-prefs-'));
});

describe('desktop prefs', () => {
  it('persists the managed default alongside full-access availability', () => {
    expect(readSecurityPermissionPreferences()).toEqual({ enabled: true });
    saveFullAccessChoicePreference(false);
    saveDefaultConversationPermissionPreference('request_approval');
    expect(readSecurityPermissionPreferences()).toEqual({ enabled: false, defaultMode: 'request_approval' });
    saveDefaultConversationPermissionPreference('auto_review');
    expect(readSecurityPermissionPreferences()).toEqual({ enabled: false, defaultMode: 'auto_review' });
  });

  it('resets a full-access default when the option is disabled', () => {
    saveDefaultConversationPermissionPreference('full_access');
    expect(readSecurityPermissionPreferences()).toEqual({ enabled: true, defaultMode: 'full_access' });
    expect(saveFullAccessChoicePreference(false)).toEqual({ enabled: false, defaultMode: 'auto_review' });
    expect(() => saveDefaultConversationPermissionPreference('full_access')).toThrow();
    expect(saveFullAccessChoicePreference(true)).toEqual({ enabled: true, defaultMode: 'auto_review' });
  });

  it('remembers whether to offer full access without overwriting other preferences', () => {
    fs.writeFileSync(desktopPrefsPath(), JSON.stringify({ themeMode: 'dark' }));
    expect(isFullAccessChoiceEnabled()).toBe(true);
    expect(saveFullAccessChoicePreference(false)).toEqual({ enabled: false });
    expect(isFullAccessChoiceEnabled()).toBe(false);
    expect(JSON.parse(fs.readFileSync(desktopPrefsPath(), 'utf8')).themeMode).toBe('dark');
    expect(saveFullAccessChoicePreference(true)).toEqual({ enabled: true });
    expect(isFullAccessChoiceEnabled()).toBe(true);
  });

  it('defaults to strict security and only accepts an explicit compatibility opt-out', () => {
    expect(isStrictSecurityEnabled()).toBe(true);

    saveStrictSecurityPreference(false);
    expect(isStrictSecurityEnabled()).toBe(false);

    saveStrictSecurityPreference(true);
    expect(isStrictSecurityEnabled()).toBe(true);
  });

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
