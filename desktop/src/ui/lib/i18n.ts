/**
 * 桌面端轻量 i18n 模块。
 *
 * 零依赖，纯对象查找。支持：
 *  - t(key, params?)         取值 + {var} 插值
 *  - setLocale / getLocale   读写当前语言
 *  - resolveLocale()         将 'system' 解析为实际 locale
 *  - translateDOM(root?)     遍历 DOM 替换 [data-i18n] 元素文本
 *  - onLocaleChange(cb)      语言变更回调
 *
 * 数据流：
 *   localStorage crew.settings.language  →  当前语言
 *   'system'  →  window.Crew.getSystemLocale() → 实际 locale
 *   'zh-CN'   →  直接使用
 *   'en'      →  直接使用
 */

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

export type Locale = 'zh-CN' | 'en';
export type LanguageSetting = 'system' | Locale;

// ---------------------------------------------------------------------------
// 翻译词典
// ---------------------------------------------------------------------------

const zh: Record<string, string> = {
  // == 通用设置 ==
  'settings.general': '通用设置',
  'settings.general.desc': '调整界面外观、输入习惯与启动行为。',
  'settings.appearance': '外观',
  'settings.behavior': '行为',
  'settings.themeMode': '外观模式',
  'settings.themeMode.desc': '跟随系统切换，或手动指定浅色/深色主题。',
  'settings.themeMode.system': '跟随系统',
  'settings.themeMode.light': '浅色（默认蓝）',
  'settings.themeMode.dark': '深色（默认蓝）',
  'settings.themeMode.sepia': '护眼米色',
  'settings.themeMode.sidebarGray': '侧栏灰色',
  'settings.themeMode.hc': '高对比度',
  'settings.accent': '强调色',
  'settings.accent.desc': '主按钮、选中态与链接的颜色。',
  'settings.accent.blue': '科技蓝',
  'settings.accent.indigo': '商务靛蓝',
  'settings.accent.violet': '活力紫',
  'settings.accent.cyan': '清亮青',
  'settings.uiFontSize': '界面字号',
  'settings.uiFontSize.desc': '控制导航、按钮、表格、设置项等 UI 文本。',
  'settings.contentFontSize': '正文字号',
  'settings.contentFontSize.desc': '控制对话消息、Markdown 正文和长文本阅读区。',
  'settings.editorFontSize': '编辑器字号',
  'settings.editorFontSize.desc': '控制输入框、表单文本框和可编辑内容。',
  'settings.terminalFontSize': '终端/代码字号',
  'settings.terminalFontSize.desc': '控制代码块、日志、工具参数和等宽文本。',
  'settings.fontFamily': '字体',
  'settings.fontFamily.desc': '系统已默认安装 PingFang SC / Microsoft YaHei。',
  'settings.fontFamily.system': '系统默认',
  'settings.fontFamily.noto': '思源黑体',
  'settings.fontFamily.inter': 'Inter',
  'settings.language': '界面语言',
  'settings.language.desc': '切换界面显示语言，跟随系统将自动检测操作系统语言。',
  'settings.language.system': '跟随系统',
  'settings.language.zhCN': '简体中文',
  'settings.language.en': 'English',

  'settings.autoStart': '开机自启',
  'settings.autoStart.desc': '登录系统时自动启动 Crew。',
  'settings.closeBehavior': '关闭窗口',
  'settings.closeBehavior.desc': '关闭窗口时是隐藏到托盘还是直接退出。',
  'settings.closeBehavior.tray': '最小化到托盘',
  'settings.closeBehavior.quit': '直接退出应用',
  'settings.closeBehavior.ask': '询问我',
  'settings.enterToSend': 'Enter 发送消息',
  'settings.enterToSend.desc': '关闭后，Enter 仅换行，发送需用 Cmd/Ctrl + Enter。',
  'settings.streaming': '流式输出',
  'settings.streaming.desc': '实时显示 Agent 输出（关闭后一次性返回）。',
  'settings.inspectorOpen': '右侧检查器（默认显示）',

  // == 设置导航 ==
  'settings.nav.title': '设置',
  'settings.nav.general': '通用设置',
  'settings.nav.account': '账户信息',
  'settings.nav.models': '模型',
  'settings.nav.channels': '渠道',
  'settings.nav.mcp': 'MCP 服务',
  'settings.nav.logs': '运行日志',
  'settings.nav.usage': '使用统计',
  'settings.nav.library': '项目与会话',
  'settings.nav.data': '数据管理',
  'settings.nav.about': '关于我们',

  // == 通知/提示 ==
  'notify.themeChanged.system': '主题已切换：跟随系统',
  'notify.themeChanged.light': '主题已切换：浅色',
  'notify.themeChanged.dark': '主题已切换：深色',
  'notify.accentUpdated': '强调色已更新',
  'notify.fontUpdated': '字体设置已更新（重启后部分应用生效）',
  'notify.closeBehaviorUpdated': '关闭行为已更新',
  'notify.closeBehaviorFailed': '关闭行为设置失败',
  'notify.autoStartOn': '已开启开机自启',
  'notify.autoStartOff': '已关闭开机自启',
  'notify.autoStartFailed': '开机自启设置失败',
  'notify.cacheCleared': '已清除本地缓存',
  'notify.cacheClearFailed': '清除失败',
  'notify.settingsReset': '已重置为默认设置',
  'notify.copied': '已复制',
  'notify.copyImage': '复制图片',
  'notify.exportFailed': '导出失败',
  'notify.backendDisconnected': '后端未连接，已导出本地数据',
  'notify.languageChanged': '界面语言已切换，部分界面将在下次打开窗口时完全生效。',

  // == 数据管理 ==
  'settings.clearCache': '清除本地缓存',
  'settings.clearCache.label': '清除缓存',
  'settings.clearCache.desc': '清空本地草稿与未发送的附件。',
  'settings.clearCache.btn': '清除',
  'settings.exportSessions': '导出',
  'settings.exportSessions.label': '导出全部会话',
  'settings.exportSessions.desc': '导出所有会话的元数据与完整消息正文（JSON，可能较慢）。',
  'settings.exporting': '导出中',
  'settings.resetAll': '重置全部设置',
  'settings.resetAll.label': '重置全部设置',
  'settings.resetAll.desc': '恢复默认设置，会话与登录态保持不变。',
  'settings.resetAll.btn': '重置',
  'settings.resetAll.confirm': '确认重置全部设置？该操作不可撤销。',
  'settings.data.title': '数据管理',
  'settings.data.desc': '清除缓存、导出/导入工作空间与会话。',

  // == 通用 ==
  'common.small': '小',
  'common.default': '默认',
  'common.large': '大',
  'common.close': '关闭',
  'common.save': '保存',
  'common.cancel': '取消',
  'common.confirm': '确认',
  'common.loading': '加载中…',

  // == 帮助文档 ==
  'help.loading': '正在加载帮助文档…',
  'help.loadFailed': '帮助文档加载失败',
  'help.unknownError': '未知错误',
  'help.docVersion': '文档版本',

  // == 账户 ==
  'account.localMode': '本地模式',
  'account.noLoginNeeded': '当前无需登录。',
  'account.goLogin': '前往登录',

  // == 导航 ==
  'nav.chat': '对话',
  'nav.wiki': 'Wiki',
  'nav.skills': '技能',
  'nav.agents': '智能体',
  'nav.audit': '审计',
  'nav.cron': '定时任务',
  'nav.system': '系统',
  'nav.newChat': '新对话',

  // == 对话过程时间线 ==
  'chat.turn.working': '已工作 {duration}',
  'chat.thinking.brief': '思考 · 持续了几秒',
  'chat.thinking.measured': '思考 · 持续了 {seconds} 秒',

  // == 初始化 ==
  'init.failed': '初始化 {name} 失败：{error}',
};

