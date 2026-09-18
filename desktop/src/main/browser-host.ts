import { createHash, randomUUID } from 'node:crypto';
import { constants as fsConstants, existsSync, realpathSync } from 'node:fs';
import {
  chmod,
  lstat,
  open,
  readdir,
  stat,
  unlink,
  writeFile,
} from 'node:fs/promises';
import path from 'node:path';
import { EventEmitter } from 'node:events';

import {
  WebContentsView,
  session as electronSession,
  type AuthInfo,
  type BrowserWindow,
  type DownloadItem,
  type Event as ElectronEvent,
  type Rectangle,
  type Session,
  type WebContents,
  type WebPreferences,
} from 'electron';

import * as pwActions from './browser/playwright-actions';
import * as pwConsole from './browser/playwright-console';
import * as pwNetwork from './browser/playwright-network';
import { PlaywrightEngine } from './browser/playwright-engine';
import {
  locatorFromRef,
} from './browser/playwright-compat';
import {
  captureSnapshot,
  captureSnapshotForFind,
  SnapshotFindError,
} from './browser/playwright-snapshot';
import {
  executeUnsafePlaywrightCode,
  RunCodeTimeoutError,
} from './browser/playwright-run-code';

import type {
  ActionContext,
  ClickOptions,
  FillFormField,
  WaitOptions,
} from './browser/playwright-actions';
import type { ChildSessionLifecycleContext } from './browser/electron-cdp-transport';
import type { FileChooser, Page } from './browser/playwright-compat';
import type {
  RefRecord,
  SnapshotFindQuery,
} from './browser/playwright-snapshot';

const RUNTIME_KEY_RE = /^crew_[0-9a-f]{12}$/;
const LABELED_TAB_RE = /^s([0-9a-f]{32})-([1-9][0-9]*)$/;
// `@eN` 来自当前快照，动作层只接受这类页面代次 ref。
const NATIVE_REF_RE = /^@e([1-9][0-9]*)$/;
const GUARD_KEY_RE = /^__crew_guard_[0-9a-f]{32}$/;
const TOKEN_RE = /^[0-9a-f]{32}$/;
// 快照与动作的超时。Playwright 默认 30s 对 agent 循环太长——一次卡死会吃掉整轮
// 对话的耐心；Python 侧的 RPC 超时是外层的第二道闸门。
const SNAPSHOT_TIMEOUT_MS = 15_000;
const ACTION_TIMEOUT_MS = 15_000;
const MODAL_SETTLE_MS = 500;
// The browser normally emits `filechooser` synchronously with the activating
// click, but production apps sometimes defer `input.click()` through a short
// animation/timer. Keep the listener armed across the click and allow a
// bounded post-click grace period before falling back to the exact file input.
const FILE_CHOOSER_GRACE_MS = 2_000;
const DEBUGGER_SETUP_TIMEOUT_MS = 5_000;
const ARTIFACT_SCHEME = 'crew-artifact';
const DEFAULT_DOWNLOAD_TIMEOUT_MS = 25_000;
const DOWNLOAD_DEADLINE_MARGIN_MS = 500;
const AUTOMATION_FOCUS_CONTINUATION_MS = 5_000;
const EDITABLE_AX_ROLES = new Set(['combobox', 'searchbox', 'spinbutton', 'textbox']);
const DEFAULT_VIEWPORT = Object.freeze({ width: 1024, height: 720 });

type ControlMode = 'ai' | 'human' | 'paused';

export interface BrowserRpcRequest {
  type?: 'request';
  id?: string;
  runtime_key: string;
  method: string;
  params?: Record<string, unknown>;
}

export interface BrowserPanelRequest {
  runtimeKey: string;
  sessionId: string;
  tabLabel: string;
  mode: ControlMode;
  bounds: Rectangle;
  visible: boolean;
}

export interface BrowserPanelCaptureRequest {
  runtimeKey: string;
  sessionId: string;
  tabLabel: string;
}

export interface BrowserPanelCapture {
  dataUrl: string;
  width: number;
  height: number;
}

export interface BrowserPanelNavigation {
  url: string;
  title: string;
  can_go_back: boolean;
  can_go_forward: boolean;
}

interface ProxyAuthState {
  proxyRules: string;
  host: string;
  port: number;
  username: string;
  password: string;
}

interface ConsoleRecord {
  level: string;
  message: string;
  source: string;
  line: number;
  timestamp: number;
}

interface NetworkRecord {
  kind: 'request' | 'response' | 'failure';
  method?: string;
  url: string;
  status?: number;
  error?: string;
  timestamp: number;
}

interface DialogState {
  type: string;
  message: string;
  defaultValue: string;
  owner: 'playwright' | 'native';
}

type ModalKind = 'dialog' | 'fileChooser';

interface SessionModalSignal {
  kind: ModalKind;
  tab: BrowserTab;
}

interface PendingModalAction {
  triggerTargetId: string;
  promise: Promise<void>;
  settled: boolean;
  error: unknown;
}

interface AutomationFocusContinuation {
  sourceOrigin: string;
  role: string;
  name: string;
  domFingerprint: string;
  expiresAt: number;
}

/**
 * 快照 ref 的宿主侧状态。
 *
 * 由 `playwright-snapshot` 产出，取代了原来基于 `backendNodeId` 的 `RefState`：
 * `aria-ref` 持有元素本身，重渲染后解析不到而不会掉包，所以不再需要
 * `pageIdentity` 这类「这个 ref 属于哪一版文档」的记账。
 */
type RefState = RefRecord;

interface DownloadGrant {
  tabId: string;
  target: string;
  claimed: boolean;
  item: DownloadItem | null;
  actionActive: boolean;
  actionDeadline: number;
  eventBaseline: number;
  resolve: (value: Record<string, unknown>) => void;
  reject: (error: BrowserHostError) => void;
  timer: NodeJS.Timeout;
}

interface GenericDownloadResult {
  downloadId: string;
  targetId: string;
  sessionHash: string;
  path: string;
  name: string;
  suggestedFilename: string;
  url: string;
  state: string;
  receivedBytes: number;
  totalBytes: number;
  createdAt: number;
  completedAt: number;
  error: string;
}

interface GenericDownloadCapture {
  sessionHash: string;
  sourceTabId: string;
  publicSignals: number;
  downloads: GenericDownloadResult[];
  nativeWaiters: Set<() => void>;
}

type DownloadListener = (
  event: ElectronEvent,
  item: DownloadItem,
  contents: WebContents,
) => void;

interface BrowserTab {
  tabId: string;
  targetId: string;
  label: string;
  sessionHash: string;
  openerTargetId: string;
  /** Runtime page-topology identity used to route same-action popup dialogs. */
  popupOrdinal: number;
  view: WebContentsView;
  // Captured at creation: `view.webContents` is undefined after the renderer is
  // destroyed, so the 'destroyed' handler must not read `.id` off it.
  webContentsId: number;
  mode: ControlMode;
  refs: Map<string, RefState>;
  dialog: DialogState | null;
  /** Mirrors transport filtering; records who received the opening event. */
  dialogForwarding: boolean;
  /**
   * Number of Host-level command modal races currently owning this tab.
   * The counter stays non-zero while a surfaced modal pauses the underlying
   * operation, preventing nested action wrappers from creating a second owner.
   */
  modalRaceDepth: number;
  /**
   * Best-effort Electron UI debug stream only. Functional console reads use
   * the active public Playwright Page's retained buffers.
   */
  console: ConsoleRecord[];
  network: NetworkRecord[];
  /** Task-local destination inherited by popups and public context.newPage(). */
  downloadDir: string;
  /** Task-local download cap inherited by popups. Zero means unlimited. */
  downloadMaxBytes: number;
  mouseX: number;
  mouseY: number;
  /** 最近一次真实接管请求的时间戳；750ms 内的重复输入只发一次接管请求。 */
  takeoverRequestAt: number;
  automationDepth: number;
  debuggerReady: Promise<void> | null;
  /** Real flattened CDP child session id → targetInfo (OOPIFs and workers). */
  childSessions: Map<string, Record<string, unknown>>;
  /** Flattened child session → parent session, needed to rebuild exact OOPIF frame paths. */
  childSessionParents: Map<string, string>;
  guardContextId: number;
  guardFrameId: string;
  guardLoaderId: string;
  /** Host-owned guard identity; no page global or MutationObserver is installed. */
  guardStateKey: string;
  guardStateToken: string;
  // Main-frame navigation/title events form a host-owned transition epoch.
  // The isolated page marker alone can briefly expose a new DOM/title with an
  // old history URL while a same-document navigation is still settling.
  navigationEpoch: number;
  navigationPending: boolean;
  visualEpoch: {
    token: string;
    pageIdentity: string;
    screenshotHash: string;
    width: number;
    height: number;
  } | null;
  lastFilled: {
    backendNodeId: number;
    pageIdentity: string;
    expectedValueHash: string;
  } | null;
  // Separate from lastFilled: snapshot consumes the one-shot value verifier,
  // but a later user-facing screenshot still needs to know whether an
  // editable element was focused by Crew automation (rather than by the
  // user/site). This lets settled exports release only our own incidental
  // focus without dismissing arbitrary page UI.
  automationFocus: {
    backendNodeId: number;
    pageIdentity: string;
    continuation: AutomationFocusContinuation | null;
  } | null;
  automationFocusPending: AutomationFocusContinuation | null;
  crashed: boolean;
  artifactToken: string;
}

interface ArtifactGrant {
  tabId: string;
  content: ArrayBuffer;
  expiresAt: number;
}

interface BrowserOwner {
  runtimeKey: string;
  profilePath: string;
  session: Session;
  tabs: Map<string, BrowserTab>;
  activeTabId: string;
  tabCounter: number;
  /** Monotonic per-opener popup order, scoped to one logical browser session. */
  popupOrdinals: Map<string, number>;
  proxy: ProxyAuthState | null;
  downloadGrant: DownloadGrant | null;
  downloadListener: DownloadListener | null;
  downloadEventSequence: number;
  genericDownloadCaptures: GenericDownloadCapture[];
  reservedDownloadPaths: Set<string>;
  artifacts: Map<string, ArtifactGrant>;
  artifactProtocolRegistered: boolean;
  /** Original full command continuation retained while a modal is surfaced. */
  pendingModalActions: Map<string, PendingModalAction>;
  /** Waiters are armed before dispatch/accept to close every event-order race. */
  modalWaiters: Map<string, Set<(signal: SessionModalSignal) => void>>;
  lifecycle: 'active' | 'closing' | 'clearing';
  /**
   * 该账号的 Playwright 引擎。
   *
   * 一个 owner 一个引擎 = 一个 transport = 一个 Playwright `Browser`，transport 只
   * 挂载本 owner 的 view。因此 Playwright 侧在**物理上**看不到别的账号的标签页，
   * per-owner 隔离不依赖调用方自觉。
   */
  engine: PlaywrightEngine;
}

interface AxValue {
  value?: unknown;
}

interface AxNode {
  ignored?: boolean;
  backendDOMNodeId?: number;
  role?: AxValue;
  name?: AxValue;
  value?: AxValue;
  properties?: Array<{ name?: string; value?: AxValue }>;
}

interface PreventableEvent {
  preventDefault(): void;
}

export class BrowserHostError extends Error {
  readonly code: string;
  readonly uncertain: boolean;
  readonly phase: string;
  readonly partial: boolean;
  readonly completed_count: number;
  readonly browser_stopped: boolean;
  readonly stop_unconfirmed: boolean;

  constructor(
    message: string,
    options: {
      code?: string;
      uncertain?: boolean;
      phase?: string;
      partial?: boolean;
      completedCount?: number;
      browserStopped?: boolean;
      stopUnconfirmed?: boolean;
    } = {},
  ) {
    super(message);
    this.name = 'BrowserHostError';
    this.code = options.code ?? 'browser_host_error';
    this.uncertain = options.uncertain ?? false;
    this.phase = options.phase ?? '';
    this.partial = options.partial ?? false;
    this.completed_count = Math.max(0, Math.trunc(options.completedCount ?? 0));
    this.browser_stopped = options.browserStopped ?? false;
    this.stop_unconfirmed = options.stopUnconfirmed ?? false;
  }
}

function asRecord(value: unknown, label = '参数'): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new BrowserHostError(`${label}必须是对象`, { code: 'invalid_request' });
  }
  return value as Record<string, unknown>;
}

function asOptionalRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown, label: string, _maximum?: number): string {
  if (typeof value !== 'string') {
    throw new BrowserHostError(`${label}必须是字符串`, { code: 'invalid_request' });
  }
  return value;
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function navigationFlag(
  details: unknown,
  name: 'isMainFrame' | 'isSameDocument',
  legacyValue: unknown,
): boolean | undefined {
  if (details && typeof details === 'object') {
    const value = (details as Record<string, unknown>)[name];
    if (typeof value === 'boolean') return value;
  }
  return typeof legacyValue === 'boolean' ? legacyValue : undefined;
}

function asPositiveInteger(value: unknown, label: string, maximum: number): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0 || parsed > maximum) {
    throw new BrowserHostError(`${label}无效`, { code: 'invalid_request' });
  }
  return parsed;
}

function transferLimit(value: unknown): number {
  if (value === undefined || value === null || value === '') {
    return DEFAULT_MAX_TRANSFER_BYTES;
  }
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new BrowserHostError('max_transfer_bytes无效', { code: 'invalid_request' });
  }
  return parsed;
}

function invalidCommandArgs(message = '浏览器命令参数无效'): never {
  throw new BrowserHostError(message, { code: 'invalid_input' });
}

function strictUnsignedInteger(value: string, minimum: number, maximum: number): number {
  if (!/^(?:0|[1-9][0-9]*)$/.test(value)) invalidCommandArgs();
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    invalidCommandArgs();
  }
  return parsed;
}

function strictClickPosition(value: string): number {
  if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value)) invalidCommandArgs();
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    invalidCommandArgs();
  }
  return parsed;
}

function strictFiniteNumber(value: string): number {
  if (
    !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/.test(value)
  ) {
    invalidCommandArgs();
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) invalidCommandArgs();
  return parsed;
}

function parseDropArgs(args: string[]): {
  ref: string;
  payload: pwActions.DropPayload;
} {
  if (args.length < 2 || !args[0]) invalidCommandArgs();
  const ref = args[0];
  const files: string[] = [];
  const data: Record<string, string> = {};
  const seenMimeTypes = new Set<string>();
  let hasData = false;
  let hasEmptyData = false;
  for (let index = 1; index < args.length;) {
    const flag = args[index];
    if (flag === '--path') {
      if (index + 1 >= args.length || !args[index + 1]) invalidCommandArgs();
      files.push(args[index + 1]);
      index += 2;
      continue;
    }
    if (flag === '--data') {
      if (
        hasEmptyData
        || index + 2 >= args.length
      ) {
        invalidCommandArgs();
      }
      const mime = args[index + 1];
      if (!mime || seenMimeTypes.has(mime)) invalidCommandArgs();
      seenMimeTypes.add(mime);
      data[mime] = args[index + 2];
      hasData = true;
      index += 3;
      continue;
    }
    if (flag === '--empty-data') {
      if (hasEmptyData || hasData) invalidCommandArgs();
      hasEmptyData = true;
      index += 1;
      continue;
    }
    invalidCommandArgs();
  }
  if (!files.length && !hasData && !hasEmptyData) invalidCommandArgs();
  return {
    ref,
    payload: {
      ...(files.length ? { files } : {}),
      ...(hasData || hasEmptyData ? { data } : {}),
    },
  };
}

function parseNetworkRequestsArgs(args: string[]): pwNetwork.NetworkRequestsOptions {
  let includeStatic = false;
  let filter: string | undefined;
  for (let index = 0; index < args.length;) {
    const flag = args[index];
    if (flag === '--static') {
      if (includeStatic) invalidCommandArgs();
      includeStatic = true;
      index += 1;
      continue;
    }
    if (flag === '--filter') {
      if (filter !== undefined || index + 1 >= args.length) invalidCommandArgs();
      filter = args[index + 1];
      try {
        new RegExp(filter);
      } catch {
        invalidCommandArgs('network filter 必须是有效的 JavaScript 正则表达式');
      }
      index += 2;
      continue;
    }
    invalidCommandArgs();
  }
  return {
    static: includeStatic,
    ...(filter !== undefined ? { filter } : {}),
  };
}

function parseConsoleArgs(args: string[]): {
  clear: boolean;
  level: pwConsole.ConsoleMessageLevel;
  all: boolean;
} {
  let clear = false;
  let all = false;
  let level: pwConsole.ConsoleMessageLevel = 'info';
  let hasLevel = false;
  for (let index = 0; index < args.length;) {
    const flag = args[index];
    if (flag === '--clear') {
      if (clear) invalidCommandArgs();
      clear = true;
      index += 1;
      continue;
    }
    if (flag === '--all') {
      if (all) invalidCommandArgs();
      all = true;
      index += 1;
      continue;
    }
    if (flag === '--level') {
      const value = args[index + 1];
      if (
        hasLevel
        || !pwConsole.CONSOLE_MESSAGE_LEVELS.includes(
          value as pwConsole.ConsoleMessageLevel,
        )
      ) {
        invalidCommandArgs('console level 仅支持 error/warning/info/debug');
      }
      level = value as pwConsole.ConsoleMessageLevel;
      hasLevel = true;
      index += 2;
      continue;
    }
    invalidCommandArgs();
  }
  if (clear && (all || hasLevel)) {
    invalidCommandArgs('console --clear 不能与读取参数组合');
  }
  return { clear, level, all };
}

type ScreenshotType = 'png' | 'jpeg';
type ScreenshotScale = 'css' | 'device';

function parseScreenshotArgs(args: string[]): {
  output: string;
  ref: string;
  type: ScreenshotType;
  fullPage: boolean;
  scale: ScreenshotScale;
  settled: boolean;
} {
  let output = '';
  let ref = '';
  let type: ScreenshotType = 'png';
  let fullPage = false;
  let scale: ScreenshotScale = 'css';
  let settled = false;
  let hasType = false;
  let hasScale = false;
  for (let index = 0; index < args.length;) {
    const flag = args[index];
    if (flag === '--ref') {
      if (ref || index + 1 >= args.length || !args[index + 1]) invalidCommandArgs();
      ref = args[index + 1];
      index += 2;
      continue;
    }
    if (flag === '--type') {
      const value = args[index + 1];
      if (hasType || (value !== 'png' && value !== 'jpeg')) {
        invalidCommandArgs('screenshot type 仅支持 png/jpeg');
      }
      type = value;
      hasType = true;
      index += 2;
      continue;
    }
    if (flag === '--full-page') {
      if (fullPage) invalidCommandArgs();
      fullPage = true;
      index += 1;
      continue;
    }
    if (flag === '--scale') {
      const value = args[index + 1];
      if (hasScale || (value !== 'css' && value !== 'device')) {
        invalidCommandArgs('screenshot scale 仅支持 css/device');
      }
      scale = value;
      hasScale = true;
      index += 2;
      continue;
    }
    if (flag === '--settled') {
      if (settled) invalidCommandArgs();
      settled = true;
      index += 1;
      continue;
    }
    if (flag?.startsWith('--') || output || !flag) invalidCommandArgs();
    output = flag;
    index += 1;
  }
  if (!output) invalidCommandArgs();
  if (ref && fullPage) {
    invalidCommandArgs('screenshot 的 full_page 与 ref 不能同时使用');
  }
  return { output, ref, type, fullPage, scale, settled };
}

function electronConsoleLevel(value: unknown): string {
  if (typeof value === 'number') {
    return ['verbose', 'info', 'warning', 'error'][value] ?? 'info';
  }
  const normalized = String(value ?? '').trim().toLowerCase();
  if (/^[0-3]$/.test(normalized)) {
    return ['verbose', 'info', 'warning', 'error'][Number(normalized)];
  }
  if (normalized === 'warn') return 'warning';
  if (new Set(['verbose', 'debug', 'info', 'warning', 'error']).has(normalized)) {
    return normalized;
  }
  return 'info';
}

function parseClickArgs(args: string[]): { ref: string; options: ClickOptions } {
  if (args.length < 1) invalidCommandArgs();
  const ref = args[0];
  const options: ClickOptions = {};
  const modifiers: NonNullable<ClickOptions['modifiers']> = [];
  const seen = new Set<string>();
  let positionX: number | undefined;
  let positionY: number | undefined;
  for (let index = 1; index < args.length;) {
    const flag = args[index];
    const value = args[index + 1];
    if (!value) invalidCommandArgs();
    if (flag === '--modifier') {
      if (!new Set(['Alt', 'Control', 'ControlOrMeta', 'Meta', 'Shift']).has(value)) {
        invalidCommandArgs();
      }
      modifiers.push(value as NonNullable<ClickOptions['modifiers']>[number]);
      index += 2;
      continue;
    }
    if (seen.has(flag)) invalidCommandArgs();
    seen.add(flag);
    if (flag === '--button') {
      if (!new Set(['left', 'right', 'middle']).has(value)) invalidCommandArgs();
      options.button = value as NonNullable<ClickOptions['button']>;
    } else if (flag === '--click-count') {
      options.clickCount = strictUnsignedInteger(value, 1, Number.MAX_SAFE_INTEGER);
    } else if (flag === '--delay-ms') {
      options.delayMs = strictUnsignedInteger(value, 0, Number.MAX_SAFE_INTEGER);
    } else if (flag === '--position-x') {
      positionX = strictClickPosition(value);
    } else if (flag === '--position-y') {
      positionY = strictClickPosition(value);
    } else {
      invalidCommandArgs();
    }
    index += 2;
  }
  if ((positionX === undefined) !== (positionY === undefined)) invalidCommandArgs();
  if (positionX !== undefined && positionY !== undefined) {
    options.position = { x: positionX, y: positionY };
  }
  if (modifiers.length) options.modifiers = modifiers;
  return { ref, options };
}

function parseFillArgs(args: string[]): {
  ref: string;
  value: string;
  submit: boolean;
  slowly: boolean;
} {
  if (args.length < 2) invalidCommandArgs();
  let submit = false;
  let slowly = false;
  for (const flag of args.slice(2)) {
    if (flag === '--submit' && !submit) submit = true;
    else if (flag === '--slowly' && !slowly) slowly = true;
    else invalidCommandArgs();
  }
  return { ref: args[0], value: args[1], submit, slowly };
}

function parseWaitArgs(args: string[]): WaitOptions {
  const options: WaitOptions = {};
  const seen = new Set<string>();
  for (let index = 0; index < args.length;) {
    const flag = args[index];
    const value = args[index + 1];
    if (!value || seen.has(flag)) invalidCommandArgs();
    seen.add(flag);
    if (flag === '--time-seconds') {
      const seconds = Number(value);
      if (!Number.isFinite(seconds) || seconds < 0) invalidCommandArgs();
      options.timeSeconds = seconds;
    } else if (flag === '--text') {
      options.text = value;
    } else if (flag === '--text-gone') {
      options.textGone = value;
    } else {
      invalidCommandArgs();
    }
    index += 2;
  }
  if (
    (options.timeSeconds ?? 0) <= 0
    && !options.text
    && !options.textGone
  ) {
    invalidCommandArgs();
  }
  return options;
}

function parseFillFormFields(value: unknown): FillFormField[] {
  if (!Array.isArray(value) || value.length < 1) {
    throw new BrowserHostError('批量表单 fields 至少包含一项', {
      code: 'invalid_fill_form',
    });
  }
  return value.map((raw, index) => {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new BrowserHostError(`批量表单第 ${index + 1} 项无效`, {
        code: 'invalid_fill_form',
      });
    }
    const field = raw as Record<string, unknown>;
    const type = field.type;
    const ref = field.ref;
    const selector = field.selector;
    const fail = (): never => {
      throw new BrowserHostError(`批量表单第 ${index + 1} 项无效`, {
        code: 'invalid_fill_form',
      });
    };
    if (
      typeof type !== 'string'
      || !['textbox', 'combobox', 'checkbox', 'radio', 'slider'].includes(type)
    ) {
      return fail();
    }
    const hasRef = typeof ref === 'string' && ref.length > 0;
    const hasSelector = (
      typeof selector === 'string'
      && selector.length > 0
    );
    if (hasRef === hasSelector) return fail();
    const targetKey = hasRef ? 'ref' : 'selector';
    const target = hasRef ? { ref: ref as string } : { selector: selector as string };
    if (type === 'textbox' || type === 'slider') {
      if (
        Object.keys(field).some((key) => !['type', targetKey, 'value'].includes(key))
        || Object.keys(field).length !== 3
        || typeof field.value !== 'string'
        || (type === 'slider' && !field.value)
      ) {
        return fail();
      }
      return { type, ...target, value: field.value } as FillFormField;
    }
    if (type === 'combobox') {
      if (
        Object.keys(field).some(
          (key) => !['type', targetKey, 'value', 'select_by'].includes(key),
        )
        || Object.keys(field).length !== 4
        || typeof field.value !== 'string'
        || (field.select_by !== 'label' && field.select_by !== 'value')
      ) {
        return fail();
      }
      return {
        type,
        ...target,
        value: field.value,
        selectBy: field.select_by,
      } as FillFormField;
    }
    if (
      Object.keys(field).some((key) => !['type', targetKey, 'value'].includes(key))
      || Object.keys(field).length !== 3
      || typeof field.value !== 'boolean'
    ) {
      return fail();
    }
    return {
      type: type as 'checkbox' | 'radio',
      ...target,
      value: field.value,
    } as FillFormField;
  });
}

interface UploadWithTriggerPayload {
  triggerSelector: string;
  inputSelector: string;
  files: string[];
}

function parseUploadWithTriggerPayload(
  params: Record<string, unknown>,
): UploadWithTriggerPayload {
  const triggerSelector = asString(
    params.trigger_selector,
    'trigger_selector',
    4_096,
  );
  const inputSelector = asString(
    params.input_selector,
    'input_selector',
    4_096,
  );
  if (!inputSelector) {
    throw new BrowserHostError('input_selector 不能为空', {
      code: 'invalid_selector',
    });
  }
  const rawFiles = params.files;
  if (!Array.isArray(rawFiles)) {
    throw new BrowserHostError('上传文件列表无效', { code: 'invalid_upload' });
  }
  const files = rawFiles.map((value, index) => {
    if (
      typeof value !== 'string'
      || !value
      || value.includes('\0')
    ) {
      throw new BrowserHostError(`files[${index}] 无效`, {
        code: 'invalid_upload',
      });
    }
    return value;
  });
  return { triggerSelector, inputSelector, files };
}

/** Capture the first FileChooser emitted after arming the listener. */
function createFileChooserCapture(page: Page): {
  arm: () => void;
  wait: (timeoutMs: number) => Promise<FileChooser | null>;
  dispose: () => void;
} {
  let armed = false;
  let captured: FileChooser | null = null;
  let resolveCaptured!: (chooser: FileChooser) => void;
  const capturedPromise = new Promise<FileChooser>((resolve) => {
    resolveCaptured = resolve;
  });
  const listener = (chooser: FileChooser): void => {
    if (captured) return;
    captured = chooser;
    resolveCaptured(chooser);
  };
  const dispose = (): void => {
    if (!armed) return;
    armed = false;
    page.off('filechooser', listener);
  };
  return {
    arm: () => {
      if (armed) return;
      armed = true;
      page.on('filechooser', listener);
    },
    wait: async (timeoutMs) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        return await Promise.race([
          capturedPromise,
          new Promise<null>((resolve) => {
            timer = setTimeout(resolve, timeoutMs, null);
          }),
        ]);
      } finally {
        if (timer) clearTimeout(timer);
        dispose();
      }
    },
    dispose,
  };
}

