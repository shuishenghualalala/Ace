/**
 * **本仓库唯一允许 import `playwright-core` 的文件。**
 *
 * 约束（升级 Playwright 时只需审这一个文件 + 契约测试）：
 *
 * - 不允许任何其他文件 `import 'playwright-core'`；
 * - 不允许业务代码 `import 'playwright-core/lib/**'`；
 * - 本文件唯一例外是解析包 exports 明确暴露的 `lib/coreBundle`，用来取得官方
 *   InjectedScript；其 bundle 布局无兼容承诺，所以必须由结构校验和真实契约兜住；
 * - 版本在 package.json 里锁死（当前 `1.62.0`，不带 `^`），升级走单独 PR。
 *
 * ## 用到的非文档化表面（升级必查）
 *
 * 1. `Locator.normalize()` 返回的 Locator 的 `_selector` 字段。
 *    `normalize()` 本身是公开 API，但它的 `toString()` 给的是**面向人的代码写法**
 *    （`getByRole('button', { name: 'x' })`），不能喂回 `page.locator()`。要持久化
 *    的是底层 selector（`internal:role=button[name="x"i]`），只能从 `_selector` 取。
 *    契约测试 `playwright-contract.test.ts` 盯住这一点。
 *
 * 2. `aria-ref=eN` 选择器引擎。`ariaSnapshot({ mode: 'ai' })` 是公开 API 且会吐出
 *    `[ref=eN]`，但"用 ref 反查 Locator"尚未正式文档化。
 *
 * 3. `lib/coreBundle` 内的 generated `injectedScriptSource`。Playwright 没有公开
 *    selectorGenerator API；录制又必须在导航销毁文档前同步取得 selector，因此把
 *    当前安装版本的官方 InjectedScript 注入录制文档。
 *
 * 4. `connectOverCDP` 的公开 `artifactsDir` 选项。Electron 原生下载由
 *    `DownloadItem.setSavePath` 落到任务目录；同时指定 core artifact 根目录，transport
 *    才能在汇报完成前建立 guid 文件，让公开 `Download.path/saveAs/createReadStream`
 *    继续遵守 Playwright 语义。
 *
 * 这四条都只在本文件内出现。
 */

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
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

export interface OfficialInjectedScriptSource {
  /** Exact generated source shipped by the installed playwright-core package. */
  source: string;
  /** Installed package version, kept with diagnostics and upgrade contracts. */
  version: string;
  /** Deterministic evidence that extraction did not select a different literal. */
  sha256: string;
}

let cachedOfficialInjectedScriptSource: OfficialInjectedScriptSource | undefined;

/**
 * Extract Playwright's generated InjectedScript from its publicly exported
 * `lib/coreBundle` entrypoint.
 *
 * Playwright does not expose selectorGenerator as a package API. Calling
 * Locator.normalize() after a document-start recorder binding wedges the real
 * Electron + OOPIF Runtime channel, so recording needs the same generator
 * synchronously inside the DOM event callback. The generated source is the one
 * artifact that contains that exact implementation and all of its dependencies.
 *
 * This parser deliberately accepts one plain JS string literal only. Any
 * bundling-layout change, duplicate candidate or malformed literal fails
 * recording startup and is caught by the upgrade contract; it never silently
 * swaps in Crew's heuristic selector.
 */