const en: Record<string, string> = {
  // == General Settings ==
  'settings.general': 'General',
  'settings.general.desc': 'Customize appearance, input behavior, and startup preferences.',
  'settings.appearance': 'Appearance',
  'settings.behavior': 'Behavior',
  'settings.themeMode': 'Theme',
  'settings.themeMode.desc': 'Follow system or manually choose light/dark theme.',
  'settings.themeMode.system': 'Follow System',
  'settings.themeMode.light': 'Light (Blue)',
  'settings.themeMode.dark': 'Dark (Blue)',
  'settings.themeMode.sepia': 'Sepia',
  'settings.themeMode.sidebarGray': 'Sidebar Gray',
  'settings.themeMode.hc': 'High Contrast',
  'settings.accent': 'Accent Color',
  'settings.accent.desc': 'Color for primary buttons, selections, and links.',
  'settings.accent.blue': 'Tech Blue',
  'settings.accent.indigo': 'Business Indigo',
  'settings.accent.violet': 'Vivid Violet',
  'settings.accent.cyan': 'Clear Cyan',
  'settings.uiFontSize': 'UI Font Size',
  'settings.uiFontSize.desc': 'Controls navigation, buttons, tables, and settings text.',
  'settings.contentFontSize': 'Content Font Size',
  'settings.contentFontSize.desc': 'Controls chat messages, Markdown, and reading areas.',
  'settings.editorFontSize': 'Editor Font Size',
  'settings.editorFontSize.desc': 'Controls input fields, forms, and editable content.',
  'settings.terminalFontSize': 'Terminal / Code Font Size',
  'settings.terminalFontSize.desc': 'Controls code blocks, logs, tool params, and monospace text.',
  'settings.fontFamily': 'Font Family',
  'settings.fontFamily.desc': 'System includes PingFang SC / Microsoft YaHei by default.',
  'settings.fontFamily.system': 'System Default',
  'settings.fontFamily.noto': 'Noto Sans SC',
  'settings.fontFamily.inter': 'Inter',
  'settings.language': 'Language',
  'settings.language.desc': 'Change the interface language. "Follow System" auto-detects your OS language.',
  'settings.language.system': 'Follow System',
  'settings.language.zhCN': '简体中文',
  'settings.language.en': 'English',

  'settings.autoStart': 'Launch at Startup',
  'settings.autoStart.desc': 'Automatically start Crew when you sign in to your system.',
  'settings.closeBehavior': 'Close Window',
  'settings.closeBehavior.desc': 'Hide to tray or quit when closing the window.',
  'settings.closeBehavior.tray': 'Minimize to Tray',
  'settings.closeBehavior.quit': 'Quit Application',
  'settings.closeBehavior.ask': 'Ask Me',
  'settings.enterToSend': 'Enter to Send',
  'settings.enterToSend.desc': 'When off, Enter inserts a newline; use Cmd/Ctrl+Enter to send.',
  'settings.streaming': 'Streaming Output',
  'settings.streaming.desc': 'Show agent output in real time (toggle off for batch response).',
  'settings.inspectorOpen': 'Inspector (Open by Default)',

  // == Settings Nav ==
  'settings.nav.title': 'Settings',
  'settings.nav.general': 'General',
  'settings.nav.account': 'Account',
  'settings.nav.models': 'Models',
  'settings.nav.channels': 'Channels',
  'settings.nav.mcp': 'MCP Services',
  'settings.nav.logs': 'Logs',
  'settings.nav.usage': 'Usage',
  'settings.nav.library': 'Projects & Sessions',
  'settings.nav.data': 'Data',
  'settings.nav.about': 'About',

  // == Notifications ==
  'notify.themeChanged.system': 'Theme: Follow System',
  'notify.themeChanged.light': 'Theme: Light',
  'notify.themeChanged.dark': 'Theme: Dark',
  'notify.accentUpdated': 'Accent color updated',
  'notify.fontUpdated': 'Font updated (restart for full effect)',
  'notify.closeBehaviorUpdated': 'Close behavior updated',
  'notify.closeBehaviorFailed': 'Failed to update close behavior',
  'notify.autoStartOn': 'Launch at startup enabled',
  'notify.autoStartOff': 'Launch at startup disabled',
  'notify.autoStartFailed': 'Failed to update startup setting',
  'notify.cacheCleared': 'Local cache cleared',
  'notify.cacheClearFailed': 'Failed to clear cache',
  'notify.settingsReset': 'Settings reset to defaults',
  'notify.copied': 'Copied',
  'notify.copyImage': 'Copy image',
  'notify.exportFailed': 'Export failed',
  'notify.backendDisconnected': 'Backend not connected, exported local data',
  'notify.languageChanged': 'Language changed. Some text will fully apply after reopening the window.',

  // == Data Management ==
  'settings.clearCache': 'Clear Local Cache',
  'settings.clearCache.label': 'Clear Cache',
  'settings.clearCache.desc': 'Clear local drafts and unsent attachments.',
  'settings.clearCache.btn': 'Clear',
  'settings.exportSessions': 'Export',
  'settings.exportSessions.label': 'Export All Sessions',
  'settings.exportSessions.desc': 'Export all sessions metadata and full message content (JSON, may be slow).',
  'settings.exporting': 'Exporting',
  'settings.resetAll': 'Reset All Settings',
  'settings.resetAll.label': 'Reset All Settings',
  'settings.resetAll.desc': 'Restore defaults; sessions and login state are preserved.',
  'settings.resetAll.btn': 'Reset',
  'settings.resetAll.confirm': 'Reset all settings? This cannot be undone.',
  'settings.data.title': 'Data Management',
  'settings.data.desc': 'Clear cache, export/import workspaces and sessions.',

  // == Common ==
  'common.small': 'Small',
  'common.default': 'Default',
  'common.large': 'Large',
  'common.close': 'Close',
  'common.save': 'Save',
  'common.cancel': 'Cancel',
  'common.confirm': 'Confirm',
  'common.loading': 'Loading…',

  // == Help ==
  'help.loading': 'Loading help documentation…',
  'help.loadFailed': 'Failed to load help documentation',
  'help.unknownError': 'Unknown error',
  'help.docVersion': 'Documentation version',

  // == Account ==
  'account.localMode': 'Local Mode',
  'account.noLoginNeeded': 'No login required.',
  'account.goLogin': 'Sign In',

  // == Navigation ==
  'nav.chat': 'Chat',
  'nav.wiki': 'Wiki',
  'nav.skills': 'Skills',
  'nav.agents': 'Agents',
  'nav.audit': 'Audit',
  'nav.cron': 'Cron',
  'nav.system': 'System',
  'nav.newChat': 'New Chat',

  // == Chat process timeline ==
  'chat.turn.working': 'Worked {duration}',
  'chat.thinking.brief': 'Thinking · a few seconds',
  'chat.thinking.measured': 'Thinking · {seconds}s',

  // == Init ==
  'init.failed': 'Failed to initialize {name}: {error}',
};

