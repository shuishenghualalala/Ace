/**
 * Settings preference helpers shared by the settings UI and tests.
 */

export interface Settings {
  themeMode: 'system' | 'light' | 'dark' | 'high-contrast';
  uiFontSize: number;
  contentFontSize: number;
  terminalFontSize: number;
  fontFamily: 'system' | 'noto' | 'inter';
  closeBehavior: 'tray' | 'quit' | 'ask';
  autoStart: boolean;
  inspectorOpen: boolean;
  shortcuts: Record<string, string>;
  autoClearCacheDays: number;
}

export const DEFAULT_SETTINGS: Settings = {
  themeMode: 'system',
  uiFontSize: 14,
  contentFontSize: 14,
  terminalFontSize: 12,
  fontFamily: 'system',
  closeBehavior: 'tray',
  autoStart: false,
  inspectorOpen: false,
  shortcuts: {
    '新建对话': '⌘ N',
    '切换侧栏': '⌘ B',
    '全局搜索': '⌘ K',
    '设置': '⌘ ,',
  },
  autoClearCacheDays: 7,
};

export type LegacySettings = Omit<Partial<Settings>, 'themeMode'> & {
  fontSize?: number;
  editorFontSize?: number;
  startWithConnect?: boolean;
  enterToSend?: boolean;
  streaming?: boolean;
  accent?: unknown;
  themeMode?: unknown;
};

export function clampNumber(value: number, min: number, max: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, Math.round(value)));
}

export function normalizeFontSizes(settings: Settings): Settings {
  return {
    ...settings,
    uiFontSize: clampNumber(settings.uiFontSize, 12, 18, DEFAULT_SETTINGS.uiFontSize),
    contentFontSize: clampNumber(settings.contentFontSize, 12, 20, DEFAULT_SETTINGS.contentFontSize),
    terminalFontSize: clampNumber(settings.terminalFontSize, 11, 18, DEFAULT_SETTINGS.terminalFontSize),
  };
}

export function hydrateSettings(parsed?: LegacySettings | null): Settings {
  if (!parsed) return { ...DEFAULT_SETTINGS };
  const {
    accent: legacyAccent,
    fontSize,
    editorFontSize,
    startWithConnect,
    enterToSend,
    streaming,
    themeMode: storedTheme,
    ...supported
  } = parsed;
  void legacyAccent;
  void startWithConnect;
  void enterToSend;
  void streaming;
  const candidateTheme = storedTheme === 'hc' ? 'high-contrast' : storedTheme;
  const themeMode: Settings['themeMode'] =
    candidateTheme === 'system' || candidateTheme === 'light' ||
    candidateTheme === 'dark' || candidateTheme === 'high-contrast'
      ? candidateTheme
      : DEFAULT_SETTINGS.themeMode;
  const merged: Settings = { ...DEFAULT_SETTINGS, ...supported, themeMode };
  if (typeof editorFontSize === 'number' && parsed.contentFontSize == null) {
    merged.contentFontSize = editorFontSize;
  }
  if (typeof fontSize === 'number') {
    merged.uiFontSize = parsed.uiFontSize ?? fontSize;
    merged.contentFontSize = parsed.contentFontSize ?? editorFontSize ?? fontSize;
  }
  return normalizeFontSizes(merged);
}

export function resolveThemeMode(mode: Settings['themeMode'], prefersDark: boolean): 'light' | 'dark' {
  if (mode === 'system') return prefersDark ? 'dark' : 'light';
  if (mode === 'high-contrast') return 'dark';
  return mode;
}

export function resolveFontFamily(fontFamily: Settings['fontFamily']): string {
  const map: Record<Settings['fontFamily'], string> = {
    system: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "PingFang SC", "Microsoft YaHei", "Segoe UI Emoji", "Segoe UI Symbol", "Apple Color Emoji", "Noto Color Emoji", emoji, sans-serif',
    noto: '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", "Segoe UI Emoji", "Segoe UI Symbol", "Apple Color Emoji", "Noto Color Emoji", emoji, sans-serif',
    inter: '"Inter", "Segoe UI", "PingFang SC", "Microsoft YaHei", "Segoe UI Emoji", "Segoe UI Symbol", "Apple Color Emoji", "Noto Color Emoji", emoji, sans-serif',
  };
  return map[fontFamily];
}

export function resolveFontFamilyOverride(fontFamily: Settings['fontFamily']): string | null {
  return fontFamily === 'system' ? null : resolveFontFamily(fontFamily);
}
