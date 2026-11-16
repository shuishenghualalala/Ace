/**
 * **本仓库唯一允许 import `playwright-core` 的文件。**
 *
 * 约束（升级 Playwright 时只需审这一个文件 + 契约测试）：
 *
 * - 不允许任何其他文件 `import 'playwright-core'`；
 * - 不允许业务代码 `import 'playwright-core/lib/**'`；
 * - 版本在 package.json 里锁死（当前 `1.62.0`，不带 `^`），升级走单独 PR。
 *
 * ## 用到的非文档化表面（升级必查）
 *
 * 1. `aria-ref=eN` 选择器引擎。`ariaSnapshot({ mode: 'ai' })` 是公开 API 且会吐出
 *    `[ref=eN]`，但"用 ref 反查 Locator"尚未正式文档化。
 *
 * 2. `connectOverCDP` 的公开 `artifactsDir` 选项。Electron 原生下载由
 *    `DownloadItem.setSavePath` 落到任务目录；同时指定 core artifact 根目录，transport
 *    才能在汇报完成前建立 guid 文件，让公开 `Download.path/saveAs/createReadStream`
 *    继续遵守 Playwright 语义。
 */

import { configurePlaywrightBrowserRegistry } from './playwright-browser-runtime';

import type {
  Browser,
  BrowserContext,
  BrowserContextOptions,
  CDPSession,
  Dialog,
  FileChooser,
  LaunchOptions,
  Locator,
  Page,
  Request,
} from 'playwright-core';
import type { CdpTransport } from './electron-cdp-transport';

export type {
  Browser,
  BrowserContext,
  BrowserContextOptions,
  CDPSession,
  Dialog,
  FileChooser,
  LaunchOptions,
  Locator,
  Page,
  Request,
};

// playwright-core fixes its browser/FFmpeg registry at package evaluation time.
// This statement must stay before the runtime require. A static ESM import would
// run first and silently defeat packaged-browser selection.
configurePlaywrightBrowserRegistry();
// The compatibility boundary intentionally loads the runtime after the
// registry bootstrap above. A static import would execute too early.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { chromium } = require('playwright-core') as typeof import('playwright-core');


/** Playwright 的 ref 形如 `e12`（主文档）或 `f1e3`（帧内）。 */
const REF_PATTERN = /^(?:f\d+)?e\d+$/;

/**
 * 接管一个已经跑起来的 Electron 浏览器。
 *
 * `noDefaults: true`：不改动既有默认 context 的下载、媒体等设置 —— 这些由 Crew 的
 * session 层负责，不该被 Playwright 覆写。
 *
 * **注意它同时会跳过 `Emulation.setFocusEmulationEnabled`**
 * （playwright-core `crPage.ts`：`skipDefaultOverrides` 分支），而后台标签页的 rAF
 * 依赖它。所以焦点模拟由 `enableFocusEmulation()` 显式下发，不能指望默认行为。
 */
export async function connectOverCdp(
  transport: CdpTransport,
  artifactsDir: string,
): Promise<Browser> {
  return await chromium.connectOverCDP(transport, {
    isLocal: true,
    noDefaults: true,
    artifactsDir,
  });
}

/**
 * 显式开启焦点模拟。
 *
 * 隐藏窗口里的渲染器默认被判为非活动，`requestAnimationFrame` 挂起；而 Playwright
 * 的 actionability 用 rAF 比较相邻两帧包围盒判断"元素已稳定"。不开这个，后台标签页
 * 的所有点击都会卡到超时 —— 而且是**静默**的，只表现为"点不动"。
 *
 * 实测：跨窗口移动后无需重设。
 */
export async function setFocusEmulation(
  context: BrowserContext,
  page: Page,
  enabled: boolean,
): Promise<void> {
  const cdp = await context.newCDPSession(page);
  try {
    await cdp.send('Emulation.setFocusEmulationEnabled', { enabled });
  } finally {
    // 这里每次只发一条命令。保留 alias session 不会带来任何能力，只会让 transport
    // 持续扇出页面事件并在长期运行中泄漏 listener/映射。
    await cdp.detach().catch(() => undefined);
  }
}

/** Backwards-compatible spelling used by older callers and the upgrade contract. */
export async function enableFocusEmulation(context: BrowserContext, page: Page): Promise<void> {
  await setFocusEmulation(context, page, true);
}

/** AI 快照：带 `[ref=eN]`，含 iframe 内容，保留层级。 */
export async function aiSnapshot(page: Page, timeoutMs: number): Promise<string> {
  return await page.ariaSnapshot({ mode: 'ai', timeout: timeoutMs });
}

/** 由快照 ref 反查 Locator。ref 只在**当前快照**生命周期内有效。 */
export function snapshotRefSelector(ref: string): string {
  if (!REF_PATTERN.test(ref)) throw new Error(`非法的 Playwright 快照 ref: ${ref}`);
  return `aria-ref=${ref}`;
}

/** 由快照 ref 反查 Locator。ref 只在**当前快照**生命周期内有效。 */
export function locatorFromRef(page: Page, ref: string): Locator {
  return page.locator(snapshotRefSelector(ref));
}