const dictionaries: Record<Locale, Record<string, string>> = { 'zh-CN': zh, en };

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------

let currentLocale: Locale = 'zh-CN';
let systemLocale: Locale | null = null;
const changeListeners: Array<() => void> = [];

// ---------------------------------------------------------------------------
// 公开 API
// ---------------------------------------------------------------------------

/** 获取当前实际使用的 locale（已解析 system）。 */
export function getLocale(): Locale {
  return currentLocale;
}

/** 注册语言变更监听器。 */
export function onLocaleChange(cb: () => void): () => void {
  changeListeners.push(cb);
  return () => {
    const idx = changeListeners.indexOf(cb);
    if (idx >= 0) changeListeners.splice(idx, 1);
  };
}

function emitLocaleChange(): void {
  for (const cb of changeListeners) cb();
}

/**
 * 根据语言设置解析实际 locale。
 * 'system' → 调用主进程 getSystemLocale() 检测系统语言
 * 其他 → 直接使用
 */
export async function resolveLocale(setting: LanguageSetting): Promise<Locale> {
  if (setting === 'system') {
    if (!systemLocale) {
      try {
        const raw = await window.Crew?.getSystemLocale?.();
        // 系统语言是中文（含各种变体）→ zh-CN，否则 → en
        systemLocale = typeof raw === 'string' && raw.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en';
      } catch {
        systemLocale = 'en';
      }
    }
    return systemLocale;
  }
  return setting;
}