export function officialInjectedScriptSource(): OfficialInjectedScriptSource {
  if (cachedOfficialInjectedScriptSource) return cachedOfficialInjectedScriptSource;

  const coreBundlePath = require.resolve('playwright-core/lib/coreBundle');
  const packagePath = require.resolve('playwright-core/package.json');
  const packageValue = JSON.parse(readFileSync(packagePath, 'utf8')) as {
    version?: unknown;
  };
  const version = typeof packageValue.version === 'string' ? packageValue.version : '';
  if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
    throw new Error('playwright-core package version is invalid');
  }

  const bundle = readFileSync(coreBundlePath, 'utf8');
  const marker = '// packages/playwright-core/src/generated/injectedScriptSource.ts';
  const markerIndex = bundle.indexOf(marker);
  if (markerIndex < 0 || bundle.indexOf(marker, markerIndex + marker.length) >= 0) {
    throw new Error(
      `playwright-core ${version} no longer has one generated injectedScriptSource marker`,
    );
  }
  const assignment = /\bsource\d*\s*=\s*/g;
  assignment.lastIndex = markerIndex + marker.length;
  const match = assignment.exec(bundle);
  if (!match || match.index - markerIndex > 2_000) {
    throw new Error(
      `playwright-core ${version} injectedScriptSource assignment is unavailable`,
    );
  }
  let cursor = assignment.lastIndex;
  const quote = bundle[cursor];
  if (quote !== '\'' && quote !== '"') {
    throw new Error(
      `playwright-core ${version} injectedScriptSource is not a plain string literal`,
    );
  }
  cursor += 1;
  let escaped = false;
  let end = -1;
  for (; cursor < bundle.length; cursor += 1) {
    const character = bundle[cursor];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (character === '\\') {
      escaped = true;
      continue;
    }
    if (character === quote) {
      end = cursor;
      break;
    }
  }
  if (end < 0) {
    throw new Error(`playwright-core ${version} injectedScriptSource literal is unterminated`);
  }

  const literal = bundle.slice(assignment.lastIndex, end + 1);
  const decoded = runInNewContext(literal, Object.create(null), {
    displayErrors: false,
  }) as unknown;
  if (
    typeof decoded !== 'string'
    || !decoded.includes('// packages/injected/src/injectedScript.ts')
    || !decoded.includes('InjectedScript: () => InjectedScript')
    || !decoded.includes('generateSelectorSimple(')
    || !decoded.includes('module.exports =')
  ) {
    throw new Error(
      `playwright-core ${version} injectedScriptSource failed its structural contract`,
    );
  }

  cachedOfficialInjectedScriptSource = Object.freeze({
    source: decoded,
    version,
    sha256: createHash('sha256').update(decoded).digest('hex'),
  });
  return cachedOfficialInjectedScriptSource;
}

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

/**
 * Resolve a transient aria-ref/CSS Locator through Playwright's public
 * normalization API and keep using the returned Locator directly.
 *
 * Ordinary actions must not read `_selector`; that private field is needed
 * only when recorder/skill persistence requires a serializable selector.
 */
export async function normalizeLocator(locator: Locator): Promise<Locator> {
  return await locator.normalize();
}

/**
 * 把一个临时定位（快照 ref、CSS 路径）转成**可持久化**的稳定选择器。
 *
 * 这是"录一遍 → 存进技能 → 下次精准复现"的关键一步：`normalize()` 用的是
 * Playwright codegen 同一套评分（test id → aria role → 面向用户属性 → 才轮到 CSS），
 * 并且会自动补出跨帧链：
 *
 *   `#frame >> internal:control=enter-frame >> internal:role=button[name="提交"i]`
 *
 * 返回值可以直接存进技能文件，回放时 `page.locator(存的字符串)` 即可。
 */
export async function toStableSelector(locator: Locator): Promise<string> {
  const normalized = await locator.normalize();
  // 见文件头：toString() 是给人看的代码写法，持久化必须取底层 selector。
  const selector = (normalized as unknown as { _selector?: unknown })._selector;
  if (typeof selector !== 'string' || !selector) {
    throw new Error('normalize() 未返回可持久化的 selector —— 检查 playwright-core 版本');
  }
  return selector;
}

/** 人类可读形式，写进技能文档/审计日志用（不可喂回 `page.locator()`）。 */
export async function toReadableLocator(locator: Locator): Promise<string> {
  const normalized = await locator.normalize();
  return normalized.toString();
}
