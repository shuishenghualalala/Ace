/**
 * Settings preference helpers shared by the settings UI and tests.
 */

export interface Settings {
  themeMode: 'system' | 'light' | 'dark' | 'sepia' | 'hc';
  accent: 'blue' | 'indigo' | 'violet' | 'cyan';
  uiFontSize: number;
  contentFontSize: number;
  editorFontSize: number;
  terminalFontSize: number;
  fontFamily: 'system' | 'noto' | 'inter';
  startWithConnect: boolean;
  closeBehavior: 'tray' | 'quit' | 'ask';
  autoStart: boolean;
  enterToSend: boolean;
  streaming: boolean;
  inspectorOpen: boolean;
  shortcuts: Record<string, string>;
  autoClearCacheDays: number;
}

export const DEFAULT_SETTINGS: Settings = {
  themeMode: 'system',
  accent: 'blue',
  uiFontSize: 14,
  contentFontSize: 14,
  editorFontSize: 14,
  terminalFontSize: 12,
  fontFamily: 'system',
  startWithConnect: true,
  closeBehavior: 'tray',
  autoStart: false,
  enterToSend: true,
  streaming: true,
  inspectorOpen: false,
  shortcuts: {
    '新建对话': '⌘ N',
    '切换侧栏': '⌘ B',
    '全局搜索': '⌘ K',
    '设置': '⌘ ,',
  },
  autoClearCacheDays: 7,
};

export type LegacySettings = Partial<Settings> & { fontSize?: number };

export function clampNumber(value: number, min: number, max: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, Math.round(value)));
}

export function normalizeFontSizes(settings: Settings): Settings {
  return {
    ...settings,
    uiFontSize: clampNumber(settings.uiFontSize, 12, 18, DEFAULT_SETTINGS.uiFontSize),
    contentFontSize: clampNumber(settings.contentFontSize, 12, 20, DEFAULT_SETTINGS.contentFontSize),
    editorFontSize: clampNumber(settings.editorFontSize, 12, 20, DEFAULT_SETTINGS.editorFontSize),
    terminalFontSize: clampNumber(settings.terminalFontSize, 11, 18, DEFAULT_SETTINGS.terminalFontSize),
  };
}

export function hydrateSettings(parsed?: LegacySettings | null): Settings {
  if (!parsed) return { ...DEFAULT_SETTINGS };
  const merged = { ...DEFAULT_SETTINGS, ...parsed };
  if (typeof parsed.fontSize === 'number') {
    merged.uiFontSize = parsed.uiFontSize ?? parsed.fontSize;
    merged.contentFontSize = parsed.contentFontSize ?? parsed.fontSize;
    merged.editorFontSize = parsed.editorFontSize ?? parsed.fontSize;
  }
  // Reject unknown accent / themeMode values from
  // localStorage (corrupt prefs / schema drift) rather than letting
  // them propagate into applyAccent's map[unknown] lookup or
  // applyTheme's switch.
  if (merged.accent !== 'blue' && merged.accent !== 'indigo' &&
      merged.accent !== 'violet' && merged.accent !== 'cyan') {
    merged.accent = DEFAULT_SETTINGS.accent;
  }
  if (merged.themeMode !== 'system' && merged.themeMode !== 'light' &&
      merged.themeMode !== 'dark' && merged.themeMode !== 'sepia' &&
      merged.themeMode !== 'hc') {
    merged.themeMode = DEFAULT_SETTINGS.themeMode;
  }
  return normalizeFontSizes(merged);
}

export function resolveThemeMode(mode: Settings['themeMode'], prefersDark: boolean): 'light' | 'dark' {
  if (mode === 'system') return prefersDark ? 'dark' : 'light';
  // Named themes (sepia, hc) don't have a "light/dark" resolution;
  // they go through their own _tokens.css block. Treat them as
  // 'light' for the purpose of this helper (used by legacy code
  // paths that only know about light/dark, e.g. systemThemeMedia
  // listener in settings.ts:457).
  if (mode === 'sepia' || mode === 'hc') return 'light';
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