/**
 * 同步解析 locale（不涉及 IPC 调用）。
 * 仅在 systemLocale 已缓存或 setting 非 'system' 时可用。
 */
export function resolveLocaleSync(setting: LanguageSetting): Locale {
  if (setting === 'system') return systemLocale ?? 'en';
  return setting;
}

/** 设置当前语言（实际 locale）。 */
export function setLocale(locale: Locale): void {
  if (currentLocale === locale) return;
  currentLocale = locale;
  document.documentElement.lang = locale === 'zh-CN' ? 'zh-CN' : 'en';
  emitLocaleChange();
}

/** 获取翻译字符串，支持 {key} 插值。 */
export function t(key: string, params?: Record<string, string | number>): string {
  const dict = dictionaries[currentLocale];
  let value = dict[key];
  if (value === undefined) {
    // 回退到中文
    value = dictionaries['zh-CN'][key];
  }
  if (value === undefined) return key;
  if (params) {
    value = value.replace(/\{(\w+)\}/g, (_m, p) => {
      const v = params[p];
      return v !== undefined ? String(v) : `{${p}}`;
    });
  }
  return value;
}

/**
 * 遍历 DOM，将带有 [data-i18n] 属性的元素文本替换为翻译值。
 * 也处理 [data-i18n-title]（title 属性）和 [data-i18n-placeholder]（placeholder）。
 */
export function translateDOM(root: ParentNode = document): void {
  const dict = dictionaries[currentLocale];

  root.querySelectorAll<HTMLElement>('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n');
    if (key) el.textContent = dict[key] ?? dictionaries['zh-CN'][key] ?? key;
  });

  root.querySelectorAll<HTMLElement>('[data-i18n-title]').forEach((el) => {
    const key = el.getAttribute('data-i18n-title');
    if (key) el.title = dict[key] ?? dictionaries['zh-CN'][key] ?? key;
  });

  root.querySelectorAll<HTMLElement>('[data-i18n-placeholder]').forEach((el) => {
    const key = el.getAttribute('data-i18n-placeholder');
    if (key && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) {
      el.placeholder = dict[key] ?? dictionaries['zh-CN'][key] ?? key;
    }
  });
}

/** 初始化模块：从 localStorage 读取语言设置并应用。 */
export async function initI18n(): Promise<void> {
  // 从 settings 中读取语言偏好
  let langSetting: LanguageSetting = 'system';
  try {
    const raw = localStorage.getItem('crew.settings');
    if (raw) {
      const parsed = JSON.parse(raw) as { language?: string };
      if (parsed.language === 'system' || parsed.language === 'zh-CN' || parsed.language === 'en') {
        langSetting = parsed.language as LanguageSetting;
      }
    }
  } catch { /* ignore */ }

  const locale = await resolveLocale(langSetting);
  setLocale(locale);
  translateDOM();
}