function runtimeKey(value: unknown): string {
  const key = asString(value, 'runtime_key', 64).trim();
  if (!RUNTIME_KEY_RE.test(key)) {
    throw new BrowserHostError('无效的浏览器账号标识', { code: 'invalid_runtime_key' });
  }
  return key;
}

function canonicalPath(value: string): string {
  const resolved = path.resolve(value);
  const missing: string[] = [];
  let cursor = resolved;
  while (true) {
    try {
      return path.join(realpathSync.native(cursor), ...missing.reverse());
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== 'ENOENT' && code !== 'ENOTDIR') {
        throw new BrowserHostError('无法确认浏览器路径的真实位置', { code: 'invalid_profile' });
      }
      const parent = path.dirname(cursor);
      if (parent === cursor) return resolved;
      missing.push(path.basename(cursor));
      cursor = parent;
    }
  }
}

function samePath(left: string, right: string): boolean {
  return process.platform === 'win32'
    ? left.toLocaleLowerCase() === right.toLocaleLowerCase()
    : left === right;
}

function pathKey(value: string): string {
  return process.platform === 'win32' ? value.toLocaleLowerCase() : value;
}

function validateProfileOwnership(profile: string, expectedRuntimeKey?: string): void {
  const profileName = path.basename(profile);
  const browserDir = path.dirname(profile);
  const ownerDir = path.dirname(browserDir);
  const accountsDir = path.dirname(ownerDir);
  const ownerMatch = /^acct_([0-9a-f]{16})$/i.exec(path.basename(ownerDir));
  if (
    profileName.toLocaleLowerCase() !== 'profile'
    || path.basename(browserDir).toLocaleLowerCase() !== 'browser'
    || path.basename(accountsDir).toLocaleLowerCase() !== 'accounts'
    || !ownerMatch
  ) {
    throw new BrowserHostError('浏览器 Profile 不属于账号隔离目录', {
      code: 'invalid_profile',
    });
  }
  if (expectedRuntimeKey && ownerMatch[1].slice(0, 12).toLocaleLowerCase() !== expectedRuntimeKey.slice(5)) {
    throw new BrowserHostError('浏览器 Profile 与账号标识不匹配', {
      code: 'profile_owner_mismatch',
    });
  }
}

function profilePath(value: unknown, expectedRuntimeKey?: string): string {
  const raw = asString(value, 'profile_dir', 4096).trim();
  if (!raw || !path.isAbsolute(raw)) {
    throw new BrowserHostError('浏览器 Profile 必须是绝对路径', { code: 'invalid_profile' });
  }
  const canonical = canonicalPath(raw);
  validateProfileOwnership(canonical, expectedRuntimeKey);
  return canonical;
}

function sessionHash(sessionId: string): string {
  return createHash('sha256').update(sessionId, 'utf8').digest('hex').slice(0, 32);
}

function normalizeMode(value: unknown): ControlMode {
  if (value === 'ai' || value === 'human' || value === 'paused') return value;
  throw new BrowserHostError('浏览器控制模式无效', { code: 'invalid_mode' });
}

function normalizedText(value: unknown, _maximum?: number): string {
  return String(value ?? '');
}

function resolveCommandTimeoutMs(value: unknown, deadlineValue?: unknown): number {
  let requested = ACTION_TIMEOUT_MS;
  if (value !== undefined) {
    if (
      typeof value !== 'number'
      || !Number.isFinite(value)
      || value <= 0
      || value > Number.MAX_SAFE_INTEGER
    ) {
      throw new BrowserHostError('command_timeout_ms 必须是正有限数', {
        code: 'invalid_timeout',
      });
    }
    requested = Math.max(1, Math.ceil(value));
  }
  if (deadlineValue === undefined) return requested;
  if (
    typeof deadlineValue !== 'number'
    || !Number.isSafeInteger(deadlineValue)
    || deadlineValue <= 0
  ) {
    throw new BrowserHostError('command_deadline_ms 必须是正安全整数', {
      code: 'invalid_timeout',
    });
  }
  const remaining = Math.floor(deadlineValue - Date.now());
  if (remaining <= 0) {
    throw new BrowserHostError('浏览器命令在宿主执行前已超过截止时间', {
      code: 'command_timeout',
      uncertain: false,
    });
  }
  return Math.max(1, Math.min(requested, remaining));
}

function remainingCommandTimeoutMs(deadlineAt: number): number {
  const remaining = Math.floor(deadlineAt - Date.now());
  if (remaining <= 0) {
    throw new BrowserHostError('浏览器命令已超过截止时间', {
      code: 'command_timeout',
      uncertain: false,
    });
  }
  return remaining;
}

