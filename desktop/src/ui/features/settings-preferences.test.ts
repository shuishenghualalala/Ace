import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SETTINGS,
  clampNumber,
  hydrateSettings,
  resolveFontFamily,
  resolveFontFamilyOverride,
  resolveThemeMode,
} from './settings-preferences';

describe('settings-preferences', () => {
  it('migrates legacy fontSize into ui/content sizes', () => {
    const settings = hydrateSettings({ fontSize: 16, themeMode: 'dark' });
    expect(settings.uiFontSize).toBe(16);
    expect(settings.contentFontSize).toBe(16);
    expect(settings).not.toHaveProperty('editorFontSize');
    expect(settings.terminalFontSize).toBe(DEFAULT_SETTINGS.terminalFontSize);
    expect(settings.themeMode).toBe('dark');
  });

  it('clamps font sizes into supported ranges', () => {
    const settings = hydrateSettings({
      uiFontSize: 99,
      contentFontSize: 2,
      terminalFontSize: 100,
    });
    expect(settings.uiFontSize).toBe(18);
    expect(settings.contentFontSize).toBe(12);
    expect(settings.terminalFontSize).toBe(18);
  });

  it('folds the retired editor size into content and drops dead behavior switches', () => {
    const settings = hydrateSettings({
      editorFontSize: 18,
      startWithConnect: false,
      enterToSend: false,
      streaming: false,
    });
    expect(settings.contentFontSize).toBe(18);
    expect(settings).not.toHaveProperty('editorFontSize');
    expect(settings).not.toHaveProperty('startWithConnect');
    expect(settings).not.toHaveProperty('enterToSend');
    expect(settings).not.toHaveProperty('streaming');
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

  it('keeps the system font on the design-token default', () => {
    expect(resolveFontFamilyOverride('system')).toBeNull();
    expect(resolveFontFamilyOverride('noto')).toBe(resolveFontFamily('noto'));
    expect(resolveFontFamilyOverride('inter')).toBe(resolveFontFamily('inter'));
  });

  it('clampNumber falls back on non-finite values', () => {
    expect(clampNumber(Number.NaN, 1, 10, 5)).toBe(5);
    expect(clampNumber(Infinity, 1, 10, 5)).toBe(5);
  });

  it('keeps approved themes and migrates the former high-contrast key', () => {
    expect(hydrateSettings({ themeMode: 'high-contrast' }).themeMode).toBe('high-contrast');
    expect(hydrateSettings({ themeMode: 'hc' }).themeMode).toBe('high-contrast');
    expect(resolveThemeMode('high-contrast', false)).toBe('dark');
  });

  it('drops legacy accent and rejects retired or unknown themes', () => {
    expect(hydrateSettings({ accent: 'violet' })).not.toHaveProperty('accent');
    expect(hydrateSettings({ themeMode: 'sepia' }).themeMode).toBe('system');
    expect(hydrateSettings({ themeMode: 'sidebar-gray' }).themeMode).toBe('system');
    expect(hydrateSettings({}).themeMode).toBe('system');
    expect(hydrateSettings({ themeMode: 'unknown' }).themeMode).toBe('system');
  });
});
