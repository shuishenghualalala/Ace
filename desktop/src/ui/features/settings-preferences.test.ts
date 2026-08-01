import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SETTINGS,
  clampNumber,
  hydrateSettings,
  resolveFontFamily,
  resolveThemeMode,
} from './settings-preferences';

describe('settings-preferences', () => {
  it('migrates legacy fontSize into ui/content/editor sizes', () => {
    const settings = hydrateSettings({ fontSize: 16, themeMode: 'dark' });
    expect(settings.uiFontSize).toBe(16);
    expect(settings.contentFontSize).toBe(16);
    expect(settings.editorFontSize).toBe(16);
    expect(settings.terminalFontSize).toBe(DEFAULT_SETTINGS.terminalFontSize);
    expect(settings.themeMode).toBe('dark');
  });

  it('clamps font sizes into supported ranges', () => {
    const settings = hydrateSettings({
      uiFontSize: 99,
      contentFontSize: 2,
      editorFontSize: 19.6,
      terminalFontSize: 100,
    });
    expect(settings.uiFontSize).toBe(18);
    expect(settings.contentFontSize).toBe(12);
    expect(settings.editorFontSize).toBe(20);
    expect(settings.terminalFontSize).toBe(18);
  });

  it('resolves system theme from prefers-color-scheme', () => {
    expect(resolveThemeMode('system', true)).toBe('dark');
    expect(resolveThemeMode('system', false)).toBe('light');
    expect(resolveThemeMode('light', true)).toBe('light');
    expect(resolveThemeMode('dark', false)).toBe('dark');
  });

  it('resolves configured font families to concrete stacks', () => {
    expect(resolveFontFamily('system')).toContain('system-ui');
    expect(resolveFontFamily('noto')).toContain('Noto Sans SC');
    expect(resolveFontFamily('inter')).toContain('Inter');
  });

  it('clampNumber falls back on non-finite values', () => {
    expect(clampNumber(Number.NaN, 1, 10, 5)).toBe(5);
    expect(clampNumber(Infinity, 1, 10, 5)).toBe(5);
  });

  // Coverage for the accent + theme fields that
  // are now read by settings.ts:applyAccent / applyTheme.
  it('falls back to "blue" when accent is missing or unknown', () => {
    expect(hydrateSettings({}).accent).toBe('blue');
    expect(hydrateSettings({ accent: 'magenta' as unknown as 'blue' }).accent).toBe('blue');
  });

  it('preserves a valid accent value through hydration', () => {
    expect(hydrateSettings({ accent: 'violet' }).accent).toBe('violet');
    expect(hydrateSettings({ accent: 'cyan' }).accent).toBe('cyan');
    expect(hydrateSettings({ accent: 'indigo' }).accent).toBe('indigo');
  });

  it('falls back to "system" when theme is missing or unknown', () => {
    expect(hydrateSettings({}).themeMode).toBe('system');
    expect(hydrateSettings({ themeMode: 'high-contrast' as unknown as 'system' }).themeMode).toBe('system');
  });
});