async function withDeadline<T>(
  operation: Promise<T>,
  timeoutMs: number,
  error: () => Error,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(error()), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function safeUrl(value: unknown, { allowBlank = true }: { allowBlank?: boolean } = {}): string {
  const raw = asString(value, 'url').trim();
  if (allowBlank && raw === 'about:blank') return raw;
  if (!raw) {
    throw new BrowserHostError('浏览器 URL 无效', { code: 'invalid_url' });
  }
  let candidate = raw;
  const bareLocalHost = /^(?:localhost|127(?:\.[0-9]{1,3}){3}|\[::1\])(?::[0-9]+)?(?:[/?#]|$)/i
    .test(raw);
  const barePublicHost = /^(?:(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+)(?::[0-9]+)?(?:[/?#]|$)/
    .test(raw);
  if (bareLocalHost || barePublicHost) {
    candidate = `${bareLocalHost ? 'http' : 'https'}://${raw}`;
  }
  try {
    // Match a browser address bar / Playwright MCP: bare public hosts default
    // to HTTPS, while local development hosts default to HTTP. Preserve
    // explicit standard and custom schemes unchanged.
    new URL(candidate);
  } catch {
    candidate = `${bareLocalHost ? 'http' : 'https'}://${candidate}`;
  }
  // `new URL("localhost:3000")` treats `localhost` as a custom scheme. A
  // human-entered localhost host is overwhelmingly the HTTP development case.
  if (bareLocalHost) {
    candidate = `http://${raw}`;
  }
  try {
    return new URL(candidate).toString();
  } catch {
    throw new BrowserHostError('浏览器 URL 无效', { code: 'invalid_url' });
  }
}

function httpOrigin(value: string): string {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.origin : '';
  } catch {
    return '';
  }
}

function publicUrl(value: string): string {
  return String(value ?? '');
}

function publicConsoleText(value: unknown): string {
  return String(value ?? '');
}

function downloadTimeoutMs(params: Record<string, unknown>): number {
  const candidates: number[] = [];
  if (params.timeout_ms !== undefined) {
    candidates.push(
      asPositiveInteger(params.timeout_ms, 'timeout_ms', Number.MAX_SAFE_INTEGER),
    );
  }
  if (params.command_timeout_ms !== undefined) {
    candidates.push(
      asPositiveInteger(
        params.command_timeout_ms,
        'command_timeout_ms',
        Number.MAX_SAFE_INTEGER,
      ),
    );
  }
  if (params.deadline_ms !== undefined) {
    const deadline = asPositiveInteger(params.deadline_ms, 'deadline_ms', Number.MAX_SAFE_INTEGER);
    candidates.push(deadline - Date.now());
  }
  if (params.command_deadline_ms !== undefined) {
    const deadline = asPositiveInteger(
      params.command_deadline_ms,
      'command_deadline_ms',
      Number.MAX_SAFE_INTEGER,
    );
    candidates.push(deadline - Date.now());
  }
  if (!candidates.length) candidates.push(DEFAULT_DOWNLOAD_TIMEOUT_MS);
  const available = Math.min(...candidates) - DOWNLOAD_DEADLINE_MARGIN_MS;
  if (!Number.isFinite(available) || available <= 0) {
    throw new BrowserHostError('下载授权已超过调用截止时间', {
      code: 'download_deadline_expired',
    });
  }
  return Math.max(100, Math.floor(available));
}

function taskDownloadDirectory(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  const raw = asString(value, 'download_dir').trim();
  if (!path.isAbsolute(raw)) {
    throw new BrowserHostError('download_dir 必须是绝对路径', {
      code: 'invalid_download_path',
    });
  }
  return canonicalPath(raw);
}

/**
 * 环形缓冲的上限。console 与 network 记录都走这里。
 *
 * 名字必须与行为一致：这个函数曾被改成裸 `items.push(item)` 而**名字还叫
 * pushBounded**，于是长会话里主进程内存无界增长——一个自动刷新的页面每秒打
 * 几条 console，跑一天就是几十万条对象常驻。读代码的人看到 `pushBounded`
 * 会以为有界，这比直接叫 push 更危险。
 */
/**
 * console / network 环形缓冲的**失控护栏**。
 *
 * 同样定得很高：截断调试历史会让模型看不到几百条之前的那个真正的错误
 * （`retains exact unbounded debug metadata` 那条用例要求的正是这种完整性）。
 * 但一个自动刷新的页面每秒打几条 console，跑一天就是几十万条对象常驻主进程。
 *
 * 名字与行为必须一致：这个函数曾被改成裸 `items.push(item)` 而**名字还叫
 * pushBounded**——读代码的人以为有界，比直接叫 push 更危险。
 */
const MAX_RING_ENTRIES = 20_000;

/**
 * 单会话标签页的**失控护栏**，不是产品策略。
 *
 * 刻意定得很高：真实浏览器不会卡在 8 个标签页，一个合法工作流开几十个弹窗
 * 是正常的（`does not impose a product tab limit` 那条用例就要求 66 个能开）。
 * 但每个标签页是一个真实 WebContentsView——独立渲染进程 + 一份 CDP 会话，
 * 完全不设上限时一段失控的 `window.open` 循环会把主进程内存耗尽，应用整个卡死。
 *
 * 应用崩掉同样伤成功率，所以这个数字的取法是"正常用永远碰不到，失控一定撞上"。
 */
const MAX_TABS_PER_SESSION = 512;

/** Mirrors BrowserConfig's default for callers using an older RPC shape. */
const DEFAULT_MAX_TRANSFER_BYTES = 100 * 1024 * 1024;

/** 本地 HTML 预览的字节上限。无上限时一个大报表就能让主进程 OOM。 */
const MAX_ARTIFACT_BYTES = 20 * 1024 * 1024;

function pushBounded<T>(items: T[], item: T): void {
  items.push(item);
  if (items.length > MAX_RING_ENTRIES) {
    items.splice(0, items.length - MAX_RING_ENTRIES);
  }
}

function cdpValue(value: AxValue | undefined): unknown {
  return value?.value;
}

function axProperty(node: AxNode, name: string): unknown {
  return cdpValue(node.properties?.find((property) => property.name === name)?.value);
}

type DomDescription = {
  nodeName?: unknown;
  attributes?: unknown;
};

function domDescriptionAttributes(node: DomDescription): Map<string, string> {
  if (node.attributes instanceof Map) {
    return new Map([...node.attributes.entries()].map(([name, value]) => [
      String(name).toLocaleLowerCase(),
      String(value),
    ]));
  }
  const values = Array.isArray(node.attributes) ? node.attributes.map(String) : [];
  const attributes = new Map<string, string>();
  for (let index = 0; index + 1 < values.length; index += 2) {
    attributes.set(values[index]!.toLocaleLowerCase(), values[index + 1]!);
  }
  return attributes;
}

/** Render an editable AX value without ever including password input contents. */
export function snapshotEditableValue(node: AxNode, domNode: DomDescription): string {
  const editable = axProperty(node, 'editable');
  const editableToken = typeof editable === 'string' ? editable.toLocaleLowerCase() : '';
  if (editable !== true && editableToken !== 'plaintext' && editableToken !== 'richtext') return '';
  if (domDescriptionAttributes(domNode).get('type')?.toLocaleLowerCase() === 'password') return '';
  const value = cdpValue(node.value);
  if (typeof value !== 'string' || !value) return '';
  return ` value=${JSON.stringify(value.slice(0, 100))}`;
}

/** Compactly describe a DOM node that intercepted a browser hit test. */
export function describeHitNode(node: DomDescription): string {
  const tag = String(node.nodeName ?? '').trim().toLocaleLowerCase() || 'unknown';
  if (tag === 'unknown') return tag;
  const attributes = domDescriptionAttributes(node);
  const id = attributes.get('id');
  const classes = (attributes.get('class') ?? '').split(/\s+/u).filter(Boolean);
  const description = `${tag}${id ? `#${id}` : ''}${classes.map((name) => `.${name}`).join('')}`;
  return description.slice(0, 160);
}

function ensureWithin(child: string, parent: string): boolean {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

function artifactUrl(token: string): string {
  return `${ARTIFACT_SCHEME}://${token}/index.html`;
}

function sameFileIdentity(
  left: { dev: number; ino: number },
  right: { dev: number; ino: number },
): boolean {
  return left.dev === right.dev && left.ino === right.ino;
}

/**
 * Owns untrusted remote WebContentsView instances in Electron's main process.
 *
 * The renderer is only allowed to position an already-authenticated tab. Browser
 * creation and automation enter through the gateway's account-bound RPC socket.
 */
export class BrowserHost extends EventEmitter {
  private readonly owners = new Map<string, BrowserOwner>();
  private readonly profileBindings = new Map<string, string>();
  private readonly tabsByTarget = new Map<string, { owner: BrowserOwner; tab: BrowserTab }>();
  private readonly tabsByWebContentsId = new Map<number, { owner: BrowserOwner; tab: BrowserTab }>();
  /**
   * Preserve the logical session of a public Page invocation after that Page
   * closes. BrowserContext remains usable with zero pages, so
   * `await page.close(); await context.newPage()` must not lose its owner/session.
   */
  private readonly pageLifecycleOrigins = new WeakMap<
    object,
    {
      owner: BrowserOwner;
      sessionHash: string;
      mode: ControlMode;
      webContentsId: number;
      downloadDir: string;
    }
  >();
  private readonly ownerQueues = new Map<string, Promise<void>>();
  private readonly ownerEpochs = new Map<string, number>();
  private panel: {
    owner: BrowserOwner;
    tab: BrowserTab;
    window: BrowserWindow;
    bounds: Rectangle;
  } | null = null;
  private disposed = false;

  constructor(private readonly getWindow: () => BrowserWindow | null) {
    super();
  }

  async handleRpc(requestValue: unknown): Promise<unknown> {
    const request = asRecord(requestValue, 'RPC 请求') as unknown as BrowserRpcRequest;
    const key = runtimeKey(request.runtime_key);
    const method = asString(request.method, 'RPC method', 80).trim();
    const params = asRecord(request.params ?? {}, 'RPC params');
    if (!method) throw new BrowserHostError('RPC method 不能为空', { code: 'invalid_request' });

    this.assertUsable();
    if (method === 'deny_downloads') return this.denyDownloads(key, params);
    if (method === 'close_owner') return this.closeOwner(key, params);
    if (method === 'clear_owner_data') return this.clearOwnerData(key, params);

    return this.enqueue(key, async () => {
      this.assertUsable();
      switch (method) {
        case 'execute':
          return this.execute(key, params);
        case 'page_guard':
          return this.pageGuard(key, params);
        case 'page_images':
          return this.pageImages(key, params);
        case 'coordinate_click':
          return this.coordinateClick(key, params);
        case 'close_target':
          return this.closeTargetRpc(key, params);
        case 'download':
          return this.download(key, params);
        case 'set_mode':
          return this.setMode(key, params);
        case 'doctor':
          return {
            ok: true,
            runtime: 'electron',
            engine: 'WebContentsView',
          };
        default:
          throw new BrowserHostError('不支持的浏览器 RPC method', { code: 'unsupported_method' });
      }
    });
  }

  setPanel(request: BrowserPanelRequest): void {
    this.assertUsable();
    const owner = this.requireOwner(runtimeKey(request.runtimeKey));
    const tab = this.requirePanelTab(owner, request.sessionId, request.tabLabel);
    if (tab.crashed || tab.view.webContents.isDestroyed()) {
      if (this.panel?.tab === tab) this.hidePanel();
      throw new BrowserHostError('浏览器标签页已停止', { code: 'tab_stopped' });
    }
    const mode = normalizeMode(request.mode);
    const staleHumanPopup = Boolean(
      this.panel?.owner === owner
      && this.panel.tab !== tab
      && this.panel.tab.mode === 'human'
      && this.panel.tab.sessionHash === tab.sessionHash
      && owner.activeTabId === this.panel.tab.tabId
      && this.popupDescendsFrom(owner, this.panel.tab, tab)
    );
    if (staleHumanPopup) {
      if (mode !== 'human' || tab.mode !== 'human') {
        this.hidePanel();
        throw new BrowserHostError('浏览器面板控制模式与人工弹窗状态不一致', {
          code: 'panel_mode_mismatch',
        });
      }
      const popupPanel = this.panel!;
      const window = this.getWindow();
      if (!request.visible || !window || window.isDestroyed()) {
        this.hidePanel();
        return;
      }
      const bounds = this.clampBounds(request.bounds, window);
      if (!bounds) {
        this.hidePanel();
        return;
      }
      if (popupPanel.window !== window) {
        this.detachPanel(popupPanel);
        // 让出自动化宿主的挂载，避免同一个 view 有两处记账。
        owner.engine.releaseToPanel(popupPanel.tab.view);
        window.contentView.addChildView(popupPanel.tab.view);
      }
      popupPanel.tab.view.setBounds(bounds);
      popupPanel.tab.view.setVisible(true);
      this.panel = { ...popupPanel, window, bounds };
      popupPanel.tab.view.webContents.focus();
      return;
    }
    // 面板显示的 tab 与 agent 正在操作的 tab 是两件事，不能耦合：
    // owner.activeTabId 是账号级唯一值，由「最后一个动作的 session」决定。多个会话
    // 各自跑浏览器任务时它会来回翻，若要求面板只能挂 activeTabId，用户切到另一个会话
    // 就会被拒（inactive_panel_tab）。requirePanelTab 已按 sessionHash 校验过该 tab
    // 属于请求的会话，会话隔离仍然成立；挂载本身会让该 view 渲染，也不需要它是
    // 原生「当前」tab。各会话的 agent 在自己动作前会通过 _select 重新选中自己的 tab。
    if (tab.mode !== mode) {
      if (this.panel?.tab === tab) this.hidePanel();
      throw new BrowserHostError('浏览器面板控制模式与宿主状态不一致', {
        code: 'panel_mode_mismatch',
      });
    }
    const window = this.getWindow();
    if (!request.visible || !window || window.isDestroyed()) {
      this.hidePanel();
      return;
    }
    const bounds = this.clampBounds(request.bounds, window);
    if (!bounds) {
      this.hidePanel();
      return;
    }

    const panelMoved = Boolean(
      this.panel
      && (
        this.panel.owner !== owner
        || this.panel.tab !== tab
        || this.panel.window !== window
      )
    );
    if (panelMoved && this.panel) {
      this.detachPanel(this.panel);
      this.panel = null;
    }
    if (!this.panel) {
      owner.engine.releaseToPanel(tab.view);
      window.contentView.addChildView(tab.view);
    }
    tab.view.setBounds(bounds);
    tab.view.setVisible(true);
    this.panel = { owner, tab, window, bounds };
    if (mode === 'human') tab.view.webContents.focus();
    else window.webContents.focus();
  }

  hidePanel(): void {
    if (!this.panel) return;
    this.detachPanel(this.panel);
    this.panel = null;
  }

  getPanelNavigation(request: BrowserPanelCaptureRequest): BrowserPanelNavigation {
    this.assertUsable();
    const owner = this.requireOwner(runtimeKey(request.runtimeKey));
    const tab = this.requirePanelTab(owner, request.sessionId, request.tabLabel);
    if (tab.crashed || tab.view.webContents.isDestroyed()) {
      throw new BrowserHostError('浏览器标签页已停止', { code: 'tab_stopped' });
    }
    const contents = tab.view.webContents;
    // Electron 43 已移除 webContents.canGoBack/canGoForward，必须走 navigationHistory。
    const history = contents.navigationHistory;
    return {
      url: publicUrl(contents.getURL() || 'about:blank'),
      title: normalizedText(contents.getTitle(), 2048),
      can_go_back: history.canGoBack(),
      can_go_forward: history.canGoForward(),
    };
  }

  async capturePanel(
    runtimeKeyOrRequest: string | BrowserPanelCaptureRequest,
    sessionId?: string,
    tabLabel?: string,
    modalRaceArmed = false,
  ): Promise<BrowserPanelCapture> {
    const request =
      typeof runtimeKeyOrRequest === 'string'
        ? { runtimeKey: runtimeKeyOrRequest, tabLabel: tabLabel ?? '', sessionId: sessionId ?? '' }
        : runtimeKeyOrRequest;
    const owner = this.requireOwner(runtimeKey(request.runtimeKey));
    const tab = this.requirePanelTab(owner, request.sessionId, request.tabLabel);
    // 同 setPanel：截图跟随「请求的会话的 tab」，不跟随 agent 最后操作的 activeTabId，
    // 否则另一个会话的 agent 一动，当前会话的面板截图就被拒。
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止截取浏览器画面', {
        code: 'capture_blocked',
      });
    }
    if (tab.crashed || tab.view.webContents.isDestroyed()) {
      throw new BrowserHostError('浏览器标签页已停止', { code: 'tab_stopped' });
    }
    if (!modalRaceArmed) {
      this.releaseSettledModalAction(owner, tab.sessionHash);
      return this.withSessionModalRace(
        owner,
        tab,
        () => this.capturePanel(request, undefined, undefined, true),
      );
    }
    const image = await tab.view.webContents.capturePage();
    const size = image.getSize();
    return {
      dataUrl: image.toDataURL(),
      width: size.width,
      height: size.height,
    };
  }

  handleLogin(
    event: PreventableEvent,
    webContents: WebContents | null,
    authInfo: AuthInfo,
    callback: (username?: string, password?: string) => void,
  ): boolean {
    if (!webContents || !authInfo.isProxy) return false;
    const found = this.tabsByWebContentsId.get(webContents.id);
    const proxy = found?.owner.proxy;
    if (
      !proxy ||
      proxy.host.toLocaleLowerCase() !== String(authInfo.host).toLocaleLowerCase() ||
      proxy.port !== Number(authInfo.port)
    ) {
      return false;
    }
    event.preventDefault();
    callback(proxy.username, proxy.password);
    return true;
  }

  async dispose(): Promise<void> {
    if (this.disposed) return;
    this.disposed = true;
    this.hidePanel();
    let firstError: unknown;
    for (const owner of [...this.owners.values()]) {
      try {
        await this.destroyOwner(owner);
      } catch (error) {
        firstError ??= error;
      }
    }
    this.owners.clear();
    this.profileBindings.clear();
    this.tabsByTarget.clear();
    this.tabsByWebContentsId.clear();
    this.ownerQueues.clear();
    this.ownerEpochs.clear();
    this.removeAllListeners();
    if (firstError) throw firstError;
  }

  private assertUsable(): void {
    if (this.disposed) {
      throw new BrowserHostError('桌面浏览器宿主已停止', {
        code: 'host_stopped',
        browserStopped: true,
      });
    }
  }

  private async enqueue<T>(key: string, operation: () => Promise<T>): Promise<T> {
    const epoch = this.ownerEpochs.get(key) ?? 0;
    const previous = this.ownerQueues.get(key) ?? Promise.resolve();
    let release!: () => void;
    const next = new Promise<void>((resolve) => {
      release = resolve;
    });
    const tail = previous.then(() => next, () => next);
    this.ownerQueues.set(key, tail);
    await previous.catch(() => undefined);
    if ((this.ownerEpochs.get(key) ?? 0) !== epoch) {
      release();
      throw new BrowserHostError('浏览器操作已被生命周期命令取消', {
        code: 'operation_preempted',
        uncertain: true,
      });
    }
    try {
      const result = await operation();
      if ((this.ownerEpochs.get(key) ?? 0) !== epoch) {
        throw new BrowserHostError('浏览器操作已被生命周期命令取消', {
          code: 'operation_preempted',
          uncertain: true,
        });
      }
      return result;
    } finally {
      release();
      void tail.finally(() => {
        if (this.ownerQueues.get(key) === tail) this.ownerQueues.delete(key);
      });
    }
  }

  private preemptOwnerQueue(key: string): void {
    this.ownerEpochs.set(key, (this.ownerEpochs.get(key) ?? 0) + 1);
    this.ownerQueues.delete(key);
  }

  private requireOwner(key: string): BrowserOwner {
    const owner = this.owners.get(key);
    if (!owner || owner.lifecycle !== 'active') {
      throw new BrowserHostError('账号浏览器尚未启动', {
        code: 'owner_not_running',
        browserStopped: !owner || owner.lifecycle === 'closing',
      });
    }
    return owner;
  }

  private async ensureOwner(
    key: string,
    profile: string,
    proxyUrl: string,
  ): Promise<BrowserOwner> {
    const existing = this.owners.get(key);
    if (existing) {
      if (existing.lifecycle !== 'active') {
        throw new BrowserHostError('账号浏览器正在执行生命周期操作', {
          code: 'owner_busy',
          browserStopped: existing.lifecycle === 'closing',
        });
      }
      if (!samePath(existing.profilePath, profile)) {
        throw new BrowserHostError('账号浏览器 Profile 与已启动实例不一致', {
          code: 'profile_mismatch',
        });
      }
      await this.applyProxy(existing, proxyUrl);
      return existing;
    }

    const bindingKey = pathKey(profile);
    const boundRuntime = this.profileBindings.get(bindingKey);
    if (boundRuntime && boundRuntime !== key) {
      throw new BrowserHostError('浏览器 Profile 已绑定其他账号', {
        code: 'profile_owner_mismatch',
      });
    }

    const electron = electronSession.fromPath(profile, { cache: true });
    const owner: BrowserOwner = {
      runtimeKey: key,
      profilePath: profile,
      session: electron,
      tabs: new Map(),
      activeTabId: '',
      tabCounter: 0,
      popupOrdinals: new Map(),
      proxy: null,
      downloadGrant: null,
      downloadListener: null,
      downloadEventSequence: 0,
      genericDownloadCaptures: [],
      reservedDownloadPaths: new Set(),
      artifacts: new Map(),
      artifactProtocolRegistered: false,
      pendingModalActions: new Map(),
      modalWaiters: new Map(),
      lifecycle: 'active',
      engine: new PlaywrightEngine(),
    };
    owner.engine.setInputCommandLeaseHook(({ view }) => {
      if (view.webContents.isDestroyed()) {
        throw new BrowserHostError('标签页已停止，拒绝发送自动化输入', {
          code: 'tab_stopped',
          browserStopped: true,
        });
      }
      const found = this.tabsByWebContentsId.get(view.webContents.id);
      if (
        !found
        || found.owner !== owner
        || found.tab.view !== view
        || found.tab.crashed
        || found.tab.mode !== 'ai'
      ) {
        // Input.* is the last irreversible boundary. Never let a stale alias,
        // a cross-owner view, or a takeover race reach Electron's debugger.
        throw new BrowserHostError('自动化输入租约与当前账号/控制模式不一致', {
          code: 'control_mode_blocked',
        });
      }
      const leasedTab = found.tab;
      let released = false;
      leasedTab.automationDepth += 1;
      return () => {
        if (released) return;
        released = true;
        leasedTab.automationDepth = Math.max(0, leasedTab.automationDepth - 1);
      };
    });
    owner.engine.setModalStateHook((view, kind) => {
      if (view.webContents.isDestroyed()) return;
      const found = this.tabsByWebContentsId.get(view.webContents.id);
      if (!found || found.owner !== owner || found.tab.view !== view) return;
      this.notifySessionModal(owner, found.tab, kind);
    });
    owner.engine.setChildSessionLifecycleHook(async (context) => {
      await this.handleChildSessionLifecycle(owner, context);
    });
    owner.engine.setPageLifecycleHook({
      createPage: async (context) => {
        if (owner.lifecycle !== 'active') {
          throw new BrowserHostError('账号浏览器正在执行生命周期操作', {
            code: 'owner_busy',
            browserStopped: owner.lifecycle === 'closing',
          });
        }
        if (context.browserContextId) {
          throw new BrowserHostError('当前 Electron 引擎只支持默认 BrowserContext', {
            code: 'unsupported_browser_context',
          });
        }

        const origin = context.sourceView
          ? this.pageLifecycleOrigins.get(context.sourceView)
          : undefined;
        const liveSource = origin
          ? this.tabsByWebContentsId.get(origin.webContentsId)
          : undefined;
        if (
          (liveSource && liveSource.owner !== owner)
          || (origin && origin.owner !== owner)
        ) {
          throw new BrowserHostError('Playwright 页面生命周期来源不属于当前账号', {
            code: 'foreign_tab',
          });
        }
        const active = owner.tabs.get(owner.activeTabId);
        const sourceTab = (
          liveSource?.owner === owner
          && liveSource.tab.view === context.sourceView
        )
          ? liveSource.tab
          : undefined;
        const sessionHash = sourceTab?.sessionHash
          ?? origin?.sessionHash
          ?? active?.sessionHash
          ?? '';
        const mode = sourceTab?.mode ?? origin?.mode ?? active?.mode ?? 'ai';
        if (!sessionHash) {
          throw new BrowserHostError('Playwright 页面创建缺少逻辑会话来源', {
            code: 'invalid_target',
          });
        }

        const deadlineAt = context.deadlineAt > 0
          ? context.deadlineAt
          : Date.now() + ACTION_TIMEOUT_MS;
        const requestedURL = safeUrl(context.url || 'about:blank');
        const tab = this.createTab(
          owner,
          `s${sessionHash}-${owner.tabCounter + 1}`,
          sessionHash,
          '',
          mode,
        );
        this.setTabDownloadDir(
          tab,
          sourceTab?.downloadDir
            ?? origin?.downloadDir
            ?? active?.downloadDir
            ?? '',
        );
        try {
          await this.initializeNewTab(tab, deadlineAt);
          if (requestedURL !== 'about:blank') {
            await withDeadline(
              tab.view.webContents.loadURL(requestedURL),
              remainingCommandTimeoutMs(deadlineAt),
              () => {
                tab.view.webContents.stop();
                return new BrowserHostError('Playwright 新页面导航超过命令截止时间', {
                  code: 'command_timeout',
                  uncertain: false,
                });
              },
            );
          }
          const targetId = await owner.engine.waitForViewTarget(
            tab.view,
            remainingCommandTimeoutMs(deadlineAt),
          );
          if (!owner.activeTabId) owner.activeTabId = tab.tabId;
          return targetId;
        } catch (error) {
          // createTarget is transactional at the Host boundary. A failed
          // document/debugger/attach phase must not leave an untracked view.
          this.closeTab(owner, tab);
          throw error;
        }
      },
      closePage: async (context) => {
        const origin = this.pageLifecycleOrigins.get(context.view);
        const found = origin
          ? this.tabsByWebContentsId.get(origin.webContentsId)
          : undefined;
        if (!found || found.owner !== owner || found.tab.view !== context.view) {
          throw new BrowserHostError('Playwright 要关闭的页面已不存在或不属于当前账号', {
            code: 'foreign_tab',
          });
        }
        this.closeTab(owner, found.tab);
      },
    });
    this.owners.set(key, owner);
    this.profileBindings.set(bindingKey, key);
    try {
      await this.applyProxy(owner, proxyUrl);
      this.configureSession(owner);
      return owner;
    } catch (error) {
      this.owners.delete(key);
      this.profileBindings.delete(bindingKey);
      this.detachSession(owner);
      await owner.engine.dispose().catch(() => undefined);
      throw error;
    }
  }

  private configureSession(owner: BrowserOwner): void {
    owner.session.setPermissionCheckHandler(() => true);
    owner.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(true));
    const listener: DownloadListener = (event, item, contents) => {
      this.handleWillDownload(owner, event, item, contents);
    };
    owner.downloadListener = listener;
    owner.session.on('will-download', listener);
  }

  private detachSession(owner: BrowserOwner): void {
    const listener = owner.downloadListener;
    if (listener) {
      owner.downloadListener = null;
      owner.session.removeListener('will-download', listener);
    }
    if (owner.artifactProtocolRegistered) {
      owner.session.protocol.unhandle(ARTIFACT_SCHEME);
      owner.artifactProtocolRegistered = false;
    }
    owner.artifacts.clear();
  }

  private parseProxy(proxyUrl: string): ProxyAuthState | null {
    if (!proxyUrl) {
      throw new BrowserHostError('浏览器网络策略代理不可用', {
        code: 'proxy_required',
      });
    }
    let parsed: URL;
    try {
      parsed = new URL(proxyUrl);
    } catch {
      throw new BrowserHostError('浏览器代理配置无效', { code: 'invalid_proxy' });
    }
    if (!['http:', 'https:', 'socks4:', 'socks5:'].includes(parsed.protocol)) {
      throw new BrowserHostError('浏览器代理协议无效', {
        code: 'invalid_proxy',
      });
    }
    const host = parsed.hostname.replace(/^\[|\]$/g, '').toLocaleLowerCase();
    const defaultPort = parsed.protocol === 'https:'
      ? 443
      : parsed.protocol === 'http:'
        ? 80
        : 1080;
    const port = Number(parsed.port || defaultPort);
    parsed.username = '';
    parsed.password = '';
    parsed.pathname = '';
    parsed.search = '';
    parsed.hash = '';
    return {
      proxyRules: `${parsed.protocol}//${parsed.hostname}:${port}`,
      host,
      port,
      username: decodeURIComponent(new URL(proxyUrl).username),
      password: decodeURIComponent(new URL(proxyUrl).password),
    };
  }

  private async applyProxy(owner: BrowserOwner, proxyUrl: string): Promise<void> {
    const next = this.parseProxy(proxyUrl);
    if (
      owner.proxy?.proxyRules === next?.proxyRules &&
      owner.proxy?.username === next?.username &&
      owner.proxy?.password === next?.password
    ) {
      return;
    }
    try {
      await owner.session.setProxy(next
        ? {
            mode: 'fixed_servers',
            proxyRules: next.proxyRules,
          }
        : { mode: 'direct' });
      await owner.session.closeAllConnections();
    } catch (error) {
      throw new BrowserHostError(
        `无法应用浏览器网络配置：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'proxy_unavailable' },
      );
    }
    // Only publish the new proxy as active after Chromium has accepted it and
    // every connection created under the previous policy has been closed. If
    // either step fails, a later request must retry instead of trusting stale
    // bookkeeping.
    owner.proxy = next;
  }

  private async execute(key: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const profile = profilePath(params.profile_dir, key);
    const proxy = asString(params.proxy_url, 'proxy_url', 4096).trim();
    const owner = await this.ensureOwner(key, profile, proxy);
    const command = asString(params.command, 'browser command', 80).trim();
    const requestedDownloadDir = taskDownloadDirectory(params.download_dir);
    const requestedTransferLimit = transferLimit(params.max_transfer_bytes);
    const rawArgs = params.args ?? [];
    if (!Array.isArray(rawArgs)) {
      throw new BrowserHostError('浏览器命令参数无效', { code: 'invalid_request' });
    }
    const args = rawArgs.map((item, index) => asString(item, `args[${index}]`));

    try {
      let operation = (): Promise<unknown> => (
        this.executeCommand(owner, command, args, params)
      );
      if (command !== 'tab') {
        const requestedTarget = typeof params.target_id === 'string'
          ? params.target_id.trim()
          : '';
        const soleTab = owner.tabs.size === 1
          ? owner.tabs.values().next().value
          : undefined;
        const targetId = requestedTarget || soleTab?.targetId || '';
        if (targetId) {
          const tab = this.targetTab(owner, targetId);
          if (requestedDownloadDir) {
            this.setTabDownloadDir(tab, requestedDownloadDir);
          }
          tab.downloadMaxBytes = requestedTransferLimit;
          const inner = operation;
          operation = () => this.withGenericDownloadCapture(
            owner,
            tab,
            resolveCommandTimeoutMs(
              params.command_timeout_ms,
              params.command_deadline_ms,
            ),
            inner,
          );
        }
      }
      const data = await operation();
      return { success: true, data };
    } catch (error) {
      if (error instanceof BrowserHostError) throw error;
      throw new BrowserHostError(error instanceof Error ? error.message : '浏览器操作失败', {
        code: 'command_failed',
        uncertain: asBoolean(params.mutating),
      });
    }
  }

  private async withGenericDownloadCapture(
    owner: BrowserOwner,
    tab: BrowserTab,
    timeoutMs: number,
    operation: () => Promise<unknown>,
  ): Promise<unknown> {
    if (!tab.downloadDir) return operation();
    const capture: GenericDownloadCapture = {
      sessionHash: tab.sessionHash,
      sourceTabId: tab.tabId,
      publicSignals: 0,
      downloads: [],
      nativeWaiters: new Set(),
    };
    owner.genericDownloadCaptures.push(capture);
    let page: Page | undefined;
    const onPublicDownload = (): void => {
      capture.publicSignals += 1;
    };
    try {
      page = await owner.engine.pageForView(tab.view, timeoutMs).catch(() => undefined);
      page?.on('download', onPublicDownload);
      const result = await operation();
      if (capture.publicSignals > capture.downloads.length) {
        await new Promise<void>((resolve) => {
          let settled = false;
          const finish = (): void => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            capture.nativeWaiters.delete(finish);
            resolve();
          };
          const timer = setTimeout(finish, Math.min(250, timeoutMs));
          timer.unref();
          capture.nativeWaiters.add(finish);
        });
      } else if (capture.downloads.length) {
        await new Promise<void>((resolve) => setImmediate(resolve));
      }
      if (!capture.downloads.length) return result;
      const downloads = capture.downloads.map((download) => ({ ...download }));
      if (result && typeof result === 'object' && !Array.isArray(result)) {
        return {
          ...(result as Record<string, unknown>),
          downloads,
        };
      }
      return { value: result, downloads };
    } finally {
      page?.off('download', onPublicDownload);
      for (const finish of capture.nativeWaiters) finish();
      capture.nativeWaiters.clear();
      const captureIndex = owner.genericDownloadCaptures.indexOf(capture);
      if (captureIndex >= 0) owner.genericDownloadCaptures.splice(captureIndex, 1);
    }
  }

  private genericDownloadCaptureForTab(
    owner: BrowserOwner,
    tab: BrowserTab,
  ): GenericDownloadCapture | undefined {
    return [...owner.genericDownloadCaptures].reverse().find((candidate) => {
      const source = owner.tabs.get(candidate.sourceTabId);
      return candidate.sessionHash === tab.sessionHash
        && (
          candidate.sourceTabId === tab.tabId
          || Boolean(source && this.popupDescendsFrom(owner, tab, source))
        );
    });
  }

  private async executeCommand(
    owner: BrowserOwner,
    command: string,
    args: string[],
    params: Record<string, unknown>,
    modalRaceArmed = false,
  ): Promise<unknown> {
    let commandTimeoutMs = resolveCommandTimeoutMs(
      params.command_timeout_ms,
      params.command_deadline_ms,
    );
    const commandDeadlineAt = Date.now() + commandTimeoutMs;
    if (command === 'tab') {
      return this.tabCommand(
        owner,
        args,
        commandDeadlineAt,
        taskDownloadDirectory(params.download_dir),
        transferLimit(params.max_transfer_bytes),
      );
    }
    const requestedTarget = typeof params.target_id === 'string'
      ? params.target_id.trim()
      : '';
    const soleTab = owner.tabs.size === 1 ? owner.tabs.values().next().value : undefined;
    const targetId = requestedTarget || soleTab?.targetId || '';
    if (!targetId) {
      throw new BrowserHostError('非 tab 命令必须指定目标标签页', {
        code: 'invalid_target',
      });
    }
    const tab = this.targetTab(owner, targetId);
    const humanMaintenanceCommand =
      (command === 'console' && args.length === 1 && args[0] === '--clear')
      || (
        command === 'network'
        && args.length === 2
        && args[0] === 'requests'
        && args[1] === '--clear'
      );
    const humanNavigationCommand =
      new Set(['open', 'preview', 'back', 'forward', 'reload']).has(command)
      || (command === 'get' && args.length === 1 && new Set(['url', 'title', 'history']).has(args[0]));
    if (
      tab.mode === 'paused'
      || (tab.mode === 'human' && !humanMaintenanceCommand && !humanNavigationCommand)
    ) {
      throw new BrowserHostError('人工接管或暂停期间禁止浏览器自动化与页面观察', {
        code: 'control_mode_blocked',
      });
    }
    await withDeadline(
      this.ensureDebugger(tab),
      remainingCommandTimeoutMs(commandDeadlineAt),
      () => new BrowserHostError('连接浏览器调试器超过命令截止时间', {
        code: 'command_timeout',
        uncertain: false,
      }),
    );
    commandTimeoutMs = remainingCommandTimeoutMs(commandDeadlineAt);
    this.releaseSettledModalAction(owner, tab.sessionHash);
    const sessionDialogs = this.sessionDialogTabs(owner, tab.sessionHash);
    const sessionChoosers = this.sessionFileChooserTabs(owner, tab.sessionHash);
    const clearsDialog = command === 'dialog';
    const clearsSessionFileChooser = command === 'file_upload'
      || (command === 'upload' && args[0] === '--chooser');
    const clearsTargetFileChooser = command === 'upload_with_trigger';
    const clearsFileChooser = clearsSessionFileChooser || clearsTargetFileChooser;
    if (sessionDialogs.length && !clearsDialog) {
      throw new BrowserHostError('浏览器会话有待处理的 JavaScript 对话框', {
        code: 'dialog_pending',
      });
    }
    if (
      sessionChoosers.length
      && (
        !clearsFileChooser
        // upload_with_trigger can intentionally replace only the chooser on
        // its own page. A chooser in a sibling popup is independent state and
        // must neither be discarded nor hidden from the session coordinator.
        || (
          clearsTargetFileChooser
          && sessionChoosers.some((candidate) => candidate !== tab)
        )
      )
    ) {
      throw new BrowserHostError('浏览器会话有待处理的文件选择器', {
        code: 'file_chooser_pending',
      });
    }
    if (
      owner.pendingModalActions.has(tab.sessionHash)
      && !clearsDialog
      && !clearsFileChooser
    ) {
      throw new BrowserHostError('浏览器会话有尚未收束的 modal 动作', {
        code: sessionDialogs.length ? 'dialog_pending' : 'file_chooser_pending',
      });
    }
    if (
      !modalRaceArmed
      && command !== 'dialog'
      && command !== 'console'
      && command !== 'network'
      && command !== 'network_requests'
      && command !== 'network_request'
    ) {
      return this.withSessionModalRace(
        owner,
        tab,
        () => this.executeCommand(owner, command, args, params, true),
        {
          ...(clearsFileChooser ? { clearsExisting: 'fileChooser' as const } : {}),
          // upload_with_trigger arms and consumes the exact chooser inside one
          // operation. Ignore only the event emitted by this exact target;
          // a sibling popup chooser must still interrupt the command.
          ...(command === 'upload_with_trigger'
            ? {
                ignoreSignal: (signal: SessionModalSignal) => (
                  signal.kind === 'fileChooser' && signal.tab === tab
                ),
              }
            : {}),
        },
      );
    }
    switch (command) {
      case 'open':
        return this.navigate(owner, tab, args[0], commandDeadlineAt);
      case 'preview':
        return this.previewArtifact(owner, tab, args, commandDeadlineAt);
      case 'back': {
        if (args.length) invalidCommandArgs();
        const ctx = await this.actionContext(tab, commandTimeoutMs);
        try {
          await pwActions.goBack(ctx);
        } catch (error) {
          if (!(error instanceof pwActions.ActionError && error.code === 'no_history')) {
            this.clearDocumentState(tab);
          }
          BrowserHost.rethrowAction(error);
        }
        this.clearDocumentState(tab);
        return {};
      }
      case 'forward': {
        if (args.length) invalidCommandArgs();
        const ctx = await this.actionContext(tab, commandTimeoutMs);
        try {
          await pwActions.goForward(ctx);
        } catch (error) {
          if (!(error instanceof pwActions.ActionError && error.code === 'no_history')) {
            this.clearDocumentState(tab);
          }
          BrowserHost.rethrowAction(error);
        }
        this.clearDocumentState(tab);
        return {};
      }
      case 'reload': {
        if (args.length) invalidCommandArgs();
        const ctx = await this.actionContext(tab, commandTimeoutMs);
        this.clearDocumentState(tab);
        try {
          await pwActions.reload(ctx);
        } catch (error) {
          BrowserHost.rethrowAction(error);
        }
        return {};
      }
      case 'snapshot':
        return this.snapshot(tab, !args.includes('--compact'), true, commandTimeoutMs);
      case 'find': {
        if (
          args.length !== 2
          || !new Set(['--text', '--regex']).has(args[0] ?? '')
          || !args[1]
        ) {
          invalidCommandArgs();
        }
        const query: SnapshotFindQuery = args[0] === '--regex'
          ? { regex: args[1] }
          : { text: args[1] };
        try {
          return await this.snapshot(
            tab,
            false,
            true,
            commandTimeoutMs,
            query,
          );
        } catch (error) {
          if (error instanceof SnapshotFindError) {
            throw new BrowserHostError(error.message, {
              code: 'invalid_find_query',
            });
          }
          throw error;
        }
      }
      case 'get':
        return this.getCommand(tab, args, commandTimeoutMs);
      case 'click': {
        const parsed = parseClickArgs(args);
        await pwActions.click(
          await this.actionContext(tab, commandTimeoutMs),
          parsed.ref,
          parsed.options,
        )
          .catch(BrowserHost.rethrowAction);
        return {};
      }
      case 'fill': {
        const parsed = parseFillArgs(args);
        // type+submit 走同一次 RPC：填完立即在同一个 exact Locator 上按 Enter，
        // 中间没有模型往返或第二次选择器解析。
        tab.visualEpoch = null;
        await pwActions.fill(
          await this.actionContext(tab, commandTimeoutMs),
          parsed.ref,
          parsed.value,
          { submit: parsed.submit, slowly: parsed.slowly },
        ).catch(BrowserHost.rethrowAction);
        return {};
      }
      case 'fill_form': {
        if (args.length) invalidCommandArgs();
        const fields = parseFillFormFields(params.fields);
        const result = await pwActions
          .fillForm(await this.actionContext(tab, commandTimeoutMs), fields)
          .catch(BrowserHost.rethrowAction);
        return { completed_count: result.completedCount };
      }
      case 'drag':
        if (args.length !== 2) invalidCommandArgs();
        await pwActions
          .drag(await this.actionContext(tab, commandTimeoutMs), args[0], args[1])
          .catch(BrowserHost.rethrowAction);
        return {};
      case 'mouse': {
        const subcommand = args[0];
        const ctx = await this.actionContext(tab, commandTimeoutMs);
        tab.visualEpoch = null;
        if (subcommand === 'move') {
          if (args.length !== 3) invalidCommandArgs();
          await pwActions.mouseMove(
            ctx,
            strictFiniteNumber(args[1]),
            strictFiniteNumber(args[2]),
          ).catch(BrowserHost.rethrowAction);
          return {};
        }
        if (subcommand === 'down' || subcommand === 'up') {
          if (
            args.length !== 2
            || !new Set(['left', 'right', 'middle']).has(args[1])
          ) {
            invalidCommandArgs();
          }
          const button = args[1] as pwActions.ClickButton;
          if (subcommand === 'down') {
            await pwActions.mouseDown(ctx, button).catch(BrowserHost.rethrowAction);
          } else {
            await pwActions.mouseUp(ctx, button).catch(BrowserHost.rethrowAction);
          }
          return {};
        }
        if (subcommand === 'wheel') {
          if (args.length !== 3) invalidCommandArgs();
          await pwActions.mouseWheel(
            ctx,
            strictFiniteNumber(args[1]),
            strictFiniteNumber(args[2]),
          ).catch(BrowserHost.rethrowAction);
          return {};
        }
        if (subcommand === 'click') {
          if (
            args.length !== 6
            || !new Set(['left', 'right', 'middle']).has(args[3])
          ) {
            invalidCommandArgs();
          }
          const delayMs = strictFiniteNumber(args[5]);
          if (delayMs < 0) invalidCommandArgs();
          await pwActions.mouseClick(
            ctx,
            strictFiniteNumber(args[1]),
            strictFiniteNumber(args[2]),
            {
              button: args[3] as pwActions.ClickButton,
              clickCount: strictUnsignedInteger(
                args[4],
                1,
                Number.MAX_SAFE_INTEGER,
              ),
              delayMs,
            },
          ).catch(BrowserHost.rethrowAction);
          return {};
        }
        if (subcommand === 'drag') {
          if (args.length !== 5) invalidCommandArgs();
          await pwActions.mouseDrag(
            ctx,
            strictFiniteNumber(args[1]),
            strictFiniteNumber(args[2]),
            strictFiniteNumber(args[3]),
            strictFiniteNumber(args[4]),
          ).catch(BrowserHost.rethrowAction);
          return {};
        }
        return invalidCommandArgs();
      }
      case 'resize':
        if (args.length !== 2) invalidCommandArgs();
        tab.visualEpoch = null;
        await pwActions.resize(
          await this.actionContext(tab, commandTimeoutMs),
          strictFiniteNumber(args[0]),
          strictFiniteNumber(args[1]),
        ).catch(BrowserHost.rethrowAction);
        return {};
      case 'drop': {
        const parsed = parseDropArgs(args);
        const files = await this.approvedUploadFiles(owner, parsed.payload.files ?? []);
        tab.visualEpoch = null;
        await pwActions.drop(
          await this.actionContext(tab, commandTimeoutMs),
          parsed.ref,
          { ...parsed.payload, files },
        ).catch(BrowserHost.rethrowAction);
        return {};
      }
      case 'select':
        if (args.length < 1 || !args[0]) invalidCommandArgs();
        await pwActions.selectOption(
          await this.actionContext(tab, commandTimeoutMs),
          args[0],
          args.slice(1),
        )
          .catch(BrowserHost.rethrowAction);
        return {};
      case 'handle_overlay':
        // 注册一次，之后由 Playwright 在每次 actionability 检查前自动触发。
        // 参数是 selector（不是 ref）：处理器要跨越整个操作存活，而 ref 表
        // 每次快照整张替换。
        if (args.length !== 1 || !args[0]) invalidCommandArgs();
        await pwActions.registerOverlayHandler(
          await this.actionContext(tab, commandTimeoutMs),
          args[0],
        ).catch(BrowserHost.rethrowAction);
        return {};
      case 'assert_state':
        // 断言是只读判定：不进 withActionCompletion，不清 visualEpoch，
        // 也不产生后置快照。它唯一的作用是"不成立就停下来"。
        if (args.length !== 2 || !args[0] || !args[1]) invalidCommandArgs();
        await pwActions.assertState(
          await this.actionContext(tab, commandTimeoutMs),
          args[0],
          args[1],
        ).catch(BrowserHost.rethrowAction);
        return {};
      case 'check':
        if (
          args.length !== 2
          || !args[0]
          || (args[1] !== 'true' && args[1] !== 'false')
        ) {
          invalidCommandArgs();
        }
        await pwActions.setChecked(
          await this.actionContext(tab, commandTimeoutMs),
          args[0],
          args[1] === 'true',
        ).catch(BrowserHost.rethrowAction);
        return {};
      case 'hover':
        if (args.length !== 1 || !args[0]) invalidCommandArgs();
        await pwActions.hover(await this.actionContext(tab, commandTimeoutMs), args[0])
          .catch(BrowserHost.rethrowAction);
        return {};
      case 'scroll':
        if (args[0] === '--delta-x') {
          if (args.length !== 4 || args[2] !== '--delta-y') invalidCommandArgs();
          await pwActions.scrollDelta(
            await this.actionContext(tab, commandTimeoutMs),
            Number(args[1]),
            Number(args[3]),
          ).catch(BrowserHost.rethrowAction);
        } else {
          if (args.length !== 2) invalidCommandArgs();
          await pwActions.scroll(
            await this.actionContext(tab, commandTimeoutMs),
            args[0],
            Number(args[1]),
          )
            .catch(BrowserHost.rethrowAction);
        }
        return {};
      case 'press':
        if (args.length < 1 || args.length > 2) invalidCommandArgs();
        await pwActions.press(
          await this.actionContext(tab, commandTimeoutMs),
          args[0],
          args[1] || undefined,
        )
          .catch(BrowserHost.rethrowAction);
        return {};
      case 'keydown':
        if (args.length !== 1) invalidCommandArgs();
        await pwActions.keyDown(await this.actionContext(tab, commandTimeoutMs), args[0])
          .catch(BrowserHost.rethrowAction);
        return {};
      case 'keyup':
        if (args.length !== 1) invalidCommandArgs();
        await pwActions.keyUp(await this.actionContext(tab, commandTimeoutMs), args[0])
          .catch(BrowserHost.rethrowAction);
        return {};
      case 'wait':
        await pwActions.waitFor(
          await this.actionContext(tab, commandTimeoutMs),
          parseWaitArgs(args),
        )
          .catch(BrowserHost.rethrowAction);
        return {};
      case 'upload':
        if (args[0] === '--chooser') {
          return await this.pendingFileUpload(tab, args.slice(1), commandDeadlineAt);
        }
        {
          const files = await this.approvedUploadFiles(owner, args.slice(1));
          await pwActions.upload(
            await this.actionContext(tab, commandTimeoutMs),
            args[0] ?? '',
            files,
          )
            .catch(BrowserHost.rethrowAction);
        }
        return {};
      case 'file_upload':
        return await this.pendingFileUpload(tab, args, commandDeadlineAt);
      case 'upload_with_trigger':
        if (args.length) invalidCommandArgs();
        return await this.uploadWithTrigger(
          tab,
          parseUploadWithTriggerPayload(params),
          commandTimeoutMs,
        ).catch(BrowserHost.rethrowAction);
      case 'vision_screenshot':
        return this.visionScreenshot(tab, args, params);
      case 'screenshot':
        return this.screenshot(tab, args, params, commandTimeoutMs);
      case 'console':
        return await this.consoleCommand(tab, args, commandTimeoutMs);
      case 'network_requests': {
        const options = parseNetworkRequestsArgs(args);
        const page = await owner.engine.pageForView(tab.view, commandTimeoutMs);
        return await pwNetwork.listNetworkRequests(page, options);
      }
      case 'network_request': {
        if (args.length < 1 || args.length > 2) invalidCommandArgs();
        const index = strictUnsignedInteger(
          args[0],
          1,
          Number.MAX_SAFE_INTEGER,
        );
        const part = args[1];
        if (
          part !== undefined
          && !pwNetwork.NETWORK_REQUEST_PARTS.includes(
            part as pwNetwork.NetworkRequestPart,
          )
        ) {
          invalidCommandArgs('network request part 无效');
        }
        const page = await owner.engine.pageForView(tab.view, commandTimeoutMs);
        try {
          return await pwNetwork.networkRequest(
            page,
            index,
            part as pwNetwork.NetworkRequestPart | undefined,
          );
        } catch (error) {
          if (error instanceof pwNetwork.NetworkRequestNotFoundError) {
            throw new BrowserHostError(error.message, {
              code: 'network_request_not_found',
            });
          }
          throw error;
        }
      }
      case 'network':
        return await this.networkCommand(tab, args, commandTimeoutMs);
      case 'dialog':
        return this.dialogCommand(tab, args, commandTimeoutMs);
      case 'eval':
        return this.evaluate(tab, args, commandDeadlineAt);
      case 'run_code_unsafe':
        return this.runCodeUnsafe(tab, args, commandDeadlineAt);
      default:
        throw new BrowserHostError('不支持的浏览器命令', { code: 'unsupported_command' });
    }
  }
  private async tabCommand(
    owner: BrowserOwner,
    args: string[],
    commandDeadlineAt: number,
    downloadDir = '',
    downloadMaxBytes = DEFAULT_MAX_TRANSFER_BYTES,
  ): Promise<Record<string, unknown>> {
    if (args.length === 1 && args[0] === 'list') {
      return {
        tabs: [...owner.tabs.values()].map((tab) => ({
          tabId: tab.tabId,
          label: tab.label,
          title: normalizedText(tab.view.webContents.getTitle()),
          url: publicUrl(tab.view.webContents.getURL() || 'about:blank'),
          type: 'page',
          active: owner.activeTabId === tab.tabId,
          targetId: tab.targetId,
          sessionHash: tab.sessionHash,
          openerTargetId: tab.openerTargetId,
          popupOrdinal: tab.popupOrdinal,
        })),
      };
    }
    if (args[0] === 'new' || args[0] === 'new-user') {
      const userCreated = args[0] === 'new-user';
      const labelIndex = args.indexOf('--label');
      if (labelIndex < 0 || !args[labelIndex + 1]) {
        throw new BrowserHostError('新标签页缺少 Crew label', { code: 'invalid_tab' });
      }
      const label = args[labelIndex + 1];
      const match = LABELED_TAB_RE.exec(label);
      if (!match) throw new BrowserHostError('Crew 标签页 label 无效', { code: 'invalid_tab' });
      if (
        !userCreated
        &&
        [...owner.tabs.values()].some(
          (tab) => tab.sessionHash === match[1] && tab.mode !== 'ai',
        )
      ) {
        throw new BrowserHostError('人工接管或暂停期间禁止为该会话创建标签页', {
          code: 'control_mode_blocked',
        });
      }
      if ([...owner.tabs.values()].some((tab) => tab.label === label)) {
        throw new BrowserHostError('Crew 标签页 label 已存在', { code: 'duplicate_tab' });
      }
      const url = safeUrl(args.at(-1) ?? 'about:blank');
      const tab = this.createTab(owner, label, match[1], '', userCreated ? 'human' : 'ai');
      if (downloadDir) this.setTabDownloadDir(tab, downloadDir);
      tab.downloadMaxBytes = downloadMaxBytes;
      owner.activeTabId = tab.tabId;
      let navigation: Record<string, unknown> = {};
      try {
        await this.initializeNewTab(tab, commandDeadlineAt);
        navigation = await this.withGenericDownloadCapture(
          owner,
          tab,
          remainingCommandTimeoutMs(commandDeadlineAt),
          () => this.navigate(owner, tab, url, commandDeadlineAt),
        ) as Record<string, unknown>;
      } catch (error) {
        this.closeTab(owner, tab);
        throw error;
      }
      return {
        tabId: tab.tabId,
        targetId: tab.targetId,
        label: tab.label,
        ...(Array.isArray(navigation.downloads)
          ? { downloads: navigation.downloads }
          : {}),
      };
    }
    if (
      (args[0] === 'close' || args[0] === 'close-user')
      && (args.length === 1 || args.length === 2)
    ) {
      const tab = args[1]
        ? this.findTab(owner, args[1])
        : owner.tabs.get(owner.activeTabId);
      if (!tab) {
        throw new BrowserHostError('当前没有可关闭的标签页', {
          code: 'no_active_tab',
        });
      }
      if (args[0] === 'close' && tab.mode !== 'ai') {
        throw new BrowserHostError('人工接管或暂停期间禁止自动关闭标签页', {
          code: 'control_mode_blocked',
        });
      }
      this.closeTab(owner, tab);
      return {};
    }
    if (args.length === 1 && args[0]) {
      const tab = this.findTab(owner, args[0]);
      if (tab.mode === 'paused') {
        throw new BrowserHostError('暂停期间禁止切换浏览器标签页', {
          code: 'control_mode_blocked',
        });
      }
      if (downloadDir) this.setTabDownloadDir(tab, downloadDir);
      tab.downloadMaxBytes = downloadMaxBytes;
      owner.activeTabId = tab.tabId;
      return {};
    }
    throw new BrowserHostError('tab 命令无效', { code: 'invalid_tab_command' });
  }

  private createTab(
    owner: BrowserOwner,
    label: string,
    tabSessionHash: string,
    openerTargetId: string,
    initialMode: ControlMode = 'ai',
    inheritedWebPreferences: WebPreferences = {},
    adoptedWebContents?: WebContents,
  ): BrowserTab {
    // 单会话标签页上限。
    //
    // 每个标签页是一个真实的 WebContentsView（独立渲染进程 + 一份 CDP 会话）。
    // 没有上限的话，一个失控的 `window.open` 循环或者一段被注入的脚本就能把
    // 主进程内存耗尽——而这不是"安全摩擦"，是可用性事故：应用整个卡死。
    if (
      [...owner.tabs.values()].filter(
        (candidate) => candidate.sessionHash === tabSessionHash,
      ).length >= MAX_TABS_PER_SESSION
    ) {
      throw new BrowserHostError(`单会话最多允许 ${MAX_TABS_PER_SESSION} 个标签页`, {
        code: 'tab_limit',
      });
    }
    owner.tabCounter += 1;
    const popupKey = `${tabSessionHash}\u0000${openerTargetId}`;
    const popupOrdinal = openerTargetId
      ? (owner.popupOrdinals.get(popupKey) ?? 0) + 1
      : 0;
    if (openerTargetId) owner.popupOrdinals.set(popupKey, popupOrdinal);
    const view = new WebContentsView({
      ...(adoptedWebContents ? { webContents: adoptedWebContents } : {}),
      webPreferences: {
        // Electron's createWindow callback supplies private opener/bootstrap
        // preferences that are required for window.open to complete. Preserve
        // that opaque topology, then enforce Crew's own runtime preferences.
        ...inheritedWebPreferences,
        session: owner.session,
        nodeIntegration: false,
        nodeIntegrationInSubFrames: false,
        nodeIntegrationInWorker: false,
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        webviewTag: false,
        devTools: false,
        plugins: false,
        spellcheck: false,
        navigateOnDragDrop: false,
        backgroundThrottling: false,
      },
    });
    view.setVisible(false);
    view.setBounds({ x: 0, y: 0, ...DEFAULT_VIEWPORT });
    const openerEntry = openerTargetId
      ? this.tabsByTarget.get(openerTargetId)
      : undefined;
    const tab: BrowserTab = {
      // BrowserManager intentionally treats this process-local selector as
      // reusable and keeps the manager's compact tN compatibility shape. The
      // random targetId below remains the immutable ownership identity.
      tabId: `t${owner.tabCounter}`,
      targetId: `target-${randomUUID()}`,
      webContentsId: view.webContents.id,
      label,
      sessionHash: tabSessionHash,
      openerTargetId,
      popupOrdinal,
      view,
      mode: initialMode,
      refs: new Map(),
      dialog: null,
      dialogForwarding: initialMode === 'ai',
      modalRaceDepth: 0,
      console: [],
      network: [],
      downloadDir: openerEntry?.owner === owner
        ? openerEntry.tab.downloadDir
        : '',
      downloadMaxBytes: openerEntry?.owner === owner
        ? openerEntry.tab.downloadMaxBytes
        : DEFAULT_MAX_TRANSFER_BYTES,
      mouseX: DEFAULT_VIEWPORT.width / 2,
      mouseY: DEFAULT_VIEWPORT.height / 2,
      takeoverRequestAt: 0,
      automationDepth: 0,
      debuggerReady: null,
      childSessions: new Map(),
      childSessionParents: new Map(),
      guardContextId: 0,
      guardFrameId: '',
      guardLoaderId: '',
      guardStateKey: '',
      guardStateToken: '',
      navigationEpoch: 0,
      navigationPending: false,
      visualEpoch: null,
      lastFilled: null,
      automationFocus: null,
      automationFocusPending: null,
      crashed: false,
      artifactToken: '',
    };
    owner.tabs.set(tab.tabId, tab);
    this.tabsByTarget.set(tab.targetId, { owner, tab });
    this.tabsByWebContentsId.set(view.webContents.id, { owner, tab });
    this.pageLifecycleOrigins.set(view, {
      owner,
      sessionHash: tabSessionHash,
      mode: initialMode,
      webContentsId: tab.webContentsId,
      downloadDir: tab.downloadDir,
    });
    // 交给 Playwright 引擎：挂到隐藏的自动化宿主窗口上并登记进 transport。
    // 后台可用性依赖三个条件（焦点模拟 / view 可见 / 挂在窗口上），见 automation-host。
    // 先登记期望的焦点模式，再挂载/连接。人类弹窗可能以 human 模式出生，若顺序
    // 反过来，Playwright 首次 prepare 会短暂把它伪装成聚焦页面。
    // 失败必须被吞在这里，不能变成 unhandled rejection。
    //
    // 焦点模拟只影响"后台标签页能不能被自动化"，设不上是可降级的；而一个
    // 逃出去的 promise rejection 在 Node 默认模式下会**终止整个主进程**——
    // 用一次可降级的失败换掉整个应用，是这条 `void` 最坏的一种结局。
    void owner.engine.setAutomationMode(view, initialMode === 'ai').catch((error) => {
      this.emit('browser-error', {
        runtimeKey: owner.runtimeKey,
        targetId: '',
        error: error instanceof Error ? error.message : 'setAutomationMode failed',
      });
    });
    owner.engine.registerTab(view, {
      opener: openerEntry?.owner === owner ? openerEntry.tab.view : undefined,
    });
    this.attachTabEvents(owner, tab);
    const prepareDebugger = async (): Promise<void> => {
      // Electron invokes createWindow before it has adopted the WebContents
      // returned by that callback. A synchronous debugger.attach here blocks
      // Chromium's window.open/middle-click Input.dispatchMouseEvent forever.
      // The transport has the same one-turn barrier; keep BrowserHost's direct
      // debugger listener on the safe side of that adoption boundary as well.
      if (openerTargetId) {
        await new Promise<void>((resolve) => setImmediate(resolve));
      }
      await this.ensureDebugger(tab);
    };
    void prepareDebugger().catch((error: unknown) => {
      this.emit('browser-error', {
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        error: error instanceof Error ? error.message : 'debugger attach failed',
      });
    });
    this.emit('tab-created', this.publicTabEvent(owner, tab));
    return tab;
  }

  // 控制权只由**显式**动作改变（面板按钮 / browser_use 的 takeover），不再从原生
  // 输入推断。原先 AI 模式下任何 keyDown/mouseDown/mouseWheel 都会请求接管，而
  // automationDepth 在 AI 两步之间基本恒为 0——用户只是滚动页面围观就被判成「正在
  // 手动操作」并暂停 AI；叠加模型没有 return 动作、面板只在关闭时才交还，这是一扇
  // 单向门。现在：拦截保留（租约外输入仍会和在途自动化抢页面），推断取消，滚轮放行。
  private attachTabEvents(owner: BrowserOwner, tab: BrowserTab): void {
    const contents = tab.view.webContents;
    contents.on('before-input-event', (event, _input) => {
      // AI 模式下仍然拦截按键：租约外的原生输入会和自动化抢同一个页面。
      // 但**不再**据此推断接管——控制权只由显式动作改变。
      if (tab.mode !== 'human' && tab.automationDepth === 0) {
        event.preventDefault();
      }
    });
    // Electron 43 类型定义未包含 before-mouse-event，但运行时与单测均依赖它。
    const contentsWithMouseEvents = contents as Electron.WebContents & {
      on(
        event: 'before-mouse-event',
        listener: (event: Electron.Event, input: { type?: string }) => void,
      ): Electron.WebContents;
    };
    contentsWithMouseEvents.on('before-mouse-event', (event: Electron.Event, input: { type?: string }) => {
      const type = String(input.type || '');
      if (tab.mode !== 'human' && tab.automationDepth === 0) {
        // 滚轮是**阅读**手势：用户滚动只是想看 AI 在做什么。既不拦截也不影响控制权，
        // 否则「想看一眼」都做不到。点击/按键仍拦截，避免和在途自动化抢页面。
        if (type === 'mouseWheel') {
          // 但必须作废视觉 epoch：坐标点击的 x/y 是从**定格截图**上量的，只在页面
          // 没动过时成立。放行滚轮意味着页面可能在「截图」与「按坐标点」之间被用户
          // 滚走——截图上 y=280 是「取消」，滚 200px 后同一坐标成了「删除」，
          // 而 pageIdentity 不变，dispatch 前的校验发现不了，点击会**静默落错**。
          // 置空后坐标点击抛 invalid_visual_epoch（「请重新截图」），大声失败而非点错。
          // ref 点击不受影响：它按元素身份定位，与滚动位置无关。
          tab.visualEpoch = null;
          return;
        }
        // 只有 mouseDown 算接管手势。滚轮不算：页面白屏/加载失败时用户对着窗口
        // 随手滚一下是常态，把这种无意输入当成「我要接管」会把控制权从正在
        // 干活的 AI 手里抢走。输入仍 preventDefault，页面不会因为滚动产生变化。
        if (type === 'mouseDown') {
          this.requestHumanInteraction(owner, tab, 'pointer');
        }
        event.preventDefault();
      }
    });
    // Let Chromium/Electron navigate to every scheme the embedding application
    // has registered. The Host observes lifecycle events below but does not
    // impose a second URL policy over the browser engine.
    contents.setWindowOpenHandler((details) => {
      // Chromium has already resolved the user's tab-opening gesture for us.
      // A middle click or Ctrl/Meta+click arrives as `background-tab`; every
      // other disposition (`default`, `foreground-tab`, `new-window`, `other`)
      // is a foreground interaction surface. Keep this decision in the
      // synchronous handler closure because Electron's later createWindow
      // callback receives only BrowserWindow options, not the disposition.
      const opensInBackground = details.disposition === 'background-tab';
      return {
        action: 'allow',
        // Crew models every opened browsing context as a browser tab. Closing
        // an opener must therefore not let Electron tear down an independent
        // OAuth/result/child tab before Playwright can bind it.
        outlivesOpener: true,
        // Electron applies these preferences to an ordinary window.open's
        // provisional WebContents. Background-tab gestures may instead ask
        // createWindow to construct the WebContents itself; both paths use the
        // same explicit preferences below.
        overrideBrowserWindowOptions: {
          webPreferences: {
            session: owner.session,
            nodeIntegration: false,
            nodeIntegrationInSubFrames: false,
            nodeIntegrationInWorker: false,
            contextIsolation: true,
            sandbox: true,
            webSecurity: true,
            webviewTag: false,
            devTools: false,
            plugins: false,
            spellcheck: false,
            navigateOnDragDrop: false,
            backgroundThrottling: false,
          },
        },
        createWindow: (options) => {
          const popupContents = (
            options as typeof options & { webContents?: WebContents }
          ).webContents;
          // Electron supplies a provisional WebContents for ordinary
          // window.open/target=_blank, but a real middle-click
          // (`background-tab`) passes `webContents: undefined`. Adopt when
          // present; otherwise create the requested WebContentsView ourselves.
          // Rejecting the latter makes Input.dispatchMouseEvent return while no
          // popup exists, so Playwright waits for `popup` until timeout.
          const popup = this.createTab(
            owner,
            '',
            tab.sessionHash,
            tab.targetId,
            tab.mode,
            options.webPreferences,
            popupContents,
          );
          if (!popupContents) {
            // Chromium does not navigate a WebContents constructed for
            // `background-tab` after createWindow returns (Electron 43 real
            // behavior). Start that exact requested navigation ourselves on
            // the next turn, once Electron has completed the callback.
            setImmediate(() => {
              if (popup.view.webContents.isDestroyed()) return;
              void popup.view.webContents.loadURL(details.url).catch((error: unknown) => {
                this.emit('browser-error', {
                  runtimeKey: owner.runtimeKey,
                  targetId: popup.targetId,
                  code: 'popup_navigation_failed',
                  error: error instanceof Error
                    ? error.message
                    : 'background popup navigation failed',
                });
              });
            });
          }
          if (!opensInBackground) {
            // Foreground target=_blank/window.open/OAuth flows continue on the
            // popup. Background-tab gestures deliberately leave both the
            // active identity and the visible human panel on the opener.
            owner.activeTabId = popup.tabId;
            this.mountHumanPopup(owner, tab, popup);
          }
          return popup.view.webContents;
        },
      };
    });
    contents.on('console-message', (details, ...legacy: unknown[]) => {
      if (tab.mode !== 'ai') return;
      const [level, message, line, sourceId] = legacy;
      const structured = details as unknown as Partial<{
        level: string;
        message: string;
        lineNumber: number;
        sourceId: string;
      }>;
      const record: ConsoleRecord = {
        level: electronConsoleLevel(structured.level ?? level),
        message: publicConsoleText(structured.message ?? message),
        source: publicUrl(
          structured.sourceId ?? (typeof sourceId === 'string' ? sourceId : ''),
        ),
        line: Number(structured.lineNumber ?? line) || 0,
        timestamp: Date.now(),
      };
      // This stream exists only for the browser panel's live diagnostics.
      // Command results must come from Playwright Page.consoleMessages() and
      // Page.pageErrors(), whose navigation filters and error stacks Electron
      // does not provide.
      pushBounded(tab.console, record);
      this.emit('debug', {
        type: 'debug',
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        channel: 'console',
        record: {
          method: 'console-message',
          level: record.level,
          text: record.message,
          source: record.source,
          line: record.line,
          timestamp: record.timestamp,
        },
      });
    });
    contents.on('page-title-updated', () => {
      // A title change is NOT a navigation, so it must not bump navigationEpoch.
      // Same-document navigations that also change the title (e.g. Baidu in-page
      // search) already bump the epoch via did-navigate-in-page below. Bumping
      // here too let any page with a churning title — countdowns, unread badges
      // like "(3) Inbox", media players, or a hostile
      // setInterval(()=>document.title=Math.random()) — reset the snapshot
      // stability gate forever and make itself permanently un-observable to the
      // agent. We still emit tab-updated so the UI reflects the new title.
      this.emit('tab-updated', this.publicTabEvent(owner, tab));
    });
    contents.on('did-start-navigation', (
      details,
      _legacyUrl,
      legacyIsSameDocument,
      legacyIsMainFrame,
    ) => {
      // Electron 43 uses structured fields on the event/details object while
      // older hosts supply positional booleans. Unknown shape is fail-closed:
      // mark it pending, but never let it clear a main-frame transition.
      const isMainFrame = navigationFlag(details, 'isMainFrame', legacyIsMainFrame);
      const isSameDocument = navigationFlag(
        details,
        'isSameDocument',
        legacyIsSameDocument,
      );
      const navigationUrl = details && typeof details === 'object'
        && typeof (details as unknown as Record<string, unknown>).url === 'string'
        ? String((details as unknown as Record<string, unknown>).url)
        : String(_legacyUrl ?? '');
      if (isMainFrame !== false) {
        tab.navigationEpoch += 1;
        tab.navigationPending = true;
      }
      if (isMainFrame === true && isSameDocument === false) {
        // backendNodeId is document-scoped. Across a form navigation retain
        // only a short-lived, value-free semantic proof for the exact
        // searchbox, and only while the destination remains same-origin.
        // Explicit open/back/reload clear this proof before navigation starts.
        const candidate = tab.automationFocus?.continuation ?? tab.automationFocusPending;
        const destinationOrigin = httpOrigin(navigationUrl);
        const continuation = candidate
          && candidate.expiresAt >= Date.now()
          && destinationOrigin === candidate.sourceOrigin
          ? candidate
          : null;
        this.clearDocumentState(tab, continuation);
      }
    });
    contents.on('did-navigate', () => {
      tab.navigationEpoch += 1;
      tab.navigationPending = false;
      this.emit('tab-updated', this.publicTabEvent(owner, tab));
    });
    contents.on('did-navigate-in-page', (details, _legacyUrl, legacyIsMainFrame) => {
      const isMainFrame = navigationFlag(details, 'isMainFrame', legacyIsMainFrame);
      if (isMainFrame === true) {
        tab.navigationEpoch += 1;
        tab.navigationPending = false;
        this.emit('tab-updated', this.publicTabEvent(owner, tab));
      }
    });
    contents.on('did-fail-load', (
      details,
      errorCode,
      errorDescription,
      validatedUrl,
      legacyIsMainFrame,
    ) => {
      const isMainFrame = navigationFlag(details, 'isMainFrame', legacyIsMainFrame);
      if (isMainFrame === true) {
        tab.navigationEpoch += 1;
        // ERR_ABORTED from an older main navigation can race a replacement
        // navigation. Keep pending until did-stop-loading/did-navigate proves
        // the WebContents has converged; otherwise bounded polling rejects it.
        tab.navigationPending = true;
        // ERR_ABORTED(-3) 是新导航打断旧导航的正常信号，不算失败。其余主框架
        // 加载失败会让面板挂着一块可交互白屏——前端需要知道，才能给出错误
        // 遮罩而不是让用户去点一块什么都没加载出来的页面。
        if (Number(errorCode) !== -3) {
          this.emit('tab-load-failed', {
            runtimeKey: owner.runtimeKey,
            label: tab.label,
            url: typeof validatedUrl === 'string' ? validatedUrl : '',
            errorDescription: String(errorDescription || ''),
          });
        }
      }
    });
    contents.on('did-stop-loading', () => {
      tab.navigationEpoch += 1;
      tab.navigationPending = false;
    });
    contents.on('login', (event, _details, authInfo, callback) => {
      // Supply configured proxy credentials when applicable. For ordinary
      // HTTP authentication leave Electron's native challenge untouched so a
      // visible/human-controlled page can complete it.
      this.handleLogin(event, contents, authInfo, callback);
    });
    contents.on('render-process-gone', (_event, details) => {
      tab.navigationEpoch += 1;
      tab.navigationPending = false;
      tab.crashed = true;
      tab.debuggerReady = null;
      this.recoverPanelAfterTabFailure(owner, tab);
      this.emit('tab-crashed', {
        ...this.publicTabEvent(owner, tab),
        reason: details.reason,
      });
    });
    contents.once('destroyed', () => {
      tab.navigationEpoch += 1;
      tab.navigationPending = false;
      this.recoverPanelAfterTabFailure(owner, tab);
      // Renderer-initiated close (window.close/OAuth popup) bypasses closeTab().
      // unregisterTab is idempotent, so the normal close path may safely reach
      // this handler after already unregistering.
      owner.engine.unregisterTab(tab.view);
      this.forgetTab(owner, tab);
    });
    contents.debugger.on('detach', () => {
      tab.debuggerReady = null;
      tab.childSessions.clear();
      tab.childSessionParents.clear();
      tab.guardContextId = 0;
      tab.guardFrameId = '';
      tab.guardLoaderId = '';
      tab.guardStateKey = '';
      tab.guardStateToken = '';
    });
    contents.debugger.on('message', (
      _event,
      method: string,
      params: unknown,
      childSessionId?: string,
    ) => {
      this.handleDebuggerEvent(owner, tab, method, params, childSessionId);
    });
  }

  private requestHumanInteraction(
    owner: BrowserOwner,
    tab: BrowserTab,
    source: 'pointer' | 'keyboard',
  ): void {
    // Remote content remains unable to switch its own control mode. Only a real
    // native input event on the currently mounted trusted panel can ask the
    // renderer to run the authenticated takeover transaction.
    if (this.panel?.owner !== owner || this.panel.tab !== tab) return;
    const now = Date.now();
    if (now - tab.takeoverRequestAt < 750) return;
    tab.takeoverRequestAt = now;
    // 接管请求的来源排查口：白屏误触、真实手势混在一起时，靠这条日志区分
    // 是键盘还是指针、落在哪个标签页上。
    console.info(
      `[browser-host] user interaction requested: source=${source} `
      + `tab=${tab.label} session=${tab.sessionHash} runtime=${owner.runtimeKey}`,
    );
    this.emit('user-interaction-requested', {
      runtimeKey: owner.runtimeKey,
      label: tab.label,
      source,
    });
  }

  private publicTabEvent(owner: BrowserOwner, tab: BrowserTab): Record<string, unknown> {
    return {
      runtimeKey: owner.runtimeKey,
      tabId: tab.tabId,
      targetId: tab.targetId,
      label: tab.label,
      sessionHash: tab.sessionHash,
      openerTargetId: tab.openerTargetId,
      popupOrdinal: tab.popupOrdinal,
      url: publicUrl(tab.view.webContents.getURL() || 'about:blank'),
      title: normalizedText(tab.view.webContents.getTitle()),
    };
  }

  /** Keep the Host's child-session topology aligned with the Playwright page. */
  private async handleChildSessionLifecycle(
    owner: BrowserOwner,
    context: ChildSessionLifecycleContext,
  ): Promise<void> {
    if (context.view.webContents.isDestroyed()) return;
    const found = this.tabsByWebContentsId.get(context.view.webContents.id);
    if (!found || found.owner !== owner || found.tab.view !== context.view) return;
    const tab = found.tab;
    if (context.phase === 'detached') {
      tab.childSessions.delete(context.sessionId);
      tab.childSessionParents.delete(context.sessionId);
      return;
    }

    tab.childSessions.set(context.sessionId, { ...context.targetInfo });
    tab.childSessionParents.set(context.sessionId, context.parentSessionId);
  }

  private handleDebuggerEvent(
    owner: BrowserOwner,
    tab: BrowserTab,
    method: string,
    paramsValue: unknown,
    childSessionId = '',
  ): void {
    const params =
      paramsValue && typeof paramsValue === 'object' && !Array.isArray(paramsValue)
        ? (paramsValue as Record<string, unknown>)
        : {};
    if (method === 'Page.javascriptDialogOpening') {
      tab.dialog = {
        type: normalizedText(params.type),
        message: normalizedText(params.message),
        defaultValue: normalizedText(params.defaultPrompt),
        owner: tab.dialogForwarding ? 'playwright' : 'native',
      };
      this.notifySessionModal(owner, tab, 'dialog');
      this.emit('dialog', {
        ...this.publicTabEvent(owner, tab),
        dialog: {
          type: tab.dialog.type,
          message: tab.dialog.message,
          defaultValue: tab.dialog.defaultValue,
        },
      });
      return;
    }
    if (method === 'Runtime.executionContextsCleared') {
      // Guard contexts belong to the main page session. An OOPIF is allowed to
      // reuse the same numeric context id without invalidating that guard.
      if (!childSessionId) {
        tab.guardContextId = 0;
        tab.guardFrameId = '';
        tab.guardLoaderId = '';
        tab.guardStateKey = '';
        tab.guardStateToken = '';
      }
      return;
    }
    if (method === 'Runtime.executionContextDestroyed') {
      const executionContextId = Number(params.executionContextId);
      if (!childSessionId && executionContextId === tab.guardContextId) {
        tab.guardContextId = 0;
        tab.guardFrameId = '';
        tab.guardLoaderId = '';
        tab.guardStateKey = '';
        tab.guardStateToken = '';
      }
      return;
    }
    if (method === 'Page.javascriptDialogClosed') {
      tab.dialog = null;
      return;
    }
    if (method === 'Network.requestWillBeSent') {
      if (tab.mode !== 'ai') return;
      const request = asOptionalRecord(params.request);
      const record: NetworkRecord = {
        kind: 'request',
        method: normalizedText(request.method, 20),
        url: publicUrl(String(request.url ?? '')),
        timestamp: Date.now(),
      };
      pushBounded(tab.network, record);
      this.emit('debug', {
        type: 'debug',
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        channel: 'network',
        record: {
          method,
          request_method: record.method,
          url: record.url,
          timestamp: record.timestamp,
        },
      });
      return;
    }
    if (method === 'Network.responseReceived') {
      if (tab.mode !== 'ai') return;
      const response = asOptionalRecord(params.response);
      const record: NetworkRecord = {
        kind: 'response',
        url: publicUrl(String(response.url ?? '')),
        status: Number(response.status) || 0,
        timestamp: Date.now(),
      };
      pushBounded(tab.network, record);
      this.emit('debug', {
        type: 'debug',
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        channel: 'network',
        record: {
          method,
          url: record.url,
          status: record.status,
          timestamp: record.timestamp,
        },
      });
      return;
    }
    if (method === 'Network.loadingFailed') {
      if (tab.mode !== 'ai') return;
      const record: NetworkRecord = {
        kind: 'failure',
        url: '',
        error: publicConsoleText(params.errorText),
        timestamp: Date.now(),
      };
      pushBounded(tab.network, record);
      this.emit('debug', {
        type: 'debug',
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        channel: 'network',
        record: {
          method,
          url: '',
          error: record.error,
          timestamp: record.timestamp,
        },
      });
    }
  }

  private async ensureDebugger(tab: BrowserTab): Promise<void> {
    if (tab.view.webContents.isDestroyed() || tab.crashed) {
      throw new BrowserHostError('浏览器标签页已停止', {
        code: 'tab_stopped',
      });
    }
    if (tab.debuggerReady) return tab.debuggerReady;
    tab.debuggerReady = (async () => {
      try {
        // A late tab may already be attached by ElectronCdpTransport. Attached
        // only describes the wire, not whether BrowserHost's required domains
        // are enabled; always run this idempotent enable set once per epoch.
        if (!tab.view.webContents.debugger.isAttached()) {
          tab.view.webContents.debugger.attach('1.3');
        }
        const enableDomain = (
          method: string,
          params?: Record<string, unknown>,
        ): Promise<unknown> => withDeadline(
          tab.view.webContents.debugger.sendCommand(method, params),
          DEBUGGER_SETUP_TIMEOUT_MS,
          () => new Error(`${method} timed out after ${DEBUGGER_SETUP_TIMEOUT_MS}ms`),
        );
        await Promise.all([
          enableDomain('Page.enable'),
          enableDomain('DOM.enable'),
          enableDomain('Accessibility.enable'),
          enableDomain('Network.enable', {
            maxTotalBufferSize: 1_000_000,
            maxResourceBufferSize: 100_000,
          }),
          // Runtime is owned by Playwright's logical page session. Enabling it
          // here, before ElectronCdpTransport has published that session, makes
          // Chromium emit executionContextCreated only to this out-of-band
          // listener. A later idempotent Runtime.enable from Playwright then
          // receives no main-world event, so locator utility worlds keep
          // working while page.evaluate waits forever after a modal dialog.
          // Runtime.evaluate itself does not require Runtime.enable.
          enableDomain('Overlay.enable'),
        ]);
      } catch (error) {
        tab.debuggerReady = null;
        throw new BrowserHostError(
          `无法连接 Electron 浏览器调试器：${error instanceof Error ? error.message : 'unknown'}`,
          { code: 'debugger_unavailable' },
        );
      }
    })();
    return tab.debuggerReady;
  }

  /**
   * A newly constructed Electron WebContents has no renderer/document yet.
   * On real Electron, enabling CDP domains in that state can remain pending
   * until the first document is committed. Materialize a deterministic blank
   * document before awaiting the debugger barrier, then let the caller perform
   * the requested navigation. The caller owns rollback and closes the tab if
   * either bounded phase fails.
   */
  private async initializeNewTab(tab: BrowserTab, commandDeadlineAt: number): Promise<void> {
    const contents = tab.view.webContents;
    if (!contents.getURL()) {
      await withDeadline(
        contents.loadURL('about:blank'),
        Math.min(
          DEBUGGER_SETUP_TIMEOUT_MS,
          remainingCommandTimeoutMs(commandDeadlineAt),
        ),
        () => {
          contents.stop();
          return new BrowserHostError('浏览器初始文档加载超过命令截止时间', {
            code: 'command_timeout',
            uncertain: false,
          });
        },
      );
    }
    await withDeadline(
      this.ensureDebugger(tab),
      Math.min(
        DEBUGGER_SETUP_TIMEOUT_MS,
        remainingCommandTimeoutMs(commandDeadlineAt),
      ),
      () => new BrowserHostError('连接浏览器调试器超过命令截止时间', {
        code: 'command_timeout',
        uncertain: false,
      }),
    );
  }

  private async navigate(
    owner: BrowserOwner,
    tab: BrowserTab,
    value: unknown,
    commandDeadlineAt: number,
  ): Promise<Record<string, unknown>> {
    const url = safeUrl(value);
    // Trusted address-bar/tool navigation may leave an artifact preview. Page
    // navigation itself remains governed by Chromium/Playwright.
    this.revokeArtifact(owner, tab);
    this.clearDocumentState(tab);
    const contents = tab.view.webContents;
    const ctx = await this.actionContext(
      tab,
      remainingCommandTimeoutMs(commandDeadlineAt),
    );
    // Match Playwright MCP's "since loading the page" request index. Arm the
    // public Page request ledger before goto so the document request itself is
    // index 1 and later detail calls cannot drift with page.requests()' rolling
    // retention window.
    pwNetwork.resetNetworkRequests(ctx.page);
    let downloadSeen = false;
    let resolveDownload!: () => void;
    const downloadSignal = new Promise<void>((resolve) => {
      resolveDownload = resolve;
    });
    const downloadListener = (): void => {
      downloadSeen = true;
      resolveDownload();
    };
    ctx.page.on('download', downloadListener);
    try {
      try {
        await ctx.page.goto(url, {
          waitUntil: 'domcontentloaded',
          timeout: remainingCommandTimeoutMs(commandDeadlineAt),
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (/Download is starting/i.test(message)) {
          if (!downloadSeen) {
            const waitMs = Math.min(
              3_000,
              remainingCommandTimeoutMs(commandDeadlineAt),
            );
            let timer: ReturnType<typeof setTimeout> | null = null;
            const capture = this.genericDownloadCaptureForTab(owner, tab);
            let nativeFinish: (() => void) | null = null;
            const signals: Promise<void>[] = [downloadSignal];
            if (capture) {
              signals.push(new Promise<void>((resolve) => {
                if (capture.downloads.length) {
                  resolve();
                  return;
                }
                const finish = (): void => {
                  capture.nativeWaiters.delete(finish);
                  resolve();
                };
                nativeFinish = finish;
                capture.nativeWaiters.add(finish);
              }));
            }
            try {
              await Promise.race([
                ...signals,
                new Promise<never>((_, reject) => {
                  timer = setTimeout(
                    () => reject(error),
                    waitMs,
                  );
                }),
              ]);
            } finally {
              if (timer) clearTimeout(timer);
              if (capture && nativeFinish) {
                capture.nativeWaiters.delete(nativeFinish);
              }
            }
          }
          // Let the native Electron `will-download` listener bind/save the
          // item before the command response starts a fresh observation.
          const settleMs = Math.min(
            500,
            remainingCommandTimeoutMs(commandDeadlineAt),
          );
          if (settleMs > 0) {
            await new Promise<void>((resolve) => setTimeout(resolve, settleMs));
          }
          return {
            url: contents.getURL(),
            download_started: true,
          };
        }
        if (/Timeout [0-9]+ms exceeded/i.test(message)) {
          contents.stop();
          throw new BrowserHostError('页面导航超过命令截止时间', {
            code: 'command_timeout',
            uncertain: true,
          });
        }
        throw error;
      }

      // DOMContentLoaded is the functional boundary. A slow analytics/font
      // tail must not hold an otherwise usable page hostage; observe `load`
      // for at most the upstream five-second grace period.
      const remainingForLoad = Math.floor(commandDeadlineAt - Date.now());
      if (remainingForLoad > 0) {
        await ctx.page.waitForLoadState('load', {
          timeout: Math.min(5_000, remainingForLoad),
        }).catch(() => undefined);
      }
      return { url: ctx.page.url() };
    } catch (error) {
      if (error instanceof BrowserHostError) throw error;
      throw new BrowserHostError(
        `页面导航失败：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'navigation_failed', uncertain: true },
      );
    } finally {
      ctx.page.off('download', downloadListener);
    }
  }

  private activeTab(owner: BrowserOwner): BrowserTab {
    const tab = owner.tabs.get(owner.activeTabId);
    if (!tab) throw new BrowserHostError('当前没有活动浏览器标签页', { code: 'no_active_tab' });
    return tab;
  }

  private findTab(owner: BrowserOwner, identity: string): BrowserTab {
    const matches = [...owner.tabs.values()].filter(
      (tab) => tab.tabId === identity || tab.label === identity || tab.targetId === identity,
    );
    if (matches.length !== 1) {
      throw new BrowserHostError('无法唯一确认浏览器标签页', { code: 'ambiguous_tab' });
    }
    return matches[0];
  }

  private targetTab(owner: BrowserOwner, value: unknown): BrowserTab {
    const target = asString(value, 'target_id', 256);
    const found = this.tabsByTarget.get(target);
    if (!found || found.owner !== owner) {
      throw new BrowserHostError('标签页不属于当前账号', { code: 'foreign_tab' });
    }
    return found.tab;
  }

  private ref(tab: BrowserTab, value: string | undefined): RefState {
    if (!value || !NATIVE_REF_RE.test(value)) {
      throw new BrowserHostError('浏览器元素 ref 无效', { code: 'invalid_ref' });
    }
    const ref = tab.refs.get(value);
    if (!ref) throw new BrowserHostError('浏览器元素 ref 已失效', { code: 'stale_ref' });
    return ref;
  }

  // Electron's Debugger API deliberately returns `any` because CDP schemas are
  // protocol-versioned. Keep that unsoundness inside this one private boundary.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async send(tab: BrowserTab, method: string, params?: Record<string, unknown>): Promise<any> {
    await this.ensureDebugger(tab);
    return tab.view.webContents.debugger.sendCommand(method, params);
  }

  // Flattened OOPIF object/context ids are scoped to their real child session.
  // Omitting this third argument silently sends the command to the main frame.
  private async sendInSession(
    tab: BrowserTab,
    childSessionId: string,
    method: string,
    params?: Record<string, unknown>,
  ): Promise<any> { // eslint-disable-line @typescript-eslint/no-explicit-any
    await this.ensureDebugger(tab);
    return tab.view.webContents.debugger.sendCommand(
      method,
      params,
      childSessionId || undefined,
    );
  }


  private ensureArtifactProtocol(owner: BrowserOwner): void {
    if (owner.artifactProtocolRegistered) return;
    owner.session.protocol.handle(ARTIFACT_SCHEME, async (request) => {
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        return new Response('Method Not Allowed', { status: 405 });
      }
      let parsed: URL;
      try {
        parsed = new URL(request.url);
      } catch {
        return new Response('Bad Request', { status: 400 });
      }
      const token = parsed.hostname.toLocaleLowerCase();
      const grant = owner.artifacts.get(token);
      if (
        !grant
        || grant.expiresAt <= Date.now()
        || parsed.pathname !== '/index.html'
        || parsed.search
      ) {
        if (grant?.expiresAt && grant.expiresAt <= Date.now()) owner.artifacts.delete(token);
        return new Response('Not Found', { status: 404 });
      }
      const headers = {
        'Cache-Control': 'no-store',
        'Content-Type': 'text/html; charset=utf-8',
        'Content-Security-Policy': [
          "default-src 'none'",
          "script-src 'unsafe-inline' 'unsafe-eval' blob:",
          "style-src 'unsafe-inline' data: blob:",
          "connect-src 'none'",
          "img-src data: blob:",
          "font-src data:",
          "media-src data: blob:",
          "frame-src 'none'",
          "worker-src blob:",
          "object-src 'none'",
          "base-uri 'none'",
          "form-action 'none'",
          'sandbox allow-scripts',
        ].join('; '),
        'Referrer-Policy': 'no-referrer',
        'Permissions-Policy': [
          'camera=()',
          'clipboard-read=()',
          'clipboard-write=()',
          'geolocation=()',
          'microphone=()',
          'payment=()',
          'usb=()',
        ].join(', '),
        'X-DNS-Prefetch-Control': 'off',
        'X-Content-Type-Options': 'nosniff',
      };
      return new Response(request.method === 'HEAD' ? null : grant.content, { status: 200, headers });
    });
    owner.artifactProtocolRegistered = true;
  }

  private isAllowedArtifactUrl(owner: BrowserOwner, tab: BrowserTab, value: string): boolean {
    if (!tab.artifactToken) return false;
    try {
      const parsed = new URL(value);
      const token = parsed.hostname.toLocaleLowerCase();
      const grant = owner.artifacts.get(token);
      return parsed.protocol === `${ARTIFACT_SCHEME}:`
        && token === tab.artifactToken
        && parsed.pathname === '/index.html'
        && !parsed.search
        && Boolean(grant && grant.tabId === tab.tabId && grant.expiresAt > Date.now());
    } catch {
      return false;
    }
  }

  private revokeArtifact(owner: BrowserOwner, tab: BrowserTab): void {
    if (tab.artifactToken) owner.artifacts.delete(tab.artifactToken);
    tab.artifactToken = '';
  }

  private async previewArtifact(
    owner: BrowserOwner,
    tab: BrowserTab,
    args: string[],
    commandDeadlineAt: number,
  ): Promise<Record<string, unknown>> {
    if (tab.mode !== 'human') {
      throw new BrowserHostError('本地 HTML 预览只能由用户在人工控制模式打开', {
        code: 'control_mode_blocked',
      });
    }
    const rawFile = args[0] ?? '';
    const rawRoot = args[1] ?? '';
    if (!path.isAbsolute(rawFile) || !path.isAbsolute(rawRoot)) {
      throw new BrowserHostError('本地预览路径必须是绝对路径', { code: 'invalid_artifact' });
    }
    let file: string;
    let root: string;
    try {
      file = realpathSync.native(rawFile);
      root = realpathSync.native(rawRoot);
    } catch {
      throw new BrowserHostError('本地 HTML 文件不存在', { code: 'artifact_missing' });
    }
    if (!ensureWithin(file, root) || !/\.html?$/i.test(file)) {
      throw new BrowserHostError('本地预览仅允许当前工作区内的 HTML 文件', {
        code: 'artifact_outside_workspace',
      });
    }
    let content: Buffer;
    let handle: Awaited<ReturnType<typeof open>> | undefined;
    try {
      const noFollow = process.platform === 'win32' ? 0 : fsConstants.O_NOFOLLOW;
      handle = await open(file, fsConstants.O_RDONLY | noFollow);
      const descriptorInfo = await handle.stat();
      const [currentFile, currentRoot] = [
        realpathSync.native(rawFile),
        realpathSync.native(rawRoot),
      ];
      const [currentFileInfo, rootInfo] = await Promise.all([
        stat(currentFile),
        stat(currentRoot),
      ]);
      if (
        !samePath(file, currentFile)
        || !samePath(root, currentRoot)
        || !ensureWithin(currentFile, currentRoot)
        || !descriptorInfo.isFile()
        || !currentFileInfo.isFile()
        || !rootInfo.isDirectory()
        || !sameFileIdentity(descriptorInfo, currentFileInfo)
      ) {
        throw new BrowserHostError('本地 HTML 文件无效或已变化', {
          code: 'invalid_artifact',
        });
      }

      // 本地 HTML 预览的大小上限。
      //
      // `Buffer.alloc(descriptorInfo.size)` 无上限时，一个几 GB 的 HTML 会让
      // 主进程直接 OOM——而这是用户点一下「在浏览器中打开」就能触发的，
      // 不需要任何恶意，一个导出的大报表就够了。
      if (descriptorInfo.size > MAX_ARTIFACT_BYTES) {
        throw new BrowserHostError(
          `本地 HTML 超过 ${Math.round(MAX_ARTIFACT_BYTES / 1024 / 1024)}MB 预览上限`,
          { code: 'artifact_too_large' },
        );
      }
      // Keep validation and use on one descriptor. Reopening the path here
      // would reintroduce a symlink/ancestor race after the checks above.
      content = Buffer.alloc(descriptorInfo.size);
      let offset = 0;
      while (offset < content.length) {
        const { bytesRead } = await handle.read(
          content,
          offset,
          content.length - offset,
          offset,
        );
        if (bytesRead === 0) break;
        offset += bytesRead;
      }
      const extra = Buffer.alloc(1);
      const [{ bytesRead: extraBytes }, finalInfo] = await Promise.all([
        handle.read(extra, 0, 1, descriptorInfo.size),
        handle.stat(),
      ]);
      if (
        offset !== content.length
        || extraBytes !== 0
        || finalInfo.size !== descriptorInfo.size
        || !sameFileIdentity(finalInfo, descriptorInfo)
      ) {
        throw new BrowserHostError('本地 HTML 文件在读取期间发生变化', {
          code: 'artifact_changed',
        });
      }
    } catch (error) {
      if (error instanceof BrowserHostError) throw error;
      throw new BrowserHostError('本地 HTML 文件无法安全读取', {
        code: 'invalid_artifact',
      });
    } finally {
      await handle?.close().catch(() => undefined);
    }
    this.ensureArtifactProtocol(owner);
    this.revokeArtifact(owner, tab);
    const token = randomUUID().replaceAll('-', '');
    const contentBytes = new Uint8Array(content);
    const contentBuffer = contentBytes.buffer.slice(
      contentBytes.byteOffset,
      contentBytes.byteOffset + contentBytes.byteLength,
    ) as ArrayBuffer;
    owner.artifacts.set(token, {
      tabId: tab.tabId,
      content: contentBuffer,
      expiresAt: Date.now() + 24 * 60 * 60 * 1000,
    });
    tab.artifactToken = token;
    const url = artifactUrl(token);
    this.clearDocumentState(tab);
    try {
      const contents = tab.view.webContents;
      await withDeadline(
        contents.loadURL(url),
        remainingCommandTimeoutMs(commandDeadlineAt),
        () => {
          contents.stop();
          return new BrowserHostError('本地 HTML 预览超过命令截止时间', {
            code: 'command_timeout',
            uncertain: true,
          });
        },
      );
    } catch (error) {
      this.revokeArtifact(owner, tab);
      if (error instanceof BrowserHostError) throw error;
      throw new BrowserHostError(
        `本地 HTML 预览失败：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'artifact_load_failed', uncertain: true },
      );
    }
    return { url };
  }

  private async history(tab: BrowserTab, direction: 'back' | 'forward'): Promise<Record<string, unknown>> {
    const history = tab.view.webContents.navigationHistory;
    if (direction === 'back') {
      if (!history.canGoBack()) throw new BrowserHostError('当前页面无法后退', { code: 'no_history' });
      // Explicit history navigation must clear focus provenance before
      // Electron can synchronously emit did-start-navigation.
      this.clearDocumentState(tab);
      history.goBack();
    } else {
      if (!history.canGoForward()) throw new BrowserHostError('当前页面无法前进', { code: 'no_history' });
      this.clearDocumentState(tab);
      history.goForward();
    }
    return {};
  }

  private clearDocumentState(
    tab: BrowserTab,
    continuation: AutomationFocusContinuation | null = null,
  ): void {
    tab.refs.clear();
    tab.visualEpoch = null;
    tab.lastFilled = null;
    tab.automationFocus = null;
    tab.automationFocusPending = continuation;
    tab.guardContextId = 0;
    tab.guardFrameId = '';
    tab.guardLoaderId = '';
    tab.guardStateKey = '';
    tab.guardStateToken = '';
  }

  private async currentPageIdentity(tab: BrowserTab): Promise<string> {
    const frameTree = await this.send(tab, 'Page.getFrameTree');
    const frame = asOptionalRecord(asOptionalRecord(frameTree.frameTree).frame);
    const frameId = asString(frame.id, 'frame id', 256);
    const loaderId = typeof frame.loaderId === 'string' ? frame.loaderId : '';
    // frameId + loaderId identify the document for screenshot/focus continuity.
    // A same-document history or query-string update preserves that identity;
    // cross-document navigation changes loaderId. If a non-conforming CDP
    // implementation omits loaderId, retain the URL as the compatibility
    // discriminator instead of treating every document as identical.
    const documentIdentity = loaderId
      ? `${frameId}\0${loaderId}`
      : `${frameId}\0url:${tab.view.webContents.getURL() || 'about:blank'}`;
    return createHash('sha256')
      .update(documentIdentity, 'utf8')
      .digest('hex');
  }

  /**
   * 采集一次页面快照。
   *
   * 全部交给 Playwright 的 `ariaSnapshot({mode:'ai'})`：它保留层级、天然包含 iframe
   * 内容、shadow DOM 里的控件也在，且每个元素的 ref 持有元素本身。原来那套
   * `Accessibility.getFullAXTree` 拍平成行 + `backendNodeId` 登记 + 全量 DOM 索引
   * 查属性的做法整体退役。
   *
   * `register` 为 false 时不动 tab.refs，供只读的内部观察避免冲掉 AI 正在用的 ref 表。
   */
  private async snapshot(
    tab: BrowserTab,
    full: boolean,
    register = true,
    timeoutMs = SNAPSHOT_TIMEOUT_MS,
    findQuery?: SnapshotFindQuery,
  ): Promise<Record<string, unknown>> {
    const owner = this.ownerOfTab(tab);
    const page = await owner.engine.pageForView(tab.view, timeoutMs);
    const bounds = tab.view.getBounds();
    const options = {
      full,
      viewport: { width: bounds.width, height: bounds.height },
      hash: (value: string) => createHash('sha256').update(value).digest('hex').slice(0, 32),
      timeoutMs,
    };
    const snap = findQuery
      ? await captureSnapshotForFind(page, options, findQuery)
      : await captureSnapshot(page, options);

    if (register) {
      // 新快照使上一份的所有 ref 失效（Playwright 的注入脚本只保留最近一份），
      // 所以必须整体替换而不是合并。
      tab.refs = snap.refs;
    }

    const history = tab.view.webContents.navigationHistory;
    return {
      snapshot: snap.text,
      url: publicUrl(snap.url || 'about:blank'),
      // Default ref-only snapshots deliberately skip page.title() Runtime work.
      // Electron already owns this metadata without entering the renderer.
      title: publicConsoleText(tab.view.webContents.getTitle() || snap.title),
      can_go_back: history.canGoBack(),
      can_go_forward: history.canGoForward(),
      // 非空表示这份快照被截断了，值是触发的上限。
      truncated: snap.truncated,
      // 提交类控件的显式标记（`<button type=submit>` 在 form 内、
      // `<input type=submit|image>`）。
      //
      // 这一位由**宿主**计算，绝不能让上层去解析渲染文本里的 `[action=submit]`
      // ——行格式一变，判定就静默失效，而这一位正是只读技能"不许点提交"那条
      // 约束的唯一依据。只在有提交控件时才带上，普通页面不增加载荷。
      ...(Object.keys(snap.refActions).length
        ? { ref_actions: snap.refActions }
        : {}),
    };
  }

  /** 反查 tab 所属的 owner。快照/动作要拿到该 owner 的 Playwright 引擎。 */
  private ownerOfTab(tab: BrowserTab): BrowserOwner {
    const found = this.tabsByTarget.get(tab.targetId);
    if (!found) throw new BrowserHostError('标签页不属于任何账号', { code: 'foreign_tab' });
    return found.owner;
  }

  private setTabDownloadDir(tab: BrowserTab, downloadDir: string): void {
    tab.downloadDir = downloadDir;
    this.pageLifecycleOrigins.set(tab.view, {
      owner: this.ownerOfTab(tab),
      sessionHash: tab.sessionHash,
      mode: tab.mode,
      webContentsId: tab.webContentsId,
      downloadDir,
    });
  }

  private sessionTabs(owner: BrowserOwner, sessionHash: string): BrowserTab[] {
    return [...owner.tabs.values()].filter((candidate) => (
      candidate.sessionHash === sessionHash
      && !candidate.crashed
      && !candidate.view.webContents.isDestroyed()
    ));
  }

  private sessionDialogTabs(owner: BrowserOwner, sessionHash: string): BrowserTab[] {
    return this.sessionTabs(owner, sessionHash).filter((candidate) => candidate.dialog);
  }

  private sessionFileChooserTabs(owner: BrowserOwner, sessionHash: string): BrowserTab[] {
    return this.sessionTabs(owner, sessionHash).filter(
      (candidate) => owner.engine.hasPendingFileChooser(candidate.view),
    );
  }

  private notifySessionModal(
    owner: BrowserOwner,
    tab: BrowserTab,
    kind: ModalKind,
  ): void {
    const waiters = owner.modalWaiters.get(tab.sessionHash);
    if (!waiters?.size) return;
    const signal: SessionModalSignal = { kind, tab };
    for (const waiter of [...waiters]) waiter(signal);
  }

  private armSessionModalWaiter(
    owner: BrowserOwner,
    sessionHash: string,
    ignoreSignal?: ModalKind | ((signal: SessionModalSignal) => boolean),
  ): {
    promise: Promise<SessionModalSignal>;
    dispose: () => void;
  } {
    let active = true;
    let listener!: (signal: SessionModalSignal) => void;
    const promise = new Promise<SessionModalSignal>((resolve) => {
      listener = (signal) => {
        if (!active) return;
        if (
          typeof ignoreSignal === 'function'
            ? ignoreSignal(signal)
            : signal.kind === ignoreSignal
        ) return;
        active = false;
        const current = owner.modalWaiters.get(sessionHash);
        current?.delete(listener);
        if (current && !current.size) owner.modalWaiters.delete(sessionHash);
        resolve(signal);
      };
      const waiters = owner.modalWaiters.get(sessionHash) ?? new Set();
      waiters.add(listener);
      owner.modalWaiters.set(sessionHash, waiters);
    });
    return {
      promise,
      dispose: () => {
        if (!active) return;
        active = false;
        const current = owner.modalWaiters.get(sessionHash);
        current?.delete(listener);
        if (current && !current.size) owner.modalWaiters.delete(sessionHash);
      },
    };
  }

  private retainPendingModalAction(
    owner: BrowserOwner,
    tab: BrowserTab,
    promise: Promise<void>,
  ): PendingModalAction {
    const state: PendingModalAction = {
      triggerTargetId: tab.targetId,
      promise,
      settled: false,
      error: undefined,
    };
    owner.pendingModalActions.set(tab.sessionHash, state);
    // Attach both branches immediately to avoid an unhandled rejection while
    // the user is reading/handling the surfaced modal. Keep the outcome in the
    // state until the modal-clearing command explicitly consumes it.
    void promise.then(
      () => {
        state.settled = true;
      },
      (error: unknown) => {
        state.error = error;
        state.settled = true;
      },
    );
    return state;
  }

  private modalActionFailure(error: unknown): BrowserHostError {
    if (error instanceof BrowserHostError) {
      return new BrowserHostError(
        `modal 关闭后原动作失败：${error.message}`,
        {
          code: 'modal_action_failed',
          uncertain: error.uncertain,
          phase: error.phase,
          partial: error.partial,
          completedCount: error.completed_count,
          browserStopped: error.browser_stopped,
          stopUnconfirmed: error.stop_unconfirmed,
        },
      );
    }
    if (error instanceof pwActions.ActionError) {
      return new BrowserHostError(
        `modal 关闭后原动作失败：${error.message}`,
        {
          code: 'modal_action_failed',
          uncertain: error.uncertain,
          phase: error.phase,
          partial: error.partial,
          completedCount: error.completedCount,
        },
      );
    }
    return new BrowserHostError(
      `modal 关闭后原动作失败：${error instanceof Error ? error.message : 'unknown'}`,
      {
        code: 'modal_action_failed',
        uncertain: true,
        partial: true,
      },
    );
  }

  private releaseSettledModalAction(owner: BrowserOwner, sessionHash: string): void {
    const state = owner.pendingModalActions.get(sessionHash);
    if (!state?.settled) return;
    if (
      this.sessionDialogTabs(owner, sessionHash).length
      || this.sessionFileChooserTabs(owner, sessionHash).length
    ) {
      return;
    }
    owner.pendingModalActions.delete(sessionHash);
    if (state.error !== undefined) throw this.modalActionFailure(state.error);
  }

  /**
   * Race an entire Host command—not just a Locator call—against any modal in
   * the logical session. This covers navigation, coordinate input, completion
   * settling and dialogs opened by a newly-created popup.
   */
  private async withSessionModalRace<T>(
    owner: BrowserOwner,
    tab: BrowserTab,
    operation: () => Promise<T>,
    options: {
      clearsExisting?: ModalKind;
      /** Ignore only a modal that the operation itself owns and consumes. */
      ignoreSignal?: ModalKind | ((signal: SessionModalSignal) => boolean);
    } = {},
  ): Promise<T> {
    const sessionHash = tab.sessionHash;
    const existingDialogs = this.sessionDialogTabs(owner, sessionHash);
    const existingChoosers = this.sessionFileChooserTabs(owner, sessionHash);
    if (existingDialogs.length && options.clearsExisting !== 'dialog') {
      throw new BrowserHostError('浏览器会话有待处理的 JavaScript 对话框', {
        code: 'dialog_pending',
      });
    }
    if (existingChoosers.length && options.clearsExisting !== 'fileChooser') {
      throw new BrowserHostError('浏览器会话有待处理的文件选择器', {
        code: 'file_chooser_pending',
      });
    }

    const retained = owner.pendingModalActions.get(sessionHash);
    const waiter = this.armSessionModalWaiter(owner, sessionHash, options.ignoreSignal);
    tab.modalRaceDepth += 1;
    // The waiter is already armed, so dispatch immediately. Besides preserving
    // native event order this lets lifecycle commands
    // flip their synchronous acceptance gates before the caller can enqueue a
    // tail event in the next microtask.
    let operationPromise: Promise<T>;
    try {
      operationPromise = Promise.resolve(operation());
    } catch (error) {
      operationPromise = Promise.reject(error);
    }
    operationPromise = operationPromise.finally(() => {
      tab.modalRaceDepth = Math.max(0, tab.modalRaceDepth - 1);
    });
    const fullPromise = retained
      ? operationPromise.then(async (result) => {
          await retained.promise;
          return result;
        })
      : operationPromise;
    const outcome = await Promise.race([
      fullPromise.then(
        (value) => ({ kind: 'complete' as const, value }),
        (error: unknown) => ({ kind: 'error' as const, error }),
      ),
      waiter.promise.then((signal) => ({ kind: 'modal' as const, signal })),
    ]);
    if (outcome.kind === 'modal') {
      waiter.dispose();
      const continuation = fullPromise.then(() => undefined);
      this.retainPendingModalAction(owner, tab, continuation);
      throw new BrowserHostError(
        outcome.signal.kind === 'dialog'
          ? '动作已触发 JavaScript 对话框；请先处理对话框'
          : '动作已触发文件选择器；请先上传文件或取消',
        {
          code: outcome.signal.kind === 'dialog'
            ? 'dialog_pending'
            : 'file_chooser_pending',
          phase: 'dispatching',
        },
      );
    }
    waiter.dispose();
    if (
      retained
      && owner.pendingModalActions.get(sessionHash) === retained
      && this.sessionDialogTabs(owner, sessionHash).length === 0
      && this.sessionFileChooserTabs(owner, sessionHash).length === 0
    ) {
      owner.pendingModalActions.delete(sessionHash);
    }
    if (outcome.kind === 'error') throw outcome.error;
    return outcome.value;
  }

  /** 组装动作层需要的上下文。 */
  private async actionContext(
    tab: BrowserTab,
    timeoutMs = ACTION_TIMEOUT_MS,
  ): Promise<ActionContext> {
    const owner = this.ownerOfTab(tab);
    const deadlineAt = Date.now() + timeoutMs;
    const page = await owner.engine.pageForView(tab.view, timeoutMs);
    const remainingTimeoutMs = Math.max(1, Math.floor(deadlineAt - Date.now()));
    return {
      page,
      refs: tab.refs,
      hash: (value: string) => createHash('sha256').update(value).digest('hex').slice(0, 32),
      timeoutMs: remainingTimeoutMs,
      deadlineAt,
      // Host owns the complete command race (including non-Locator mutations,
      // popup modals and completion settling). The action-level race remains
      // available to direct users of playwright-actions and its parity contracts.
      raceDialogs: (
        tab.modalRaceDepth === 0
        && !owner.modalWaiters.has(tab.sessionHash)
      ),
      onModalActionPending: (pending) => {
        this.retainPendingModalAction(owner, tab, pending);
      },
    };
  }

  /** Resolve upload inputs only from this account's identity-checked staging root. */
  private async approvedUploadFiles(
    owner: BrowserOwner,
    files: string[],
  ): Promise<string[]> {
    if (
      !Array.isArray(files)
      || files.some((file) => typeof file !== 'string' || !path.isAbsolute(file))
    ) {
      throw new BrowserHostError('上传文件列表无效', { code: 'invalid_upload' });
    }
    if (!files.length) return [];

    const rawRoot = path.join(path.dirname(owner.profilePath), 'approved-uploads');
    try {
      const root = realpathSync.native(rawRoot);
      if (!samePath(root, path.resolve(rawRoot))) throw new Error('linked upload root');
      const validateEntry = async (entry: string): Promise<string> => {
        const resolved = realpathSync.native(entry);
        const info = await lstat(entry);
        if (
          info.isSymbolicLink()
          || !samePath(resolved, path.resolve(entry))
          || !ensureWithin(resolved, root)
          || (!info.isFile() && !info.isDirectory())
        ) {
          throw new Error('invalid upload entry');
        }
        if (info.isDirectory()) {
          const children = await readdir(entry);
          await Promise.all(children.map((child) => validateEntry(path.join(entry, child))));
        }
        return resolved;
      };
      return await Promise.all(files.map(async (file) => {
        return validateEntry(file);
      }));
    } catch {
      throw new BrowserHostError('上传文件不属于账号审批暂存目录', {
        code: 'invalid_upload_path',
      });
    }
  }

  /**
   * Complete a triggered file upload without a click/file_upload RPC gap.
   *
   * A pending chooser is only a one-slot temporal mirror in PlaywrightEngine;
   * consuming it after a separate click can accidentally apply files to an old
   * chooser. This operation instead clears the mirror, arms an exact page
   * listener immediately before the trigger mutation, and completes only the
   * chooser captured in this call. A strict input-selector fallback is allowed
   * only when the trigger provably did not dispatch or no chooser appeared
   * during the bounded post-click grace period.
   */
  private async uploadWithTrigger(
    tab: BrowserTab,
    payload: UploadWithTriggerPayload,
    timeoutMs = ACTION_TIMEOUT_MS,
  ): Promise<Record<string, unknown>> {
    const ctx = await this.actionContext(tab, timeoutMs);
    const owner = this.ownerOfTab(tab);
    const files = await this.approvedUploadFiles(owner, payload.files);
    const engine = owner.engine;
    const directUpload = async (): Promise<Record<string, unknown>> => {
      const ref = `@upload-input-${randomUUID()}`;
      try {
        await pwActions.resolveUniqueSelector(
          ctx,
          ref,
          payload.inputSelector,
          ctx.hash,
        );
        await pwActions.upload(ctx, ref, files);
        return {
          via: 'input',
          uploaded: files.length,
        };
      } finally {
        ctx.refs.delete(ref);
      }
    };
    const preDispatchFailure = (error: unknown): boolean => (
      error instanceof pwActions.ActionError
      && error.phase === 'pre_dispatch'
      && !error.uncertain
      && !error.partial
    );
    const afterTrigger = async <T>(operation: () => Promise<T>): Promise<T> => {
      try {
        return await operation();
      } catch (error) {
        if (!(error instanceof pwActions.ActionError)) throw error;
        throw new pwActions.ActionError(
          `文件触发器已执行，但后续上传未完成：${error.message}`,
          error.code,
          {
            phase: error.uncertain ? error.phase : 'partial',
            uncertain: error.uncertain,
            partial: true,
            completedCount: 1,
          },
        );
      }
    };

    // Drop any chooser intercepted by an earlier, unrelated interaction before
    // arming this call's event listener.
    engine.takePendingFileChooser(tab.view);
    try {
      // Clearing a file input never needs to open a picker. Avoid an otherwise
      // pointless trigger click and preserve Playwright's [] clear primitive.
      if (!payload.triggerSelector || files.length === 0) {
        return await directUpload();
      }

      const triggerRef = `@upload-trigger-${randomUUID()}`;
      try {
        await pwActions.resolveUniqueSelector(
          ctx,
          triggerRef,
          payload.triggerSelector,
          ctx.hash,
        );
      } catch (error) {
        ctx.refs.delete(triggerRef);
        if (preDispatchFailure(error)) return await directUpload();
        throw error;
      }

      const capture = createFileChooserCapture(ctx.page);
      try {
        try {
          await pwActions.clickArmed(ctx, triggerRef, capture.arm);
        } catch (error) {
          capture.dispose();
          // Playwright's action log can prove that strict resolution or
          // actionability failed before native dispatch. Only that class may
          // fall back; input_uncertain must leave the page untouched.
          if (preDispatchFailure(error)) return await directUpload();
          throw error;
        }

        const eventChooser = await capture.wait(FILE_CHOOSER_GRACE_MS);
        // Engine's listener mirrors the same event. Drain it even when our
        // exact listener already captured the chooser; at the timeout boundary
        // it also closes the tiny event/timer race.
        const mirroredCount = engine.pendingFileChooserCount(tab.view);
        const mirroredChooser = engine.takePendingFileChooser(tab.view);
        if (
          mirroredCount > 1
          ||
          eventChooser
          && mirroredChooser
          && eventChooser !== mirroredChooser
        ) {
          throw new pwActions.ActionError(
            '一次上传触发了多个文件选择器，无法确定应完成哪一个',
            'file_chooser_race',
            {
              phase: 'dispatching',
              uncertain: true,
              partial: true,
              completedCount: 1,
            },
          );
        }
        const chooser = eventChooser ?? mirroredChooser;
        if (!chooser) return await afterTrigger(directUpload);

        const multiple = chooser.isMultiple();
        await afterTrigger(
          () => pwActions.uploadFileChooser(ctx, chooser, files),
        );
        return {
          via: 'chooser',
          uploaded: files.length,
          multiple,
        };
      } finally {
        capture.dispose();
        ctx.refs.delete(triggerRef);
      }
    } finally {
      // Never let a chooser observed during a failed/timeout path poison the
      // next upload RPC.
      engine.takePendingFileChooser(tab.view);
    }
  }

  /**
   * Complete the exact FileChooser opened by the immediately preceding page interaction.
   *
   * Syntax:
   *   file_upload <path>...       (or legacy-compatible upload --chooser <path>...)
   *   file_upload --cancel        (an empty argument list is also cancellation)
   *
   * This is intentionally separate from direct `upload @ref paths...`: many production sites
   * hide the input behind a button and only expose its exact target through Playwright's
   * browser-native FileChooser event.
   */
  private async pendingFileUpload(
    tab: BrowserTab,
    args: string[],
    commandDeadlineAt: number,
  ): Promise<Record<string, unknown>> {
    const cancel = args.length === 0 || (args.length === 1 && args[0] === '--cancel');
    if (!cancel && args.includes('--cancel')) invalidCommandArgs();
    const owner = this.ownerOfTab(tab);
    const files = cancel ? undefined : await this.approvedUploadFiles(owner, args);
    const chooserTabs = this.sessionFileChooserTabs(owner, tab.sessionHash);
    const selected = owner.engine.hasPendingFileChooser(tab.view)
      ? tab
      : chooserTabs.length === 1
        ? chooserTabs[0]
        : undefined;
    if (!selected && chooserTabs.length > 1) {
      throw new BrowserHostError(
        '浏览器会话同时存在多个文件选择器，请先选择对应标签页',
        { code: 'ambiguous_file_chooser' },
      );
    }
    const ctx = await this.actionContext(
      selected ?? tab,
      remainingCommandTimeoutMs(commandDeadlineAt),
    );
    const chooserCount = selected
      ? owner.engine.pendingFileChooserCount(selected.view)
      : 0;
    const chooser = selected
      ? owner.engine.takePendingFileChooser(selected.view)
      : null;
    if (chooserCount > 1) {
      throw new BrowserHostError(
        '一次动作触发了多个文件选择器，无法确定应完成哪一个',
        {
          code: 'file_chooser_race',
          uncertain: true,
          partial: true,
        },
      );
    }
    if (!chooser) {
      throw new BrowserHostError('当前页面没有待处理的文件选择器', {
        code: 'no_file_chooser',
      });
    }
    const multiple = chooser.isMultiple();
    await pwActions.uploadFileChooser(ctx, chooser, files)
      .catch(BrowserHost.rethrowAction);
    return {
      canceled: cancel,
      uploaded: cancel ? 0 : args.length,
      multiple,
    };
  }

  /** 把动作层的错误码原样带到 BrowserHostError，Python 按码分类。 */
  private static rethrowAction(error: unknown): never {
    if (error instanceof pwActions.ActionError) {
      throw new BrowserHostError(error.message, {
        code: error.code,
        uncertain: error.uncertain,
        phase: error.phase,
        partial: error.partial,
        completedCount: error.completedCount,
      });
    }
    throw error;
  }

  private async getCommand(
    tab: BrowserTab,
    args: string[],
    timeoutMs = ACTION_TIMEOUT_MS,
  ): Promise<Record<string, unknown>> {
    const kind = args[0] ?? '';
    if (kind === 'url') return { url: publicUrl(tab.view.webContents.getURL() || 'about:blank') };
    if (kind === 'title') return { title: publicConsoleText(tab.view.webContents.getTitle()) };
    if (kind === 'history') {
      const history = tab.view.webContents.navigationHistory;
      return {
        can_go_back: history.canGoBack(),
        can_go_forward: history.canGoForward(),
      };
    }
    if (kind === 'cdp-url') {
      throw new BrowserHostError('Electron 内置浏览器不暴露 CDP 地址', {
        code: 'cdp_not_exposed',
      });
    }
    if (kind === 'box') {
      const record = this.ref(tab, args[1]);
      const page = await this.ownerOfTab(tab).engine.pageForView(tab.view, timeoutMs);
      const locator = record.playwrightRef
        ? locatorFromRef(page, record.playwrightRef)
        : page.locator(record.selector);
      const box = await locator
        .boundingBox({ timeout: timeoutMs });
      if (!box) throw new BrowserHostError('元素当前不可见，无法取包围盒', { code: 'stale_ref' });
      return { box };
    }
    if (kind === 'text') {
      const text = await pwActions.textOf(await this.actionContext(tab, timeoutMs), args[1] ?? '')
        .catch(BrowserHost.rethrowAction);
      return { text };
    }
    if (kind === 'attr') {
      const attribute = args[2] ?? '';
      if (!attribute) throw new BrowserHostError('属性名不能为空', { code: 'invalid_attribute' });
      const attributeValue = await pwActions
        .attributeOf(await this.actionContext(tab, timeoutMs), args[1] ?? '', attribute)
        .catch(BrowserHost.rethrowAction);
      return { attribute: attributeValue };
    }
    throw new BrowserHostError('不支持的 get 命令', { code: 'unsupported_get' });
  }

  /**
   * 记录「这次焦点是自动化造成的」。
   *
   * 截图/导出前要把 Crew 自己留下的焦点释放掉（否则截图里带着我们制造的光标与高亮），
   * 但绝不能释放用户或页面自己创建的焦点——所以必须有出处凭据。
   *
   * 动作面改走 Playwright 之后没有现成的 backendNodeId 了，所以填写完成后直接问页面
   * 「现在谁有焦点」，再把它解析成 backendNodeId。三次往返，只发生在 fill 上。
   */
  private async recordAutomationFocus(tab: BrowserTab, role: string, name: string): Promise<void> {
    try {
      const evaluated = await this.send(tab, 'Runtime.evaluate', {
        expression: 'document.activeElement',
        returnByValue: false,
      });
      const objectId = String(evaluated?.result?.objectId ?? '');
      if (!objectId) return;
      let backendNodeId = 0;
      let node: Record<string, unknown> = {};
      try {
        const described = await this.send(tab, 'DOM.describeNode', { objectId });
        node = asOptionalRecord(described?.node);
        backendNodeId = Number(node.backendNodeId ?? 0);
      } finally {
        await this.send(tab, 'Runtime.releaseObject', { objectId }).catch(() => undefined);
      }
      if (!backendNodeId) return;
      const sourceOrigin = httpOrigin(tab.view.webContents.getURL() || 'about:blank');
      const normalizedRole = normalizedText(role, 100).toLocaleLowerCase();
      const normalizedName = normalizedText(name, 500).toLocaleLowerCase();
      tab.automationFocus = {
        backendNodeId,
        pageIdentity: await this.currentPageIdentity(tab),
        // 同源表单跳转后，精确节点凭据会降级成这份有界的连续性凭据。
        continuation: sourceOrigin && normalizedRole && normalizedName
          ? {
            sourceOrigin,
            role: normalizedRole,
            name: normalizedName,
            domFingerprint: this.focusDomFingerprint(node),
            expiresAt: Date.now() + AUTOMATION_FOCUS_CONTINUATION_MS,
          }
          : null,
      };
      tab.automationFocusPending = null;
    } catch {
      // 拿不到出处就不记——宁可少释放一次焦点，也不能凭猜测去动用户的焦点。
      tab.automationFocus = null;
    }
  }

  /**
   * 焦点连续性指纹。截图前要判断「现在获得焦点的还是不是刚才那个元素」，
   * 只剩这一处使用。
   */
  private focusDomFingerprint(node: Record<string, unknown>): string {
    const attributes = this.domAttributes(node);
    const fields = [String(node.nodeName ?? '').toLocaleUpperCase()];
    for (const name of ['type', 'name', 'id', 'aria-label', 'placeholder']) {
      fields.push(`${name}\0${normalizedText(attributes.get(name) ?? '', 1_000)}`);
    }
    return createHash('sha256').update(fields.join('\0'), 'utf8').digest('hex');
  }

  private domAttributes(node: Record<string, unknown>): Map<string, string> {
    const values = Array.isArray(node.attributes) ? node.attributes.map(String) : [];
    const attributes = new Map<string, string>();
    let total = 0;
    for (let index = 0; index + 1 < values.length && attributes.size < 128; index += 2) {
      const name = values[index].toLocaleLowerCase();
      const value = values[index + 1];
      total += name.length + value.length;
      if (total > 65_536) break;
      attributes.set(name, value);
    }
    return attributes;
  }

  private async dispatchInput(
    tab: BrowserTab,
    method: string,
    params: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<void> {
    tab.automationDepth += 1;
    try {
      const operation = this.send(tab, method, params);
      if (timeoutMs === undefined) {
        await operation;
      } else {
        await withDeadline(
          operation,
          timeoutMs,
          () => new BrowserHostError('浏览器输入派发超过命令截止时间', {
            code: 'command_timeout',
            uncertain: true,
            phase: 'dispatching',
          }),
        );
      }
    } finally {
      tab.automationDepth = Math.max(0, tab.automationDepth - 1);
    }
  }

  private async captureScreenshotPng(tab: BrowserTab): Promise<{
    image: Buffer;
    width: number;
    height: number;
    hash: string;
  }> {
    const captured = await this.send(tab, 'Page.captureScreenshot', {
      format: 'png',
      fromSurface: true,
      captureBeyondViewport: false,
    });
    const data = String(captured?.data ?? '');
    if (
      !data
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)
    ) {
      throw new BrowserHostError('浏览器截图数据无效', {
        code: 'invalid_screenshot',
      });
    }
    const image = Buffer.from(data, 'base64');
    const pngSignature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const width = image.length >= 24 ? image.readUInt32BE(16) : 0;
    const height = image.length >= 24 ? image.readUInt32BE(20) : 0;
    if (
      !image.subarray(0, 8).equals(pngSignature)
      || width <= 0
      || height <= 0
    ) {
      throw new BrowserHostError('浏览器截图不是有效 PNG', {
        code: 'invalid_screenshot',
      });
    }
    return {
      image,
      width,
      height,
      hash: createHash('sha256').update(image).digest('hex'),
    };
  }

  private async releaseAutomationFocusForScreenshot(tab: BrowserTab): Promise<boolean> {
    const tracked = tab.automationFocus;
    const pending = tab.automationFocusPending;
    if (!tracked && !pending) return false;
    // Cross-document continuation is one-shot regardless of whether the
    // current page still proves it. A later screenshot must never get another
    // chance to reinterpret unrelated focus.
    tab.automationFocusPending = null;
    const pageIdentity = await this.currentPageIdentity(tab);
    let backendNodeId = 0;
    const focusedEditable = (node: AxNode | undefined): boolean => {
      const role = normalizedText(cdpValue(node?.role), 100).toLocaleLowerCase();
      const editable = node ? axProperty(node, 'editable') : undefined;
      const editableToken = typeof editable === 'string' ? editable.toLocaleLowerCase() : '';
      return Boolean(
        node
        && Number(node.backendDOMNodeId) > 0
        && axProperty(node, 'focused') === true
        && EDITABLE_AX_ROLES.has(role)
        && axProperty(node, 'disabled') !== true
        && axProperty(node, 'readonly') !== true
        && (editable === true || editableToken === 'plaintext' || editableToken === 'richtext'),
      );
    };

    const matchesContinuation = async (
      node: AxNode,
      continuation: AutomationFocusContinuation,
    ): Promise<boolean> => {
      const role = normalizedText(cdpValue(node.role), 100).toLocaleLowerCase();
      const name = normalizedText(cdpValue(node.name), 500).toLocaleLowerCase();
      if (!name || role !== continuation.role || name !== continuation.name) return false;
      try {
        const described = await this.send(tab, 'DOM.describeNode', {
          backendNodeId: Number(node.backendDOMNodeId),
          depth: 0,
          pierce: true,
        });
        const domNode = asOptionalRecord(described?.node);
        return Object.keys(domNode).length > 0
          && this.focusDomFingerprint(domNode) === continuation.domFingerprint;
      } catch {
        return false;
      }
    };

    if (tracked?.pageIdentity === pageIdentity) {
      const axResult = (await this.send(tab, 'Accessibility.getPartialAXTree', {
        backendNodeId: tracked.backendNodeId,
        fetchRelatives: false,
      })) as { nodes?: AxNode[] };
      const current = axResult.nodes?.find(
        (node) => Number(node.backendDOMNodeId) === tracked.backendNodeId,
      );
      if (
        focusedEditable(current)
        && (!tracked.continuation || await matchesContinuation(current!, tracked.continuation))
      ) {
        backendNodeId = tracked.backendNodeId;
      }
    } else if (tracked) {
      tab.automationFocus = null;
    }

    // A same-origin form navigation may recreate/autofocus the searchbox. It
    // is eligible only within the short TTL, when exactly one focused editable
    // exists and its value-free semantic/DOM proof matches the original field.
    if (
      !backendNodeId
      && !tab.automationFocus
      && pending
      && pending.expiresAt >= Date.now()
      && httpOrigin(tab.view.webContents.getURL() || '') === pending.sourceOrigin
    ) {
      const axResult = (await this.send(tab, 'Accessibility.getFullAXTree', { depth: 32 })) as {
        nodes?: AxNode[];
      };
      const focused = (axResult.nodes ?? []).filter(focusedEditable);
      if (focused.length === 1 && await matchesContinuation(focused[0]!, pending)) {
        backendNodeId = Number(focused[0]!.backendDOMNodeId) || 0;
      }
    }
    if (!backendNodeId) {
      tab.automationFocus = null;
      return false;
    }
    if (await this.currentPageIdentity(tab) !== pageIdentity) {
      tab.automationFocus = null;
      return false;
    }
    tab.automationFocus = null;

    // Resolve in Crew's isolated world when available. The fixed function can
    // only blur this exact node and cannot read its value or arbitrary page
    // content. Unlike sending Escape, it cannot dismiss an unrelated modal.
    try {
      const resolved = await this.send(tab, 'DOM.resolveNode', {
        backendNodeId,
        ...(tab.guardContextId > 0 ? { executionContextId: tab.guardContextId } : {}),
      });
      const objectId = String(resolved?.object?.objectId ?? '');
      if (!objectId) return false;
      try {
        const result = await this.send(tab, 'Runtime.callFunctionOn', {
          objectId,
          functionDeclaration: `function(){
            const owner=this?.ownerDocument;
            if(!owner||owner.activeElement!==this)return false;
            const prototype=owner.defaultView?.HTMLElement?.prototype;
            const blur=prototype?.blur;
            if(typeof blur!=='function')return false;
            blur.call(this);
            return owner.activeElement!==this;
          }`,
          returnByValue: true,
          awaitPromise: false,
          userGesture: false,
        });
        const released = result?.result?.value === true;
        return released;
      } finally {
        await this.send(tab, 'Runtime.releaseObject', { objectId }).catch(() => undefined);
      }
    } catch {
      // Presentation settling is best effort. A stale/detached tracked node
      // must never prevent the user from receiving an otherwise valid image.
      tab.automationFocus = null;
      return false;
    }
  }

  /**
   * Capture the exact viewport used by model vision and coordinate clicks.
   *
   * This intentionally stays on the fixed CDP PNG contract: the returned
   * dimensions/hash are bound to ``visualEpoch`` and must not inherit any
   * user-export options such as full-page, JPEG or CSS scaling.
   */
  private async visionScreenshot(
    tab: BrowserTab,
    args: string[],
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    if (args.length !== 1 || !args[0]) invalidCommandArgs();
    const output = canonicalPath(path.resolve(asString(args[0], 'screenshot path', 4096)));
    const profile = profilePath(params.profile_dir);
    if (!ensureWithin(output, path.dirname(profile))) {
      throw new BrowserHostError('截图目标不属于账号浏览器目录', { code: 'invalid_artifact_path' });
    }
    const pageIdentity = await this.currentPageIdentity(tab);
    const captured = await this.captureScreenshotPng(tab);
    if (await this.currentPageIdentity(tab) !== pageIdentity) {
      tab.visualEpoch = null;
      throw new BrowserHostError('页面在截图期间已变化，请重新观察', {
        code: 'page_changed',
      });
    }
    const token = randomUUID().replaceAll('-', '');
    tab.visualEpoch = {
      token,
      pageIdentity,
      screenshotHash: captured.hash,
      width: captured.width,
      height: captured.height,
    };
    await writeFile(output, captured.image, { mode: 0o600 });
    await chmod(output, 0o600);
    return {
      path: output,
      width: captured.width,
      height: captured.height,
      host_epoch: token,
      settled: false,
      focus_released: false,
    };
  }

  /**
   * Export a user-facing screenshot through Playwright's public Page/Locator
   * APIs. This deliberately does not create a visual epoch: full-page,
   * element-only and CSS-scaled images are not coordinate-click viewports.
   */
  private async screenshot(
    tab: BrowserTab,
    args: string[],
    params: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<Record<string, unknown>> {
    const parsed = parseScreenshotArgs(args);
    const output = canonicalPath(path.resolve(
      asString(parsed.output, 'screenshot path', 4096),
    ));
    const profile = profilePath(params.profile_dir);
    if (!ensureWithin(output, path.dirname(profile))) {
      throw new BrowserHostError('截图目标不属于账号浏览器目录', {
        code: 'invalid_artifact_path',
      });
    }
    const beforeSettleIdentity = await this.currentPageIdentity(tab);
    const beforeSettleUrl = tab.view.webContents.getURL() || 'about:blank';
    let focusReleased = false;
    if (parsed.settled) {
      await this.send(tab, 'Overlay.hideHighlight').catch(() => undefined);
      focusReleased = await this.releaseAutomationFocusForScreenshot(tab);
      if (focusReleased) {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      }
      if (
        await this.currentPageIdentity(tab) !== beforeSettleIdentity
        || (tab.view.webContents.getURL() || 'about:blank') !== beforeSettleUrl
      ) {
        this.clearDocumentState(tab);
        throw new BrowserHostError('页面在收束截图焦点时发生跳转，请重新观察', {
          code: 'page_changed',
        });
      }
    }

    const owner = this.ownerOfTab(tab);
    const page = await owner.engine.pageForView(tab.view, timeoutMs);
    const pageIdentity = await this.currentPageIdentity(tab);
    const commonOptions = {
      type: parsed.type,
      scale: parsed.scale,
      timeout: timeoutMs,
      ...(parsed.type === 'jpeg' ? { quality: 90 } : {}),
    } as const;
    const image = parsed.ref
      ? await (() => {
          const record = this.ref(tab, parsed.ref);
          const locator = record.playwrightRef
            ? locatorFromRef(page, record.playwrightRef)
            : page.locator(record.selector);
          return locator.screenshot(commonOptions);
        })()
      : await page.screenshot({
          ...commonOptions,
          fullPage: parsed.fullPage,
        });
    if (await this.currentPageIdentity(tab) !== pageIdentity) {
      throw new BrowserHostError('页面在截图期间已变化，请重新观察', {
        code: 'page_changed',
      });
    }
    const validImage = parsed.type === 'png'
      ? image.length >= 8
        && image.subarray(0, 8).equals(
          Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
        )
      : image.length >= 4
        && image[0] === 0xff
        && image[1] === 0xd8
        && image[image.length - 2] === 0xff
        && image[image.length - 1] === 0xd9;
    if (!validImage) {
      throw new BrowserHostError('Playwright 返回了无效的截图数据', {
        code: 'invalid_screenshot',
      });
    }
    await writeFile(output, image, { mode: 0o600 });
    await chmod(output, 0o600);
    return {
      path: output,
      type: parsed.type,
      bytes: image.length,
      settled: parsed.settled,
      focus_released: focusReleased,
    };
  }

  private async consoleCommand(
    tab: BrowserTab,
    args: string[],
    timeoutMs: number,
  ): Promise<Record<string, unknown>> {
    const options = parseConsoleArgs(args);
    const page = await this.ownerOfTab(tab).engine.pageForView(tab.view, timeoutMs);
    if (options.clear) {
      await pwConsole.clearConsoleMessages(page);
      // Keep the UI-only cache consistent with the explicit functional clear.
      tab.console.splice(0);
      return { text: '' };
    }
    return {
      ...await pwConsole.readConsoleMessages(page, {
        level: options.level,
        all: options.all,
      }),
    };
  }

  private async networkCommand(
    tab: BrowserTab,
    args: string[],
    timeoutMs: number,
  ): Promise<Record<string, unknown>> {
    if (
      args.length < 1
      || args.length > 2
      || args[0] !== 'requests'
      || (args.length === 2 && args[1] !== '--clear')
    ) {
      throw new BrowserHostError('network 仅支持 requests', { code: 'unsupported_command' });
    }
    if (args[1] === '--clear') {
      const page = await this.ownerOfTab(tab).engine.pageForView(tab.view, timeoutMs);
      await pwNetwork.resetNetworkRequests(page);
      tab.network.splice(0);
    }
    return { text: JSON.stringify(tab.network) };
  }

  private async dialogCommand(
    tab: BrowserTab,
    args: string[],
    timeoutMs = ACTION_TIMEOUT_MS,
  ): Promise<Record<string, unknown>> {
    const owner = this.ownerOfTab(tab);
    const dialogTabs = this.sessionDialogTabs(owner, tab.sessionHash);
    const selected = tab.dialog
      ? tab
      : dialogTabs.length === 1
        ? dialogTabs[0]
        : undefined;
    const action = args[0] ?? '';
    if (action === 'status') {
      if (dialogTabs.length > 1 && !tab.dialog) {
        return {
          hasDialog: true,
          ambiguous: true,
          dialogs: dialogTabs.map((candidate) => ({
            targetId: candidate.targetId,
            label: candidate.label,
            type: candidate.dialog?.type ?? '',
            message: candidate.dialog?.message ?? '',
            defaultValue: candidate.dialog?.defaultValue ?? '',
          })),
        };
      }
      return selected?.dialog
        ? {
            hasDialog: true,
            type: selected.dialog.type,
            message: selected.dialog.message,
            defaultValue: selected.dialog.defaultValue,
          }
        : { hasDialog: false };
    }
    if (action !== 'accept' && action !== 'dismiss') {
      throw new BrowserHostError('dialog 动作无效', { code: 'invalid_dialog' });
    }
    if (!selected?.dialog) {
      throw new BrowserHostError(
        dialogTabs.length > 1
          ? '浏览器会话同时存在多个对话框，请先选择对应标签页'
          : '页面当前没有对话框',
        { code: dialogTabs.length > 1 ? 'ambiguous_dialog' : 'no_dialog' },
      );
    }
    selected.visualEpoch = null;
    const promptText = action === 'accept' && args.length >= 2
      ? args[1]
      : undefined;
    const dialogOwner = selected.dialog.owner;
    const retained = owner.pendingModalActions.get(tab.sessionHash);
    // Arm before closing. alert('one'); confirm('two') can emit the second
    // opening synchronously from inside the first Dialog.accept().
    const waiter = this.armSessionModalWaiter(owner, tab.sessionHash);
    let closeFinished = false;
    const close = (async () => {
      if (dialogOwner === 'playwright') {
        await owner.engine.handleDialog(selected.view, {
          accept: action === 'accept',
          ...(promptText !== undefined ? { promptText } : {}),
          timeoutMs,
        });
      } else {
        // Human-mode openings are deliberately filtered from Playwright, so a
        // direct CDP close cannot leave core's DialogManager stale.
        await this.send(selected, 'Page.handleJavaScriptDialog', {
          accept: action === 'accept',
          ...(promptText !== undefined ? { promptText } : {}),
        });
      }
      closeFinished = true;
      if (retained) await retained.promise;
      else await new Promise<void>((resolve) => setTimeout(resolve, MODAL_SETTLE_MS));
    })();
    const outcome = await Promise.race([
      close.then(
        () => ({ kind: 'complete' as const }),
        (error: unknown) => ({ kind: 'error' as const, error }),
      ),
      waiter.promise.then((signal) => ({ kind: 'modal' as const, signal })),
    ]);
    waiter.dispose();
    if (outcome.kind === 'modal') {
      const next = outcome.signal.tab.dialog;
      return {
        hasDialog: outcome.signal.kind === 'dialog' && Boolean(next),
        modalPending: true,
        modalType: outcome.signal.kind,
        targetId: outcome.signal.tab.targetId,
        label: outcome.signal.tab.label,
        ...(next
          ? {
              type: next.type,
              message: next.message,
              defaultValue: next.defaultValue,
            }
          : {}),
      };
    }
    if (retained && owner.pendingModalActions.get(tab.sessionHash) === retained) {
      owner.pendingModalActions.delete(tab.sessionHash);
    }
    if (outcome.kind === 'error') {
      if (closeFinished && retained) throw this.modalActionFailure(outcome.error);
      throw new BrowserHostError(
        `无法处理 JavaScript 对话框：${
          outcome.error instanceof Error ? outcome.error.message : 'unknown'
        }`,
        { code: 'dialog_failed', uncertain: true },
      );
    }
    const nextDialogs = this.sessionDialogTabs(owner, tab.sessionHash);
    const next = nextDialogs.length === 1 ? nextDialogs[0] : undefined;
    const nextDialog = next?.dialog;
    if (next && nextDialog) {
      return {
        hasDialog: true,
        modalPending: true,
        modalType: 'dialog',
        targetId: next.targetId,
        label: next.label,
        type: nextDialog.type,
        message: nextDialog.message,
        defaultValue: nextDialog.defaultValue,
      };
    }
    return { hasDialog: false };
  }

  /**
   * Playwright-MCP-compatible page/element JavaScript evaluation.
   *
   * `function` may be a function expression or an arbitrary expression. When
   * a ref is supplied the resolved strict Locator is passed as `element`.
   * Evaluation is intentionally treated as a mutation boundary: arbitrary
   * page code can change DOM, history, storage or focus even when its return
   * value looks read-only, so all previously issued refs are invalidated.
   */
  private async evaluate(
    tab: BrowserTab,
    args: string[],
    commandDeadlineAt: number,
  ): Promise<Record<string, unknown>> {
    if (args.length < 1 || args.length > 2 || !args[0]) invalidCommandArgs();
    const expression = args[0];
    const nativeRef = args[1] ?? '';
    const ctx = await this.actionContext(
      tab,
      remainingCommandTimeoutMs(commandDeadlineAt),
    );
    tab.visualEpoch = null;
    try {
      const evaluated = await pwActions.withActionCompletion(ctx, async () => (
        nativeRef
          ? await (() => {
              const record = this.ref(tab, nativeRef);
              const locator = record.playwrightRef
                ? locatorFromRef(ctx.page, record.playwrightRef)
                : ctx.page.locator(record.selector);
              return locator.evaluate(
                async (element, pageExpression) => {
                  const value = globalThis.eval(`(${pageExpression})`);
                  const isFunction = typeof value === 'function';
                  const result = await (isFunction ? value(element) : value);
                  return { result, isFunction, isUndefined: result === undefined };
                },
                expression,
                { timeout: remainingCommandTimeoutMs(commandDeadlineAt) },
              );
            })()
          : await withDeadline(
              ctx.page.evaluate(async (pageExpression) => {
                const value = globalThis.eval(`(${pageExpression})`);
                const isFunction = typeof value === 'function';
                const result = await (isFunction ? value() : value);
                return { result, isFunction, isUndefined: result === undefined };
              }, expression),
              remainingCommandTimeoutMs(commandDeadlineAt),
              () => new BrowserHostError('page.evaluate 超过命令截止时间', {
                code: 'command_timeout',
                uncertain: true,
                phase: 'dispatching',
              }),
            )
      ));
      const result = asOptionalRecord(evaluated);
      const serialized = result.isUndefined === true
        ? 'undefined'
        : JSON.stringify(result.result, null, 2) ?? 'undefined';
      return {
        value: result.result,
        is_function: result.isFunction === true,
        is_undefined: result.isUndefined === true,
        serialized,
      };
    } catch (error) {
      BrowserHost.rethrowAction(error);
    } finally {
      this.clearDocumentState(tab);
    }
  }

  /**
   * Execute the official Playwright server-side escape hatch.
   *
   * The source must be an async/sync function accepting the public Playwright
   * Page object. Python resolves `filename` against the task workdir and sends
   * the exact UTF-8 source as argv[0]; argv[1] is retained only as a VM stack
   * filename. Arbitrary code can mutate page state before throwing, therefore
   * refs are invalidated on every terminal path.
   */
  private async runCodeUnsafe(
    tab: BrowserTab,
    args: string[],
    commandDeadlineAt: number,
  ): Promise<Record<string, unknown>> {
    if (args.length < 1 || args.length > 2) invalidCommandArgs();
    const code = args[0];
    const filename = args[1] || undefined;
    const ctx = await this.actionContext(
      tab,
      remainingCommandTimeoutMs(commandDeadlineAt),
    );
    tab.visualEpoch = null;
    try {
      const owner = this.ownerOfTab(tab);
      const result = await owner.engine.withPageLifecycleSource(
        tab.view,
        commandDeadlineAt,
        async () => await executeUnsafePlaywrightCode(ctx.page, code, {
          deadlineAt: commandDeadlineAt,
          ...(filename ? { filename } : {}),
          withCompletion: async (action) => (
            await pwActions.withActionCompletion(ctx, action)
          ),
          onTimeout: async () => {
            await this.recoverTimedOutRunCode(tab, ctx.page);
          },
        }),
      );
      return {
        has_result: typeof result === 'string',
        ...(typeof result === 'string' ? { result } : {}),
      };
    } catch (error) {
      if (error instanceof RunCodeTimeoutError) {
        throw new BrowserHostError(error.message, {
          code: 'command_timeout',
          uncertain: true,
          phase: 'dispatching',
          partial: true,
        });
      }
      BrowserHost.rethrowAction(error);
    } finally {
      this.clearDocumentState(tab);
    }
  }

  /**
   * Replace the document before exposing a run-code timeout.
   *
   * Revoking the VM façade prevents calls that have not dispatched yet. A
   * browser-side `evaluate()` timer or Locator actionability loop is already
   * running in Chromium and JavaScript promises have no public cancellation
   * API. Navigating the same Page destroys that execution context and cancels
   * those operations deterministically; a bounded about:blank fallback keeps
   * the target usable even when the original URL cannot be reloaded.
   */
  private async recoverTimedOutRunCode(
    tab: BrowserTab,
    page: Page,
  ): Promise<void> {
    this.clearDocumentState(tab);
    tab.visualEpoch = null;
    const recoveryTimeoutMs = 5_000;
    const currentUrl = page.url() || tab.view.webContents.getURL() || 'about:blank';
    try {
      if (page.isClosed()) {
        throw new Error('timed-out snippet closed its Page');
      }
      await page.goto(currentUrl, {
        waitUntil: 'domcontentloaded',
        timeout: recoveryTimeoutMs,
      });
    } catch {
      const contents = tab.view.webContents;
      if (contents.isDestroyed()) return;
      contents.stop();
      await withDeadline(
        contents.loadURL('about:blank'),
        recoveryTimeoutMs,
        () => {
          contents.stop();
          return new BrowserHostError('超时代码的页面恢复未能完成', {
            code: 'command_timeout',
            uncertain: true,
            partial: true,
          });
        },
      ).catch(() => undefined);
    }
    if (!tab.view.webContents.isDestroyed()) {
      await this.ownerOfTab(tab).engine.pageForView(
        tab.view,
        recoveryTimeoutMs,
      ).catch(() => undefined);
    }
  }

  private async pageGuard(
    key: string,
    params: Record<string, unknown>,
    modalRaceArmed = false,
  ): Promise<string> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    await this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim());
    const tab = this.targetTab(owner, params.target_id);
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止读取页面守卫状态', {
        code: 'control_mode_blocked',
      });
    }
    if (!modalRaceArmed) {
      this.releaseSettledModalAction(owner, tab.sessionHash);
      return this.withSessionModalRace(
        owner,
        tab,
        () => this.pageGuard(key, params, true),
      );
    }
    await this.ensureDebugger(tab);
    const commandTimeoutMs = resolveCommandTimeoutMs(params.command_timeout_ms);
    const stateKey = asString(params.state_key, 'state_key', 100);
    const stateToken = asString(params.state_token, 'state_token', 100);
    if (!GUARD_KEY_RE.test(stateKey) || !TOKEN_RE.test(stateToken)) {
      throw new BrowserHostError('页面守卫标识无效', { code: 'invalid_guard' });
    }
    const frameTree = await this.send(tab, 'Page.getFrameTree');
    const frame = asOptionalRecord(asOptionalRecord(frameTree.frameTree).frame);
    const frameId = asString(frame.id, 'frame id', 256);
    const loaderId = typeof frame.loaderId === 'string' ? frame.loaderId : '';
    const reset = asBoolean(params.reset);
    if (reset) {
      tab.guardStateKey = stateKey;
      tab.guardStateToken = stateToken;
      tab.guardFrameId = frameId;
      tab.guardLoaderId = loaderId;
    }
    const hostToken = (
      tab.guardStateKey === stateKey
      && tab.guardStateToken === stateToken
      && tab.guardFrameId === frameId
      && tab.guardLoaderId === loaderId
    ) ? stateToken : '';
    const readMarker = async (): Promise<Record<string, unknown>> => {
      try {
        const value = await withDeadline(
          tab.view.webContents.mainFrame.executeJavaScript(
            '(()=>({href:location.href,timeOrigin:performance.timeOrigin,'
            + 'scrollX:window.scrollX,scrollY:window.scrollY,width:window.innerWidth,'
            + 'height:window.innerHeight,dpr:window.devicePixelRatio}))()',
            false,
          ),
          commandTimeoutMs,
          () => new Error('page guard main-world read timed out'),
        );
        return { token: hostToken, counter: 0, ...asOptionalRecord(value) };
      } catch (error) {
        throw new BrowserHostError(
          `无法读取页面状态：${error instanceof Error ? error.message : 'unknown'}`,
          { code: 'guard_unavailable' },
        );
      }
    };
    // Electron 43 can wedge an OOPIF Runtime channel after creating a custom
    // isolated world. The guard is now host-owned; this fixed read-only
    // document-world probe installs no globals and no MutationObserver.
    const marker = await readMarker();
    const href = typeof marker.href === 'string' ? marker.href : '';
    const hostHref = tab.view.webContents.getURL() || 'about:blank';
    return JSON.stringify({
      ...marker,
      targetId: tab.targetId,
      frameId,
      loaderId,
      navigationEpoch: tab.navigationEpoch,
      navigationPending: tab.navigationPending,
      titleDigest: createHash('sha256')
        .update(tab.view.webContents.getTitle(), 'utf8')
        .digest('hex'),
      // The page-world URL and Electron's main-frame URL should agree at an
      // observation boundary. A mismatch is transitional and must never be
      // published as a fresh snapshot generation.
      locationConsistent: Boolean(href) && href === hostHref,
    });
  }

  private async pageImages(
    key: string,
    params: Record<string, unknown>,
    modalRaceArmed = false,
  ): Promise<Record<string, string>[]> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    await this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim());
    const tab = this.targetTab(owner, params.target_id);
    const commandTimeoutMs = resolveCommandTimeoutMs(params.command_timeout_ms);
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止读取页面图片', {
        code: 'control_mode_blocked',
      });
    }
    if (!modalRaceArmed) {
      this.releaseSettledModalAction(owner, tab.sessionHash);
      return this.withSessionModalRace(
        owner,
        tab,
        () => this.pageImages(key, params, true),
      );
    }
    const page = await owner.engine.pageForView(tab.view, commandTimeoutMs);
    // Playwright owns the frame/OOPIF routing. Query each real frame instead
    // of creating a one-off isolated world in only the top document.
    const frameResults = await Promise.allSettled(
      page.frames().map((frame) =>
        withDeadline(
          frame.locator('img').evaluateAll(
            (images: Element[]) => images.map((node) => {
              const image = node as HTMLImageElement;
              return {
                src: String(image.currentSrc || image.src || ''),
                alt: String(image.alt || ''),
                width: String(image.naturalWidth || image.width || 0),
                height: String(image.naturalHeight || image.height || 0),
              };
            }),
          ),
          commandTimeoutMs,
          () => new Error('frame image enumeration timed out'),
        ),
      ),
    );
    const rows = frameResults.flatMap((result) => (
      result.status === 'fulfilled' && Array.isArray(result.value)
        ? result.value
        : []
    ));
    return rows.map((row: unknown) => {
      const item = asOptionalRecord(row);
      return {
        src: String(item.src ?? ''),
        alt: String(item.alt ?? ''),
        width: String(item.width ?? ''),
        height: String(item.height ?? ''),
      };
    });
  }

  private async coordinateClick(
    key: string,
    params: Record<string, unknown>,
    modalRaceArmed = false,
  ): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    const commandTimeoutMs = resolveCommandTimeoutMs(
      params.command_timeout_ms,
      params.command_deadline_ms,
    );
    const commandDeadlineAt = Date.now() + commandTimeoutMs;
    const tab = this.targetTab(owner, params.target_id);
    const requestedDownloadDir = taskDownloadDirectory(params.download_dir);
    if (requestedDownloadDir) {
      this.setTabDownloadDir(tab, requestedDownloadDir);
    }
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止坐标点击', {
        code: 'control_mode_blocked',
      });
    }
    if (!modalRaceArmed) {
      this.releaseSettledModalAction(owner, tab.sessionHash);
      if (this.sessionDialogTabs(owner, tab.sessionHash).length) {
        throw new BrowserHostError('浏览器会话有待处理的 JavaScript 对话框', {
          code: 'dialog_pending',
        });
      }
      if (this.sessionFileChooserTabs(owner, tab.sessionHash).length) {
        throw new BrowserHostError('浏览器会话有待处理的文件选择器', {
          code: 'file_chooser_pending',
        });
      }
      return this.withGenericDownloadCapture(
        owner,
        tab,
        commandTimeoutMs,
        () => this.withSessionModalRace(
          owner,
          tab,
          () => this.coordinateClick(key, params, true),
        ),
      ) as Promise<Record<string, unknown>>;
    }
    const expectedEpoch = typeof params.expected_epoch === 'string'
      ? params.expected_epoch
      : '';
    const visualEpoch = tab.visualEpoch;
    if (!TOKEN_RE.test(expectedEpoch) || !visualEpoch || visualEpoch.token !== expectedEpoch) {
      throw new BrowserHostError('视觉截图 Host epoch 已失效，请重新截图', {
        code: 'invalid_visual_epoch',
      });
    }
    const initialIdentity = await withDeadline(
      this.currentPageIdentity(tab),
      remainingCommandTimeoutMs(commandDeadlineAt),
      () => new BrowserHostError('坐标点击前页面身份检查超时', {
        code: 'command_timeout',
        uncertain: false,
      }),
    );
    if (initialIdentity !== visualEpoch.pageIdentity) {
      tab.visualEpoch = null;
      throw new BrowserHostError('页面身份已变化，视觉截图已失效', {
        code: 'invalid_visual_epoch',
      });
    }
    const x = Number(params.x);
    const y = Number(params.y);
    if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0) {
      throw new BrowserHostError('坐标点击位置无效', { code: 'invalid_input' });
    }
    await withDeadline(
      this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim()),
      remainingCommandTimeoutMs(commandDeadlineAt),
      () => new BrowserHostError('坐标点击前应用浏览器网络配置超时', {
        code: 'command_timeout',
        uncertain: true,
        phase: 'dispatching',
      }),
    );
    const metrics = await withDeadline(
      this.send(tab, 'Page.getLayoutMetrics'),
      remainingCommandTimeoutMs(commandDeadlineAt),
      () => new BrowserHostError('坐标点击前视口检查超时', {
        code: 'command_timeout',
        uncertain: false,
      }),
    );
    const viewport = asOptionalRecord(metrics?.cssLayoutViewport ?? metrics?.layoutViewport);
    const width = Number(viewport.clientWidth);
    const height = Number(viewport.clientHeight);
    if (
      !Number.isFinite(width)
      || !Number.isFinite(height)
      || width <= 0
      || height <= 0
      || x >= width
      || y >= height
    ) {
      throw new BrowserHostError('坐标点击位置超出当前页面视口', { code: 'invalid_input' });
    }

    // Coordinate mode intentionally supports canvas, SVG, WebGL, maps,
    // custom controls and hover-revealed menus. Requiring a DOM/AX role or an
    // identical second screenshot makes those primary use cases impossible.
    // Bind only to the exact tab/document/viewport epoch and dispatch the
    // user's visual point directly.
    const dispatchIdentity = await withDeadline(
      this.currentPageIdentity(tab),
      remainingCommandTimeoutMs(commandDeadlineAt),
      () => new BrowserHostError('坐标点击派发前页面身份检查超时', {
        code: 'command_timeout',
        uncertain: false,
      }),
    );
    if (dispatchIdentity !== visualEpoch.pageIdentity) {
      tab.visualEpoch = null;
      throw new BrowserHostError('页面身份已变化，视觉截图已失效', {
        code: 'invalid_visual_epoch',
      });
    }

    tab.mouseX = x;
    tab.mouseY = y;
    // A screenshot epoch is strictly one-shot, including uncertain input errors.
    tab.visualEpoch = null;
    try {
      await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
        type: 'mouseMoved', x, y, button: 'none',
      }, remainingCommandTimeoutMs(commandDeadlineAt));
    } catch (error) {
      if (error instanceof BrowserHostError) throw error;
      throw new BrowserHostError(
        `坐标鼠标移动失败：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'input_failed' },
      );
    }
    let pressed = false;
    try {
      try {
        // Mark before awaiting CDP: a transport error can occur after Chromium
        // accepted mousePressed, so the finally block must still release it.
        pressed = true;
        await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
          type: 'mousePressed',
          x,
          y,
          button: 'left',
          buttons: 1,
          clickCount: 1,
        }, remainingCommandTimeoutMs(commandDeadlineAt));
      } catch (error) {
        if (error instanceof BrowserHostError) throw error;
        throw new BrowserHostError(
          `坐标鼠标按下结果未知：${error instanceof Error ? error.message : 'unknown'}`,
          { code: 'input_failed', uncertain: true },
        );
      }
      try {
        await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
          type: 'mouseReleased',
          x,
          y,
          button: 'left',
          buttons: 0,
          clickCount: 1,
        }, remainingCommandTimeoutMs(commandDeadlineAt));
        pressed = false;
      } catch (error) {
        if (error instanceof BrowserHostError) throw error;
        throw new BrowserHostError(
          `坐标点击已按下但释放结果未知：${error instanceof Error ? error.message : 'unknown'}`,
          { code: 'input_failed', uncertain: true },
        );
      }
    } finally {
      if (pressed) {
        await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
          type: 'mouseReleased',
          x,
          y,
          button: 'left',
          buttons: 0,
          clickCount: 1,
        }, 1_000).catch(() => undefined);
      }
    }
    return { clicked: true, x, y };
  }

  private async setMode(key: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    const tab = this.targetTab(owner, params.target_id);
    const mode = normalizeMode(params.mode);
    const candidates = [...owner.tabs.values()].filter(
      (candidate) => candidate.sessionHash === tab.sessionHash,
    );
    for (const candidate of candidates) this.assertCanSetTabMode(candidate, mode);
    const changed: Array<{ tab: BrowserTab; mode: ControlMode }> = [];
    try {
      for (const candidate of candidates) {
        const previous = candidate.mode;
        await this.setTabMode(candidate, mode);
        changed.push({ tab: candidate, mode: previous });
      }
    } catch (error) {
      let rollbackFailed = false;
      for (const entry of changed.reverse()) {
        try {
          await this.setTabMode(entry.tab, entry.mode);
        } catch {
          rollbackFailed = true;
        }
      }
      if (rollbackFailed) {
        throw new BrowserHostError('浏览器会话模式切换失败，且无法完整恢复原状态', {
          code: 'focus_mode_failed',
          uncertain: true,
          partial: true,
          completedCount: changed.length,
        });
      }
      throw error;
    }
    return { mode };
  }

  private assertCanSetTabMode(tab: BrowserTab, mode: ControlMode): void {
    const owner = this.ownerOfTab(tab);
    if (
      mode !== 'ai'
      && this.sessionDialogTabs(owner, tab.sessionHash)
        .some((candidate) => candidate.dialog?.owner === 'playwright')
    ) {
      throw new BrowserHostError(
        '请先处理当前 JavaScript 对话框，再切换为人工接管',
        { code: 'dialog_pending' },
      );
    }
    if (
      mode !== 'ai'
      && (
        owner.pendingModalActions.has(tab.sessionHash)
        || this.sessionFileChooserTabs(owner, tab.sessionHash).length > 0
      )
    ) {
      throw new BrowserHostError(
        '请先完成或取消当前 modal 动作，再切换为人工接管',
        { code: 'file_chooser_pending' },
      );
    }
  }

  private async setTabMode(tab: BrowserTab, mode: ControlMode): Promise<void> {
    this.assertCanSetTabMode(tab, mode);
    const previousMode = tab.mode;
    const previousDialogForwarding = tab.dialogForwarding;
    // Entering human/paused blocks automation immediately, before the CDP
    // focus override is removed. Returning to AI does the reverse: keep the
    // mode blocked until the override has been installed successfully.
    if (mode !== 'ai') tab.mode = mode;
    tab.dialogForwarding = mode === 'ai';
    try {
      await this.ownerOfTab(tab).engine.setAutomationMode(tab.view, mode === 'ai');
    } catch (error) {
      tab.dialogForwarding = previousDialogForwarding;
      tab.mode = previousMode;
      throw new BrowserHostError(
        `无法切换浏览器焦点模式：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'focus_mode_failed' },
      );
    }
    if (previousMode !== mode) {
      tab.visualEpoch = null;
      // A pending AI fill must not authorize exposing a value entered later by
      // a human, and a human-era value must never survive return-to-AI.
      tab.lastFilled = null;
      tab.automationFocus = null;
      tab.automationFocusPending = null;
    }
    tab.mode = mode;
    this.pageLifecycleOrigins.set(tab.view, {
      owner: this.ownerOfTab(tab),
      sessionHash: tab.sessionHash,
      mode,
      webContentsId: tab.webContentsId,
      downloadDir: tab.downloadDir,
    });
    if (this.panel?.tab === tab) {
      if (mode === 'human') tab.view.webContents.focus();
      else this.panel.window.webContents.focus();
    }
  }

  private mountHumanPopup(owner: BrowserOwner, opener: BrowserTab, popup: BrowserTab): void {
    const panel = this.panel;
    if (!panel || panel.owner !== owner || panel.tab !== opener || opener.mode !== 'human') return;
    popup.view.setBounds(panel.bounds);
    popup.view.setVisible(false);
    // createTab() initially mounts every view in AutomationHost. Move it through
    // the engine API before attaching it to the visible panel so the host's
    // mounted bookkeeping cannot retain a stale strong reference.
    owner.engine.releaseToPanel(popup.view);
    panel.window.contentView.addChildView(popup.view);
    this.detachPanel(panel);
    popup.view.setVisible(true);
    this.panel = { ...panel, tab: popup };
    popup.view.webContents.focus();
  }

  private popupDescendsFrom(owner: BrowserOwner, popup: BrowserTab, ancestor: BrowserTab): boolean {
    const visited = new Set<string>();
    let openerTargetId = popup.openerTargetId;
    while (openerTargetId) {
      if (visited.has(openerTargetId)) return false;
      visited.add(openerTargetId);
      if (openerTargetId === ancestor.targetId) return true;
      const found = this.tabsByTarget.get(openerTargetId);
      if (
        !found
        || found.owner !== owner
        || found.tab.sessionHash !== popup.sessionHash
      ) return false;
      openerTargetId = found.tab.openerTargetId;
    }
    return false;
  }

  private async denyDownloads(
    key: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const owner = this.owners.get(key);
    if (!owner) return { denied: true };
    this.verifyProfileIfPresent(owner, params.profile_dir);
    this.preemptOwnerQueue(key);
    await this.cancelDownloadGrant(
      owner,
      new BrowserHostError('下载授权已撤销', { code: 'download_denied' }),
    );
    return { denied: true };
  }

  private async download(key: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    const tab = this.targetTab(owner, params.target_id);
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止发起自动下载', {
        code: 'control_mode_blocked',
      });
    }
    const refValue = asString(params.ref, 'ref', 100);
    // 提前解析一次，让「ref 不属于当前快照」这类错误在做任何路径/预算校验之前就抛出。
    this.ref(tab, refValue);
    const rawTarget = asString(params.target, 'download target');
    if (!path.isAbsolute(rawTarget)) {
      throw new BrowserHostError('下载目标必须是绝对路径', { code: 'invalid_download_path' });
    }
    const target = canonicalPath(rawTarget);
    if (owner.downloadGrant) {
      throw new BrowserHostError('账号已有进行中的下载', { code: 'download_busy' });
    }
    const timeoutMs = downloadTimeoutMs(params);
    const commandDeadlineAt = Date.now() + timeoutMs;
    await withDeadline(
      this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim()),
      remainingCommandTimeoutMs(commandDeadlineAt),
      () => new BrowserHostError('下载前应用浏览器网络配置超时', {
        code: 'command_timeout',
        uncertain: true,
        phase: 'dispatching',
      }),
    );
    const downloadCtx = await this.actionContext(
      tab,
      remainingCommandTimeoutMs(commandDeadlineAt),
    );
    const timerDelayMs = remainingCommandTimeoutMs(commandDeadlineAt);
    const result = new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (owner.downloadGrant?.target !== target) return;
        void this.cancelDownloadGrant(
          owner,
          new BrowserHostError('等待浏览器下载超时', {
            code: 'download_timeout',
            uncertain: true,
          }),
        ).catch(() => undefined);
      }, timerDelayMs);
      timer.unref();
      owner.downloadGrant = {
        tabId: tab.tabId,
        target,
        claimed: false,
        item: null,
        actionActive: false,
        actionDeadline: 0,
        eventBaseline: owner.downloadEventSequence,
        resolve,
        reject,
        timer,
      };
    });
    try {
      await pwActions.clickArmed(downloadCtx, refValue, () => {
        const grant = owner.downloadGrant;
        if (!grant || grant.target !== target) {
          throw new BrowserHostError('下载授权在点击前已失效', {
            code: 'download_grant_expired',
          });
        }
        grant.eventBaseline = owner.downloadEventSequence;
        grant.actionActive = true;
        grant.actionDeadline = commandDeadlineAt;
      });
    } catch (error) {
      await this.cancelDownloadGrant(
        owner,
        error instanceof BrowserHostError
          ? error
          : new BrowserHostError('下载点击失败', { code: 'download_click_failed' }),
      );
      throw error;
    }
    return result;
  }

  private suggestedDownloadFilename(item: DownloadItem): string {
    try {
      return item.getFilename();
    } catch {
      return '';
    }
  }

  private uniqueGenericDownloadTarget(
    owner: BrowserOwner,
    downloadDir: string,
    suggestedFilename: string,
  ): string {
    let basename = Array.from(
      path.basename(suggestedFilename || 'download').replace(/[<>:"/\\|?*]/g, '_'),
      (character) => {
        const codePoint = character.codePointAt(0) ?? 0;
        return codePoint < 32 || codePoint === 127 ? '_' : character;
      },
    ).join('').replace(/[ .]+$/g, '');
    if (!basename || basename === '.' || basename === '..') basename = 'download';
    const parsed = path.parse(basename);
    let ordinal = 0;
    while (true) {
      const candidateName = ordinal === 0
        ? basename
        : `${parsed.name || 'download'} (${ordinal})${parsed.ext}`;
      const candidate = path.join(downloadDir, candidateName);
      const key = pathKey(candidate);
      if (!owner.reservedDownloadPaths.has(key) && !existsSync(candidate)) {
        owner.reservedDownloadPaths.add(key);
        return candidate;
      }
      ordinal += 1;
    }
  }

  private emitGenericDownload(
    owner: BrowserOwner,
    result: GenericDownloadResult,
  ): void {
    this.emit('download', {
      type: 'download',
      runtimeKey: owner.runtimeKey,
      ...result,
    });
  }

  private saveGenericDownload(
    owner: BrowserOwner,
    tab: BrowserTab,
    item: DownloadItem,
  ): void {
    if (!tab.downloadDir) return;
    const suggestedFilename = this.suggestedDownloadFilename(item);
    const target = this.uniqueGenericDownloadTarget(
      owner,
      tab.downloadDir,
      suggestedFilename,
    );
    let url = '';
    try {
      url = item.getURL();
    } catch {
      url = '';
    }
    let totalBytes = 0;
    try {
      totalBytes = Math.max(0, item.getTotalBytes());
    } catch {
      totalBytes = 0;
    }
    const result: GenericDownloadResult = {
      downloadId: randomUUID(),
      targetId: tab.targetId,
      sessionHash: tab.sessionHash,
      path: target,
      name: path.basename(target),
      suggestedFilename,
      url,
      state: 'progressing',
      receivedBytes: 0,
      totalBytes,
      createdAt: Date.now(),
      completedAt: 0,
      error: '',
    };
    const maxBytes = tab.downloadMaxBytes;
    let transferLimitExceeded = Boolean(
      maxBytes > 0 && totalBytes > maxBytes,
    );
    // Host RPCs are serialized per owner, but nested public Page lifecycles
    // and future transport changes must not turn capture bookkeeping into a
    // cross-session `download_busy` failure.  Attribute to the newest matching
    // action; unrelated timer downloads still persist and publish normally.
    const capture = this.genericDownloadCaptureForTab(owner, tab);
    if (capture) {
      capture.downloads.push(result);
      for (const finish of [...capture.nativeWaiters]) finish();
    }
    try {
      item.setSavePath(target);
    } catch (error) {
      owner.reservedDownloadPaths.delete(pathKey(target));
      result.state = 'interrupted';
      result.completedAt = Date.now();
      result.error = error instanceof Error
        ? error.message
        : '无法设置浏览器下载路径';
      this.emitGenericDownload(owner, result);
      try {
        item.cancel();
      } catch {
        // The terminal event already exposes the failure.
      }
      return;
    }
    if (transferLimitExceeded) {
      result.state = 'interrupted';
      result.completedAt = Date.now();
      result.error = `下载超过 ${maxBytes} 字节传输上限`;
      this.emitGenericDownload(owner, result);
      try {
        item.cancel();
      } catch {
        // The public result already exposes the rejected download.
      }
      void unlink(target).catch(() => undefined);
      return;
    }
    const refreshBytes = (): void => {
      try {
        result.receivedBytes = Math.max(0, item.getReceivedBytes());
      } catch {
        result.receivedBytes = 0;
      }
      try {
        result.totalBytes = Math.max(0, item.getTotalBytes());
      } catch {
        // Keep the last known total when Electron temporarily detaches state.
      }
      if (!transferLimitExceeded && maxBytes > 0 && result.receivedBytes > maxBytes) {
        transferLimitExceeded = true;
        result.state = 'interrupted';
        result.error = `下载超过 ${maxBytes} 字节传输上限`;
        try {
          item.cancel();
        } catch {
          // The terminal event below still reports the interrupted state.
        }
        void unlink(target).catch(() => undefined);
      }
    };
    let lastProgressKey = [
      result.state,
      result.receivedBytes,
      result.totalBytes,
    ].join(':');
    const onUpdated = (
      _event: ElectronEvent,
      state: 'progressing' | 'interrupted',
    ): void => {
      result.state = state;
      refreshBytes();
      result.error = state === 'interrupted'
        ? '浏览器下载暂时中断'
        : '';
      const progressKey = [
        result.state,
        result.receivedBytes,
        result.totalBytes,
      ].join(':');
      if (progressKey === lastProgressKey) return;
      lastProgressKey = progressKey;
      this.emitGenericDownload(owner, result);
    };
    item.on('updated', onUpdated);
    item.once('done', (_event, state) => {
      item.removeListener('updated', onUpdated);
      owner.reservedDownloadPaths.delete(pathKey(target));
      result.state = state;
      refreshBytes();
      result.completedAt = Date.now();
      if (transferLimitExceeded) {
        result.state = 'interrupted';
        result.error = `下载超过 ${maxBytes} 字节传输上限`;
        void unlink(target).catch(() => undefined);
      } else if (state !== 'completed') {
        result.error = `浏览器下载状态：${state}`;
      }
      this.emitGenericDownload(owner, result);
    });
    this.emitGenericDownload(owner, result);
  }

  private handleWillDownload(
    owner: BrowserOwner,
    _event: ElectronEvent,
    item: DownloadItem,
    contents: WebContents,
  ): void {
    owner.downloadEventSequence += 1;
    const eventSequence = owner.downloadEventSequence;
    const grant = owner.downloadGrant;
    const found = this.tabsByWebContentsId.get(contents.id);
    const actionExpired = Boolean(grant?.actionActive && Date.now() > grant.actionDeadline);
    const sourceTab = grant ? owner.tabs.get(grant.tabId) : null;
    const sourceMatches = Boolean(
      grant
      && found
      && found.owner === owner
      && (
        found.tab.tabId === grant.tabId
        || sourceTab && this.popupDescendsFrom(owner, found.tab, sourceTab)
      ),
    );
    const matchesAction = Boolean(
      grant
      && grant.actionActive
      && !actionExpired
      && eventSequence > grant.eventBaseline
      && sourceMatches,
    );
    if (grant && !grant.claimed && matchesAction) {
      // Highest priority: browser_download owns one exact item and target.
      // Return before generic routing can call setSavePath.
      grant.claimed = true;
      grant.actionActive = false;
      grant.item = item;
      try {
        item.setSavePath(grant.target);
      } catch (error) {
        owner.engine.registerNativeDownload(found!.tab.view, item);
        void this.cancelDownloadGrant(
          owner,
          new BrowserHostError(
            `无法设置下载保存路径：${
              error instanceof Error ? error.message : 'unknown'
            }`,
            { code: 'download_save_path_failed' },
          ),
        ).catch(() => undefined);
        return;
      }
      owner.engine.registerNativeDownload(found!.tab.view, item);
      item.once('done', (_doneEvent, state) => {
        if (owner.downloadGrant !== grant) return;
        clearTimeout(grant.timer);
        owner.downloadGrant = null;
        if (state !== 'completed') {
          owner.downloadGrant = grant;
          void this.cancelDownloadGrant(
            owner,
            new BrowserHostError('浏览器下载未完成', {
              code: 'download_failed',
              uncertain: true,
            }),
          ).catch(() => undefined);
          return;
        }
        grant.resolve({
          path: grant.target,
          name: item.getFilename(),
          bytes: item.getReceivedBytes(),
        });
      });
      return;
    }

    if (grant && actionExpired) {
      void this.cancelDownloadGrant(
        owner,
        new BrowserHostError('下载事件未在已绑定点击窗口内开始', {
          code: 'download_action_expired',
        }),
      ).catch(() => undefined);
    }
    if (!found || found.owner !== owner) return;

    // Every ordinary browser behavior is persisted to the
    // current task directory, including concurrent and timer-driven items.
    this.saveGenericDownload(owner, found.tab, item);
    owner.engine.registerNativeDownload(found.tab.view, item);
  }

  private async cancelDownloadGrant(owner: BrowserOwner, error: BrowserHostError): Promise<void> {
    const grant = owner.downloadGrant;
    if (!grant) return;
    clearTimeout(grant.timer);
    owner.downloadGrant = null;
    let cleanupError: BrowserHostError | null = null;
    try {
      await this.cancelDownloadItem(grant);
    } catch (failure) {
      cleanupError = failure instanceof BrowserHostError
        ? failure
        : new BrowserHostError('无法删除浏览器下载临时文件', {
            code: 'download_cleanup_failed',
            uncertain: true,
          });
    }
    grant.reject(cleanupError ?? error);
    if (cleanupError) throw cleanupError;
  }

  private async cancelDownloadItem(grant: DownloadGrant): Promise<void> {
    if (grant.item) {
      let state: string = 'progressing';
      try {
        state = grant.item.getState();
      } catch {
        // Some Electron builds do not expose state after the item has terminally detached.
      }
      if (state === 'progressing' || state === 'interrupted') {
        await new Promise<void>((resolve) => {
          const item = grant.item;
          let settled = false;
          let timer: NodeJS.Timeout | null = null;
          const finish = (): void => {
            if (settled) return;
            settled = true;
            if (timer) clearTimeout(timer);
            item?.removeListener('done', finish);
            resolve();
          };
          timer = setTimeout(finish, 1_000);
          timer.unref();
          item?.once('done', finish);
          try {
            item?.cancel();
          } catch {
            finish();
          }
        });
      }
    }
    if (!grant.claimed) return;
    try {
      await unlink(grant.target);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        throw new BrowserHostError('无法删除浏览器下载临时文件', {
          code: 'download_cleanup_failed',
          uncertain: true,
        });
      }
    }
  }

  private async closeTargetRpc(
    key: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    const tab = this.targetTab(owner, params.target_id);
    this.closeTab(owner, tab);
    return { closed: true };
  }

  private async closeOwner(
    key: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const owner = this.owners.get(key);
    if (!owner) return { closed: true };
    this.verifyProfileIfPresent(owner, params.profile_dir);
    this.preemptOwnerQueue(key);
    owner.lifecycle = 'closing';
    try {
      await this.destroyOwner(owner);
    } finally {
      if (this.owners.get(key) === owner) this.owners.delete(key);
    }
    return { closed: true };
  }

  private async clearOwnerData(
    key: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    this.preemptOwnerQueue(key);
    owner.lifecycle = 'clearing';
    try {
      let cleanupError: unknown;
      try {
        await this.cancelDownloadGrant(
          owner,
          new BrowserHostError('浏览数据清理已取消下载', { code: 'download_cancelled' }),
        );
      } catch (error) {
        cleanupError = error;
      }
      this.hidePanelIfOwner(owner);
      for (const tab of [...owner.tabs.values()]) this.closeTab(owner, tab);
      // Tabs are already closed, so no page can repopulate storage. Keep these
      // documented Session operations ordered: clear persistent state first,
      // then tear down any transport still held by the shared Session.
      await owner.session.clearData();
      await owner.session.clearAuthCache();
      await owner.session.clearHostResolverCache();
      await owner.session.closeAllConnections();
      if (cleanupError) throw cleanupError;
    } finally {
      if (this.owners.get(key) === owner) owner.lifecycle = 'active';
    }
    return { cleared: true };
  }

  private verifyProfileIfPresent(owner: BrowserOwner, value: unknown): void {
    if (value === undefined || value === null || value === '') return;
    if (!samePath(profilePath(value, owner.runtimeKey), owner.profilePath)) {
      throw new BrowserHostError('账号浏览器 Profile 不匹配', { code: 'profile_mismatch' });
    }
  }

  private async destroyOwner(owner: BrowserOwner): Promise<void> {
    this.hidePanelIfOwner(owner);
    let cleanupError: unknown;
    try {
      await this.cancelDownloadGrant(
        owner,
        new BrowserHostError('账号浏览器已关闭', {
          code: 'owner_closed',
          browserStopped: true,
        }),
      );
    } catch (error) {
      cleanupError = error;
    }
    try {
      for (const tab of [...owner.tabs.values()]) this.closeTab(owner, tab);
      await owner.engine.dispose().catch(() => undefined);
      await owner.session.closeAllConnections().catch(() => undefined);
    } finally {
      // session.fromPath() returns the same Session object after an idle/close
      // restart. Removing the owner-bound listener prevents a stale listener
      // from cancelling the next owner's one-shot download grant.
      this.detachSession(owner);
    }
    if (cleanupError) throw cleanupError;
  }

  private closeTab(
    owner: BrowserOwner,
    tab: BrowserTab,
  ): void {
    if (this.panel?.tab === tab) this.hidePanel();
    if (owner.downloadGrant?.tabId === tab.tabId) {
      void this.cancelDownloadGrant(
        owner,
        new BrowserHostError('下载标签页已关闭', {
          code: 'download_tab_closed',
          uncertain: owner.downloadGrant.claimed,
        }),
      ).catch(() => undefined);
    }
    this.revokeArtifact(owner, tab);
    owner.engine.unregisterTab(tab.view);
    this.forgetTab(owner, tab);
    const contents = tab.view.webContents;
    if (contents && !contents.isDestroyed()) {
      if (contents.debugger.isAttached()) {
        try {
          contents.debugger.detach();
        } catch {
          // Closing the WebContents below is the authoritative cleanup.
        }
      }
      contents.close({ waitForBeforeUnload: false });
    }
  }

  private forgetTab(owner: BrowserOwner, tab: BrowserTab): void {
    const activeFallback = owner.activeTabId === tab.tabId
      ? this.activeFallbackAfterClose(owner, tab)
      : undefined;
    owner.tabs.delete(tab.tabId);
    this.tabsByTarget.delete(tab.targetId);
    this.tabsByWebContentsId.delete(tab.webContentsId);
    if (owner.activeTabId === tab.tabId) {
      owner.activeTabId = activeFallback?.tabId ?? owner.tabs.keys().next().value ?? '';
    }
  }

  private activeFallbackAfterClose(owner: BrowserOwner, closing: BrowserTab): BrowserTab | undefined {
    const opener = closing.openerTargetId
      ? this.tabsByTarget.get(closing.openerTargetId)
      : undefined;
    if (
      opener?.owner === owner
      && opener.tab !== closing
      && opener.tab.sessionHash === closing.sessionHash
      && !opener.tab.crashed
      && !opener.tab.view.webContents.isDestroyed()
    ) {
      return opener.tab;
    }
    return [...owner.tabs.values()].find(
      (candidate) => (
        candidate !== closing
        && candidate.sessionHash === closing.sessionHash
        && !candidate.crashed
        && !candidate.view.webContents.isDestroyed()
      ),
    );
  }

  private requirePanelTab(
    owner: BrowserOwner,
    rawSessionId: string,
    labelOrId: string,
  ): BrowserTab {
    const id = asString(rawSessionId, 'sessionId', 4096);
    const identity = asString(labelOrId, 'tabLabel', 256);
    if (!id) throw new BrowserHostError('sessionId 不能为空', { code: 'invalid_session' });
    const expectedHash = sessionHash(id);
    const matches = [...owner.tabs.values()].filter(
      (tab) => (tab.label === identity || tab.tabId === identity) && tab.sessionHash === expectedHash,
    );
    if (matches.length !== 1) {
      throw new BrowserHostError('浏览器标签页不属于当前 Crew 会话', {
        code: 'foreign_session_tab',
      });
    }
    return matches[0];
  }

  private clampBounds(bounds: Rectangle, window: BrowserWindow): Rectangle | null {
    const content = window.getContentBounds();
    const x = Math.max(0, Math.floor(Number(bounds.x)));
    const y = Math.max(0, Math.floor(Number(bounds.y)));
    const right = Math.min(content.width, Math.ceil(Number(bounds.x) + Number(bounds.width)));
    const bottom = Math.min(content.height, Math.ceil(Number(bounds.y) + Number(bounds.height)));
    if (![x, y, right, bottom].every(Number.isFinite) || right <= x || bottom <= y) return null;
    return { x, y, width: right - x, height: bottom - y };
  }

  private detachPanel(panel: { owner?: BrowserOwner; tab: BrowserTab; window: BrowserWindow }): void {
    try {
      panel.tab.view.setVisible(false);
    } catch {
      // The underlying WebContents may already have been destroyed by the page.
    }
    if (!panel.window.isDestroyed()) {
      try {
        panel.window.contentView.removeChildView(panel.tab.view);
      } catch {
        // A concurrent BrowserWindow close may already have detached the view.
      }
    }
    // 面板收起后把 view 收回后台自动化宿主。不收回的话它就成了一个既不可见、
    // 又不挂在任何窗口上的游离 view —— 那正是 Playwright 点不动的状态。
    if (panel.owner && !panel.tab.view.webContents.isDestroyed()) {
      panel.owner.engine.reclaimFromPanel(panel.tab.view);
    }
  }

  private recoverPanelAfterTabFailure(owner: BrowserOwner, failed: BrowserTab): void {
    const panel = this.panel;
    if (!panel || panel.owner !== owner || panel.tab !== failed) return;
    this.detachPanel(panel);
    this.panel = null;

    const openerFound = failed.openerTargetId
      ? this.tabsByTarget.get(failed.openerTargetId)
      : undefined;
    const opener = openerFound?.owner === owner ? openerFound.tab : null;
    if (
      !opener
      || opener.sessionHash !== failed.sessionHash
      || opener.mode !== 'human'
      || opener.crashed
      || opener.view.webContents.isDestroyed()
      || panel.window.isDestroyed()
    ) {
      if (!panel.window.isDestroyed()) panel.window.webContents.focus();
      return;
    }
    try {
      opener.view.setBounds(panel.bounds);
      owner.engine.releaseToPanel(opener.view);
      panel.window.contentView.addChildView(opener.view);
      opener.view.setVisible(true);
      this.panel = { ...panel, tab: opener };
      owner.activeTabId = opener.tabId;
      opener.view.webContents.focus();
    } catch {
      this.panel = null;
      if (!panel.window.isDestroyed()) panel.window.webContents.focus();
    }
  }

  private hidePanelIfOwner(owner: BrowserOwner): void {
    if (this.panel?.owner === owner) this.hidePanel();
  }
}
