import { createHash, randomUUID } from 'node:crypto';
import { constants as fsConstants, existsSync, realpathSync } from 'node:fs';
import {
  chmod,
  copyFile,
  lstat,
  open,
  readdir,
  rename,
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

import {
  RECORDER_BINDING,
  RECORDER_CONTROL,
  RECORDER_EVENT_SCHEMA_VERSION,
  RECORDER_PROVENANCE_SCHEMA_VERSION,
  RECORDER_TARGET_STASH,
  retainRecorderEvidence,
  parseRecorderControlEvent,
  parseRecorderEvent,
  recorderScript,
  type RecorderDialogAction,
  type RecorderDialogType,
  type RecorderEvent,
  type RecorderEventType,
} from './browser-recorder';
import * as pwActions from './browser/playwright-actions';
import * as pwConsole from './browser/playwright-console';
import * as pwNetwork from './browser/playwright-network';
import { PlaywrightEngine } from './browser/playwright-engine';
import {
  locatorFromRef,
  officialInjectedScriptSource,
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
// `@eN` 来自快照，`@sN` 来自 `locate`（技能存盘的稳定选择器）。两者进同一张 ref 表，
// 因此动作层不必区分来路；前缀只是为了让日志与报错能一眼看出这个 ref 是怎么来的。
const NATIVE_REF_RE = /^@[es]([1-9][0-9]*)$/;
const GUARD_KEY_RE = /^__crew_guard_[0-9a-f]{32}$/;
const TOKEN_RE = /^[0-9a-f]{32}$/;
const RECORDING_ID_RE = /^[0-9a-f]{8,32}$/;
const PAGE_GUID_RE = /^p(?:0|[1-9][0-9]*)$/;
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
// Must settle before ElectronCdpTransport's 5s child-session barrier. The
// transport timeout remains a final graph-liveness fuse; this earlier deadline
// gives BrowserHost time to audit, stop and arm late-install cleanup first.
const NATIVE_INPUT_PROOF_TTL_MS = 1_200;
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
  causalId: number;
}

interface ExpectedDialog {
  type: RecorderDialogType;
  action: RecorderDialogAction;
  text: string;
  /** Resolved replay target for a page that already exists. */
  targetId: string;
  /** Resolved opener target for a popup created by the triggering action. */
  openerTargetId: string;
  /** One-based creation order among popups from the same opener. */
  popupOrdinal: number | null;
}

interface ExpectedDialogObservation {
  expectedIndex: number;
  actualType: string;
  /** Dialogs may be opened by a popup rather than the triggering page. */
  targetId: string;
  handlerDone: boolean;
  closeDone: boolean;
}

interface ExpectedDialogRun {
  sessionHash: string;
  triggerTargetId: string;
  timeoutMs: number;
  dialogs: ExpectedDialog[];
  /** Number of opening events already assigned to an expected slot. */
  opened: number;
  /** Number of fully type/result-validated slots, always advanced in FIFO order. */
  index: number;
  observations: ExpectedDialogObservation[];
  actionFinished: boolean;
  settled: boolean;
  resolve: () => void;
  reject: (error: BrowserHostError) => void;
  timer: NodeJS.Timeout;
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

interface RecordingLedger {
  recordingId: string;
  /** Frozen at start: changing the feature flag cannot mix rows in one trace. */
  schemaVersion: 10 | 11;
  startedAt: number;
  steps: number;
  forged: number;
  /**
   * A recording is a correctness artifact: one missing action makes the whole
   * trace unsafe to compile.  Keep this on the shared popup ledger so failures
   * in any member are reported by the eventual group stop.
   */
  incomplete: boolean;
  dropped: number;
  /** Set synchronously before group stop drains; no later popup may join. */
  closing: boolean;
  /** Global order across opener and every inherited popup. */
  queue: Promise<void>;
  /** Append-only v11 row order across every page in the shared recording. */
  eventIndex: number;
  /** Stable transaction ids are independent from event indices and timestamps. */
  transactionCounter: number;
  /** A browser task may produce late popup/navigation/dialog/download signals. */
  transactionsByCausalId: Map<number, V11TransactionIdentity>;
  reservedActions: WeakMap<RecorderEvent, V11TransactionIdentity>;
  dialogCounter: number;
  downloadCounter: number;
  downloadOrdinals: Map<string, number>;
  members: Set<BrowserTab>;
  causalCounter: number;
  /** Stable, recording-local page identities; never persist native target ids. */
  pageCounter: number;
  /** Per-opener creation order disambiguates several concurrent popups. */
  popupOrdinals: Map<string, number>;
  activeCausals: Map<string, {
    seq: number;
    causalId: number;
    capturedAt: number;
    eventType: RecorderEventType;
    scope: 'event' | 'input';
  }>;
}

interface V11TransactionIdentity {
  transactionId: number;
  step: number;
  transactionKind: 'action' | 'observation';
}

type RecordedNavigationOperation = 'goto' | 'back' | 'forward' | 'reload';

interface PendingRecordedNavigation {
  operation: RecordedNavigationOperation;
  /** Non-empty only for goto; history/reload preserve their native semantics. */
  url: string;
  state: RecordingState;
  capturedAt: number;
  committed: boolean;
  cancelled: boolean;
  observed: Promise<void>;
  settleObserved: () => void;
}

interface RecordingState {
  /** Shared by the opener and popups; session/controller state below remains per tab. */
  ledger: RecordingLedger;
  /** Stable page identity used by the compiler/replayer (p1, p2, ...). */
  pageId: string;
  openerPageId: string;
  popupOrdinal: number;
  /** Exact opener task that created this page, when creation was synchronous. */
  createdByCausalId: number;
  /** Observation transaction that introduced this popup when no action existed. */
  createdByTransaction: V11TransactionIdentity | null;
  /** The initial page-open anchor is queued exactly once. */
  initialPageRecorded: boolean;
  /** Concurrent start callers join one installation instead of double-injecting. */
  installation: Promise<void> | null;
  paused: boolean;
  /** False as soon as stop begins; already queued events still drain into this state. */
  accepting: boolean;
  /** Allows only recorder-controller lifecycleFlush packets while pause/stop drains. */
  drainingFlush: boolean;
  /** Page listeners return immediately while false; kept separate from queue acceptance. */
  captureEnabled: boolean;
  /** Per-recording unpredictable main-world globals. */
  bindingName: string;
  targetStashName: string;
  controlName: string;
  /** Main page (`''`) and every live OOPIF own independent CDP registrations. */
  sessions: Map<string, RecorderSessionState>;
  lastPageDigest: string;
  /**
   * Last CSS viewport persisted for this recording-local page.
   *
   * Electron WebContentsView bounds are expressed in display-independent
   * pixels, which are the renderer's CSS viewport pixels. Keep x/y out of this
   * identity: moving the panel must not create a replay step.
   */
  lastRecordedViewport: { width: number; height: number } | null;
  /** Runtime context ids are only unique inside one flattened CDP session. */
  contexts: Set<string>;
  /** Exact document frame id for each observed default-world execution context. */
  contextFrames: Map<string, string>;
  /** Page-local input burst token -> durable host causal identity. */
  causalTokens: Map<string, {
    causalId: number;
    capturedAt: number;
  }>;
  /** Clicks are briefly buffered so a native dblclick becomes one replay action. */
  pendingClicks: Map<string, RecorderEvent>;
  supersededClicks: WeakSet<RecorderEvent>;
  /**
   * Exact action identity captured when the main-frame navigation starts.
   *
   * The originating document (and its Runtime execution context) is normally
   * destroyed before Electron emits did-navigate. Reading activeCausals only at
   * commit time therefore turns a click-triggered POST/redirect into a
   * standalone goto. Keep the task identity across that document boundary.
   */
  pendingNavigationCausal: {
    causalId: number;
    capturedAt: number;
  } | null;
  /**
   * Explicit human address-bar/history command armed at its dispatch boundary.
   * did-navigate commits it synchronously so an unrelated later click/timer
   * cannot be absorbed into the wrong transaction.
   */
  pendingRecordedNavigation: PendingRecordedNavigation | null;
  /** Successful Playwright fallback commits whose Electron observer may arrive late. */
  ignoredRecordedNavigationUrls: Array<{ url: string; expiresAt: number }>;
  /** Prevent closeTab + WebContents.destroyed from emitting two tombstones. */
  pageCloseRecorded: boolean;
}

interface RecorderSessionState {
  scriptId: string;
  bindingName: string;
  bindingAdded: boolean;
  installed: boolean;
  cancelled: boolean;
  installation: Promise<void>;
}

interface RecorderExecutionContext {
  /** Empty means the WebContents' main page session. */
  sessionId: string;
  executionContextId: number;
}

type NativeProofKind = 'keyboard' | 'pointer' | 'scroll';
type NativeProofEvent = Exclude<RecorderEvent['type'], 'navigate'>;

interface NativeInputProof {
  kind: NativeProofKind;
  expiresAt: number;
  /** 一个原生输入最多授权各一种有因果关系的派生事件，不是时间窗内无限放行。 */
  remaining: Set<NativeProofEvent>;
  /** 键盘事件额外绑定原生 key；input/submit 派生事件不要求该字段。 */
  key: string;
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
  /** `locate` 解析出的 ref 计数，用来生成 `@sN`（与快照的 `@eN` 区分开）。 */
  locateCounter: number;
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
  /** 至多 keyboard/pointer/scroll 各一个、按事件类型一次性消费的真人输入证明。 */
  nativeInputProofs: NativeInputProof[];
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
  // 录制态。**正交于 `mode`**，不做成第四个 ControlMode 值：录制期间 AI 动作
  // 依然要被 `_require_ai()` 拒绝，把它并进 ControlMode 会把这两件事的语义搅在
  // 一起。每个 CDP session 的 script id 由 RecordingState.sessions 独立持有。
  recording: RecordingState | null;
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
  /** One atomic expected-dialog transaction per logical browser session. */
  expectedDialogRuns: Map<string, ExpectedDialogRun>;
  /** Original full command continuation retained while a modal is surfaced. */
  pendingModalActions: Map<string, PendingModalAction>;
  /** Waiters are armed before dispatch/accept to close every event-order race. */
  modalWaiters: Map<string, Set<(signal: SessionModalSignal) => void>>;
  /** One replay.v3 transaction per logical browser session. */
  atomicTransactions: Map<string, AtomicTransactionRun>;
  /**
   * Replay epoch state survives the gap between two execute_transaction RPCs.
   * Timer-driven popup/navigation/dialog/download/close signals can otherwise
   * happen after the triggering action returned but before its x-crew-wait*
   * transaction was armed.
   */
  atomicReplayEpochs: Map<string, AtomicReplayEpoch>;
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

type AtomicEffect =
  | { kind: 'navigation'; page: string; url: string }
  | {
    kind: 'popup';
    page: string;
    opener_page: string;
    popup_index: number;
    activate: boolean;
    disposition: string;
  }
  | {
    kind: 'download';
    page: string;
    alias: string;
    ordinal: number;
    suggested_filename: string;
  }
  | {
    kind: 'dialog';
    page: string;
    alias: string;
    type: RecorderDialogType;
    accept: boolean;
    text: string;
  }
  | { kind: 'page_closed'; page: string; reason: string };

interface AtomicDownloadResult {
  alias: string;
  pageGuid: string;
  ordinal: number;
  suggestedFilename: string;
  path: string;
  state: string;
  receivedBytes: number;
}

type AtomicJournalEvent =
  | {
    kind: 'navigation';
    targetId: string;
    url: string;
  }
  | {
    kind: 'popup';
    openerTargetId: string;
    targetId: string;
    popupOrdinal: number;
    activate: boolean;
    disposition: string;
  }
  | {
    kind: 'download';
    targetId: string;
    ordinal: number;
    suggestedFilename: string;
    result: Omit<AtomicDownloadResult, 'alias' | 'pageGuid'>;
    completion: Promise<void>;
  }
  | {
    kind: 'dialog';
    targetId: string;
    type: RecorderDialogType;
    accept: boolean;
    text: string;
  }
  | {
    kind: 'page_closed';
    targetId: string;
    reason: string;
  };

interface AtomicJournalEntry {
  sequence: number;
  event: AtomicJournalEvent;
  consumed: boolean;
}

interface AtomicReplayEpoch {
  sessionHash: string;
  epochId: string;
  lastTransactionId: number;
  nextEventSequence: number;
  /** Earliest sequence still eligible for the next observation-only wait. */
  historicalFloor: number;
  journal: AtomicJournalEntry[];
  pageGuidByTargetId: Map<string, string>;
  targetIdByPageGuid: Map<string, string>;
  popupOrdinalBases: Map<string, number>;
  closedPageGuids: Set<string>;
  downloadOrdinals: Map<string, number>;
  downloadDir: string;
}

interface AtomicTransactionRun {
  transactionId: number;
  sessionHash: string;
  epoch: AtomicReplayEpoch;
  /** Effects of ordinary actions must be newer than this arm point. */
  armSequence: number;
  /** Observation-only x-crew-wait* actions may consume the journal backlog. */
  allowHistoricalEffects: boolean;
  historicalFloor: number;
  deadlineAt: number;
  downloadDir: string;
  expectedEffects: AtomicEffect[];
  matchedEffects: AtomicEffect[];
  effectCursor: number;
  /** Primary x-crew-wait* effect is observed but omitted from matchedEffects. */
  reportedEffectStart: number;
  pageGuidByTargetId: Map<string, string>;
  targetIdByPageGuid: Map<string, string>;
  popupOrdinalBases: Map<string, number>;
  closedPageGuids: Set<string>;
  downloads: AtomicDownloadResult[];
  downloadCompletions: Promise<void>[];
  completion: Promise<void>;
  resolve: () => void;
  reject: (error: BrowserHostError) => void;
  settled: boolean;
  timer: NodeJS.Timeout;
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

function exactKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const keys = Object.keys(value);
  return (
    required.every((key) => Object.prototype.hasOwnProperty.call(value, key))
    && keys.every((key) => required.includes(key) || optional.includes(key))
  );
}

function recordingV11Enabled(): boolean {
  return process.env.CREW_BROWSER_RECORDING_V11_PHASE_A !== '0';
}

function strictPageGuid(value: unknown, label: string): string {
  const pageGuid = asString(value, label);
  if (!PAGE_GUID_RE.test(pageGuid)) {
    throw new BrowserHostError(`${label}无效`, { code: 'invalid_transaction' });
  }
  return pageGuid;
}

function strictTransactionInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) <= 0) {
    throw new BrowserHostError(`${label}必须是正安全整数`, {
      code: 'invalid_transaction',
    });
  }
  return Number(value);
}

function strictTransactionString(
  value: unknown,
  label: string,
  allowEmpty = false,
): string {
  const text = asString(value, label);
  if (!allowEmpty && !text) {
    throw new BrowserHostError(`${label}不能为空`, { code: 'invalid_transaction' });
  }
  return text;
}

function strictTransactionPoint(
  value: unknown,
  label: string,
): { x: number; y: number } | null {
  if (value === null) return null;
  const point = asRecord(value, label);
  if (
    !exactKeys(point, ['x', 'y'])
    || typeof point.x !== 'number'
    || typeof point.y !== 'number'
    || !Number.isFinite(point.x)
    || !Number.isFinite(point.y)
    || point.x < 0
    || point.y < 0
  ) {
    throw new BrowserHostError(`${label}无效`, { code: 'invalid_transaction' });
  }
  return { x: point.x, y: point.y };
}

const POINTER_TELEMETRY_RANGES = {
  pressure: [0, 1],
  tangentialPressure: [-1, 1],
  tiltX: [-90, 90],
  tiltY: [-90, 90],
  twist: [0, 359],
  width: [0, Number.POSITIVE_INFINITY],
  height: [0, Number.POSITIVE_INFINITY],
} as const;

function strictTransactionPointerSample(
  value: unknown,
  label: string,
  elapsed: false,
): pwActions.PointerGestureStart;
function strictTransactionPointerSample(
  value: unknown,
  label: string,
  elapsed: true,
): pwActions.PointerGesturePoint;
function strictTransactionPointerSample(
  value: unknown,
  label: string,
  elapsed: boolean,
): pwActions.PointerGestureStart | pwActions.PointerGesturePoint {
  const sample = asRecord(value, label);
  const required = elapsed ? ['x', 'y', 'elapsedMs'] : ['x', 'y'];
  const optional = Object.keys(POINTER_TELEMETRY_RANGES);
  if (
    !exactKeys(sample, required, optional)
    || typeof sample.x !== 'number'
    || typeof sample.y !== 'number'
    || !Number.isFinite(sample.x)
    || !Number.isFinite(sample.y)
    || elapsed && (
      typeof sample.elapsedMs !== 'number'
      || !Number.isFinite(sample.elapsedMs)
    )
  ) {
    throw new BrowserHostError(`${label}无效`, { code: 'invalid_transaction' });
  }
  const telemetry: Record<string, number> = {};
  const telemetryNames = Object.keys(
    POINTER_TELEMETRY_RANGES,
  ) as Array<keyof typeof POINTER_TELEMETRY_RANGES>;
  for (const name of telemetryNames) {
    if (!Object.prototype.hasOwnProperty.call(sample, name)) continue;
    const field = sample[name];
    const [minimum, maximum] = POINTER_TELEMETRY_RANGES[name];
    if (
      typeof field !== 'number'
      || !Number.isFinite(field)
      || field < minimum
      || field > maximum
    ) {
      throw new BrowserHostError(`${label}.${name}无效`, {
        code: 'invalid_transaction',
      });
    }
    telemetry[name] = field;
  }
  const point = {
    x: sample.x,
    y: sample.y,
    ...telemetry,
  };
  return elapsed
    ? {
        ...point,
        elapsedMs: sample.elapsedMs as number,
      } as pwActions.PointerGesturePoint
    : point as pwActions.PointerGestureStart;
}

function strictTransactionViewport(
  value: unknown,
  label: string,
): { width: number; height: number } {
  const viewport = asRecord(value, label);
  if (
    !exactKeys(viewport, ['width', 'height'])
    || typeof viewport.width !== 'number'
    || typeof viewport.height !== 'number'
    || !Number.isFinite(viewport.width)
    || !Number.isFinite(viewport.height)
  ) {
    throw new BrowserHostError(`${label}无效`, { code: 'invalid_transaction' });
  }
  return { width: viewport.width, height: viewport.height };
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

function parseExpectedDialogs(value: unknown): ExpectedDialog[] {
  if (!Array.isArray(value) || value.length < 1) {
    throw new BrowserHostError('回放 expected_dialogs 至少包含一项', {
      code: 'invalid_expected_dialogs',
    });
  }
  return value.map((raw, index) => {
    const allowedKeys = new Set([
      'type',
      'accept',
      'text',
      // Compiler-facing diagnostics/topology aliases. BrowserManager resolves
      // these to immutable runtime target ids before crossing the wire.
      'page',
      'label',
      'opener_page',
      'popup_ordinal',
      'target_id',
      'opener_target_id',
    ]);
    if (
      !raw
      || typeof raw !== 'object'
      || Array.isArray(raw)
      || Object.keys(raw as Record<string, unknown>).some((key) => !allowedKeys.has(key))
    ) {
      throw new BrowserHostError(`回放 expected_dialogs[${index}] 形状无效`, {
        code: 'invalid_expected_dialogs',
      });
    }
    const record = raw as Record<string, unknown>;
    const targetId = record.target_id === undefined
      ? ''
      : asString(record.target_id, `expected_dialogs[${index}].target_id`).trim();
    const openerTargetId = record.opener_target_id === undefined
      ? ''
      : asString(
        record.opener_target_id,
        `expected_dialogs[${index}].opener_target_id`,
      ).trim();
    const popupOrdinal = record.popup_ordinal === undefined
      ? null
      : (
        Number.isSafeInteger(record.popup_ordinal)
        && Number(record.popup_ordinal) >= 0
          ? Number(record.popup_ordinal)
          : NaN
      );
    if (
      !Object.prototype.hasOwnProperty.call(record, 'type')
      || !Object.prototype.hasOwnProperty.call(record, 'accept')
      || !Object.prototype.hasOwnProperty.call(record, 'text')
      || !['alert', 'confirm', 'prompt', 'beforeunload'].includes(String(record.type))
      || typeof record.accept !== 'boolean'
      || typeof record.text !== 'string'
      || record.text.includes('\0')
      || (record.type !== 'prompt' && record.text !== '')
      || (record.accept === false && record.text !== '')
      || Number.isNaN(popupOrdinal)
    ) {
      throw new BrowserHostError(`回放 expected_dialogs[${index}] 内容无效`, {
        code: 'invalid_expected_dialogs',
      });
    }
    return {
      type: record.type as RecorderDialogType,
      action: record.accept ? 'accept' : 'dismiss',
      text: record.text,
      targetId,
      openerTargetId,
      popupOrdinal,
    };
  });
}

interface AtomicAction {
  name: string;
  [key: string]: unknown;
}

function atomicModifiers(value: unknown, label: string): string[] {
  const allowed = ['Alt', 'Control', 'Meta', 'Shift'] as const;
  if (
    !Array.isArray(value)
    || value.some((item) => typeof item !== 'string' || !allowed.includes(
      item as typeof allowed[number],
    ))
    || new Set(value).size !== value.length
  ) {
    throw new BrowserHostError(`${label}无效`, { code: 'invalid_transaction' });
  }
  return allowed.filter((modifier) => value.includes(modifier));
}

function parseAtomicAction(value: unknown): AtomicAction {
  const action = asRecord(value, 'transaction action');
  const name = strictTransactionString(action.name, 'transaction action.name');
  const selector = (allowEmpty = false): string => strictTransactionString(
    action.selector,
    'transaction action.selector',
    allowEmpty,
  );
  if (name === 'click') {
    if (!exactKeys(action, [
      'name', 'selector', 'button', 'modifiers', 'clickCount', 'position',
    ])) {
      throw new BrowserHostError('transaction click 形状无效', {
        code: 'invalid_transaction',
      });
    }
    const button = action.button;
    if (
      button !== 'left'
      && button !== 'middle'
      && button !== 'right'
    ) {
      throw new BrowserHostError('transaction click.button 无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      selector: selector(),
      button,
      modifiers: atomicModifiers(action.modifiers, 'transaction click.modifiers'),
      clickCount: strictTransactionInteger(
        action.clickCount,
        'transaction click.clickCount',
      ),
      position: strictTransactionPoint(action.position, 'transaction click.position'),
    };
  }
  if (name === 'hover') {
    if (!exactKeys(action, ['name', 'selector', 'position'])) {
      throw new BrowserHostError('transaction hover 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      selector: selector(),
      position: strictTransactionPoint(action.position, 'transaction hover.position'),
    };
  }
  if (name === 'fill') {
    if (!exactKeys(action, ['name', 'selector', 'text'])) {
      throw new BrowserHostError('transaction fill 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      selector: selector(),
      text: strictTransactionString(action.text, 'transaction fill.text', true),
    };
  }
  if (name === 'check' || name === 'uncheck') {
    if (!exactKeys(action, ['name', 'selector'])) {
      throw new BrowserHostError('transaction check 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return { name, selector: selector() };
  }
  if (name === 'handle_overlay') {
    if (!exactKeys(action, ['name', 'selector'])) {
      throw new BrowserHostError('transaction handle_overlay 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return { name, selector: selector() };
  }
  if (name === 'assert_state') {
    if (!exactKeys(action, ['name', 'selector', 'state'])) {
      throw new BrowserHostError('transaction assert_state 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      selector: selector(),
      state: strictTransactionString(action.state, 'transaction assert_state.state'),
    };
  }
  if (name === 'select') {
    if (
      !exactKeys(action, ['name', 'selector', 'options'])
      || !Array.isArray(action.options)
      || action.options.some((option) => typeof option !== 'string')
    ) {
      throw new BrowserHostError('transaction select 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return { name, selector: selector(), options: [...action.options] };
  }
  if (name === 'press') {
    if (!exactKeys(action, ['name', 'selector', 'key', 'modifiers'])) {
      throw new BrowserHostError('transaction press 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      selector: selector(true),
      key: strictTransactionString(action.key, 'transaction press.key'),
      modifiers: atomicModifiers(action.modifiers, 'transaction press.modifiers'),
    };
  }
  if (name === 'setInputFiles') {
    if (
      !exactKeys(action, ['name', 'selector', 'files'])
      || !Array.isArray(action.files)
      || action.files.some((file) => typeof file !== 'string' || !file)
    ) {
      throw new BrowserHostError('transaction setInputFiles 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return { name, selector: selector(), files: [...action.files] };
  }
  if (name === 'x-crew-drop') {
    if (
      !exactKeys(action, ['name', 'selector', 'files', 'data'])
      || !Array.isArray(action.files)
      || action.files.some((file) => typeof file !== 'string' || !file)
    ) {
      throw new BrowserHostError('transaction x-crew-drop 形状无效', {
        code: 'invalid_transaction',
      });
    }
    const data = asRecord(action.data, 'transaction x-crew-drop.data');
    if (Object.values(data).some((payload) => typeof payload !== 'string')) {
      throw new BrowserHostError('transaction x-crew-drop.data 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      selector: selector(),
      files: [...action.files],
      data: Object.fromEntries(
        Object.entries(data).map(([mime, payload]) => [mime, String(payload)]),
      ),
    };
  }
  if (name === 'navigate') {
    if (!exactKeys(action, ['name', 'url'])) {
      throw new BrowserHostError(`transaction ${name} 形状无效`, {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      url: strictTransactionString(action.url, `transaction ${name}.url`),
    };
  }
  if (name === 'openPage') {
    if (!exactKeys(action, ['name', 'url'], ['viewport'])) {
      throw new BrowserHostError('transaction openPage 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      url: strictTransactionString(action.url, 'transaction openPage.url'),
      ...(action.viewport === undefined
        ? {}
        : {
            viewport: strictTransactionViewport(
              action.viewport,
              'transaction openPage.viewport',
            ),
          }),
    };
  }
  if (
    name === 'closePage'
    || name === 'x-crew-activatePage'
    || name === 'x-crew-snapshot'
  ) {
    if (!exactKeys(action, ['name'])) {
      throw new BrowserHostError(`transaction ${name} 形状无效`, {
        code: 'invalid_transaction',
      });
    }
    return { name };
  }
  if (name === 'x-crew-navigate') {
    if (!exactKeys(action, ['name', 'operation', 'url'])) {
      throw new BrowserHostError('transaction x-crew-navigate 形状无效', {
        code: 'invalid_transaction',
      });
    }
    const operation = action.operation;
    const url = strictTransactionString(
      action.url,
      'transaction x-crew-navigate.url',
      true,
    );
    if (
      operation !== 'goto'
      && operation !== 'back'
      && operation !== 'forward'
      && operation !== 'reload'
      || operation === 'goto' && !url
      || operation !== 'goto' && Boolean(url)
    ) {
      throw new BrowserHostError('transaction x-crew-navigate 内容无效', {
        code: 'invalid_transaction',
      });
    }
    return { name, operation, url };
  }
  if (name === 'x-crew-resize') {
    if (
      !exactKeys(action, ['name', 'width', 'height'])
      || typeof action.width !== 'number'
      || typeof action.height !== 'number'
      || !Number.isFinite(action.width)
      || !Number.isFinite(action.height)
    ) {
      throw new BrowserHostError('transaction x-crew-resize 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      width: action.width,
      height: action.height,
    };
  }
  if (name === 'x-crew-drag') {
    if (!exactKeys(action, [
      'name',
      'sourceSelector',
      'targetSelector',
      'sourcePosition',
      'targetPosition',
    ])) {
      throw new BrowserHostError('transaction x-crew-drag 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      sourceSelector: strictTransactionString(
        action.sourceSelector,
        'transaction drag.sourceSelector',
      ),
      targetSelector: strictTransactionString(
        action.targetSelector,
        'transaction drag.targetSelector',
      ),
      sourcePosition: strictTransactionPoint(
        action.sourcePosition,
        'transaction drag.sourcePosition',
      ),
      targetPosition: strictTransactionPoint(
        action.targetPosition,
        'transaction drag.targetPosition',
      ),
    };
  }
  if (name === 'x-crew-pointerGesture') {
    if (!exactKeys(action, [
      'name', 'selector', 'button', 'modifiers', 'start', 'points',
    ], ['pointerType'])) {
      throw new BrowserHostError('transaction pointerGesture 形状无效', {
        code: 'invalid_transaction',
      });
    }
    if (
      action.button !== 'left'
      && action.button !== 'middle'
      && action.button !== 'right'
    ) {
      throw new BrowserHostError('transaction pointerGesture.button 无效', {
        code: 'invalid_transaction',
      });
    }
    const pointerType = action.pointerType ?? 'mouse';
    if (
      pointerType !== 'mouse'
      && pointerType !== 'pen'
      && pointerType !== 'touch'
      || pointerType === 'touch' && action.button !== 'left'
    ) {
      throw new BrowserHostError('transaction pointerGesture.pointerType 无效', {
        code: 'invalid_transaction',
      });
    }
    const start = strictTransactionPointerSample(
      action.start,
      'transaction pointerGesture.start',
      false,
    );
    if (!Array.isArray(action.points) || action.points.length === 0) {
      throw new BrowserHostError('transaction pointerGesture 内容无效', {
        code: 'invalid_transaction',
      });
    }
    let previousElapsedMs = 0;
    const points = action.points.map((rawPoint, index) => {
      let point: pwActions.PointerGesturePoint;
      try {
        point = strictTransactionPointerSample(
          rawPoint,
          `transaction pointerGesture.points[${index}]`,
          true,
        );
      } catch {
        throw new BrowserHostError(
          `transaction pointerGesture.points[${index}] 无效`,
          { code: 'invalid_transaction' },
        );
      }
      if (point.elapsedMs < previousElapsedMs) {
        throw new BrowserHostError(
          `transaction pointerGesture.points[${index}] 无效`,
          { code: 'invalid_transaction' },
        );
      }
      previousElapsedMs = point.elapsedMs;
      return point;
    });
    return {
      name,
      selector: selector(),
      button: action.button,
      modifiers: atomicModifiers(
        action.modifiers,
        'transaction pointerGesture.modifiers',
      ),
      pointerType,
      start,
      points,
    };
  }
  if (name === 'x-crew-scroll') {
    if (!exactKeys(action, ['name', 'selector', 'deltaX', 'deltaY'])) {
      throw new BrowserHostError('transaction x-crew-scroll 形状无效', {
        code: 'invalid_transaction',
      });
    }
    if (
      typeof action.deltaX !== 'number'
      || typeof action.deltaY !== 'number'
      || !Number.isSafeInteger(action.deltaX)
      || !Number.isSafeInteger(action.deltaY)
      || action.deltaX === 0 && action.deltaY === 0
    ) {
      throw new BrowserHostError('transaction x-crew-scroll delta 无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      selector: selector(true),
      deltaX: action.deltaX,
      deltaY: action.deltaY,
    };
  }
  if (name === 'x-crew-waitPopup') {
    if (!exactKeys(action, [
      'name', 'popupPageGuid', 'popupIndex', 'activate', 'disposition',
    ])) {
      throw new BrowserHostError('transaction waitPopup 形状无效', {
        code: 'invalid_transaction',
      });
    }
    if (typeof action.activate !== 'boolean') {
      throw new BrowserHostError('transaction waitPopup.activate 无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      popupPageGuid: strictPageGuid(
        action.popupPageGuid,
        'transaction waitPopup.popupPageGuid',
      ),
      popupIndex: strictTransactionInteger(
        action.popupIndex,
        'transaction waitPopup.popupIndex',
      ),
      activate: action.activate,
      disposition: strictTransactionString(
        action.disposition,
        'transaction waitPopup.disposition',
      ),
    };
  }
  if (name === 'x-crew-waitNavigation') {
    if (!exactKeys(action, ['name', 'url'])) {
      throw new BrowserHostError('transaction waitNavigation 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      url: strictTransactionString(action.url, 'transaction waitNavigation.url'),
    };
  }
  if (name === 'x-crew-waitDownload') {
    if (!exactKeys(action, [
      'name', 'alias', 'ordinal', 'suggestedFilename',
    ])) {
      throw new BrowserHostError('transaction waitDownload 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      alias: strictTransactionString(action.alias, 'transaction waitDownload.alias'),
      ordinal: strictTransactionInteger(
        action.ordinal,
        'transaction waitDownload.ordinal',
      ),
      suggestedFilename: strictTransactionString(
        action.suggestedFilename,
        'transaction waitDownload.suggestedFilename',
        true,
      ),
    };
  }
  if (name === 'x-crew-waitDialog') {
    if (!exactKeys(action, [
      'name', 'alias', 'type', 'accept', 'text',
    ])) {
      throw new BrowserHostError('transaction waitDialog 形状无效', {
        code: 'invalid_transaction',
      });
    }
    if (
      !['alert', 'confirm', 'prompt', 'beforeunload'].includes(String(action.type))
      || typeof action.accept !== 'boolean'
      || typeof action.text !== 'string'
      || action.type !== 'prompt' && action.text
      || action.accept === false && action.text
    ) {
      throw new BrowserHostError('transaction waitDialog 内容无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      alias: strictTransactionString(action.alias, 'transaction waitDialog.alias'),
      type: action.type,
      accept: action.accept,
      text: action.text,
    };
  }
  if (name === 'x-crew-waitPageClosed') {
    if (!exactKeys(action, ['name', 'reason'])) {
      throw new BrowserHostError('transaction waitPageClosed 形状无效', {
        code: 'invalid_transaction',
      });
    }
    return {
      name,
      reason: strictTransactionString(
        action.reason,
        'transaction waitPageClosed.reason',
        true,
      ),
    };
  }
  throw new BrowserHostError(`不支持的 transaction action：${name}`, {
    code: 'unsupported_transaction_action',
  });
}

function parseAtomicEffects(value: unknown): AtomicEffect[] {
  if (!Array.isArray(value)) {
    throw new BrowserHostError('expectedEffects 必须是数组', {
      code: 'invalid_transaction',
    });
  }
  return value.map((raw, index): AtomicEffect => {
    const effect = asRecord(raw, `expectedEffects[${index}]`);
    const kind = effect.kind;
    if (kind === 'navigation' && exactKeys(effect, ['kind', 'page', 'url'])) {
      return {
        kind,
        page: strictPageGuid(effect.page, `expectedEffects[${index}].page`),
        url: strictTransactionString(effect.url, `expectedEffects[${index}].url`),
      };
    }
    if (
      kind === 'popup'
      && exactKeys(effect, [
        'kind',
        'page',
        'opener_page',
        'popup_index',
        'activate',
        'disposition',
      ])
      && typeof effect.activate === 'boolean'
    ) {
      return {
        kind,
        page: strictPageGuid(effect.page, `expectedEffects[${index}].page`),
        opener_page: strictPageGuid(
          effect.opener_page,
          `expectedEffects[${index}].opener_page`,
        ),
        popup_index: strictTransactionInteger(
          effect.popup_index,
          `expectedEffects[${index}].popup_index`,
        ),
        activate: effect.activate,
        disposition: strictTransactionString(
          effect.disposition,
          `expectedEffects[${index}].disposition`,
        ),
      };
    }
    if (
      kind === 'download'
      && exactKeys(effect, [
        'kind', 'page', 'alias', 'ordinal', 'suggested_filename',
      ])
    ) {
      return {
        kind,
        page: strictPageGuid(effect.page, `expectedEffects[${index}].page`),
        alias: strictTransactionString(
          effect.alias,
          `expectedEffects[${index}].alias`,
        ),
        ordinal: strictTransactionInteger(
          effect.ordinal,
          `expectedEffects[${index}].ordinal`,
        ),
        suggested_filename: strictTransactionString(
          effect.suggested_filename,
          `expectedEffects[${index}].suggested_filename`,
          true,
        ),
      };
    }
    if (
      kind === 'dialog'
      && exactKeys(effect, [
        'kind', 'page', 'alias', 'type', 'accept', 'text',
      ])
      && ['alert', 'confirm', 'prompt', 'beforeunload'].includes(String(effect.type))
      && typeof effect.accept === 'boolean'
      && typeof effect.text === 'string'
      && !(effect.type !== 'prompt' && effect.text)
      && !(effect.accept === false && effect.text)
    ) {
      return {
        kind,
        page: strictPageGuid(effect.page, `expectedEffects[${index}].page`),
        alias: strictTransactionString(
          effect.alias,
          `expectedEffects[${index}].alias`,
        ),
        type: effect.type as RecorderDialogType,
        accept: effect.accept,
        text: effect.text,
      };
    }
    if (
      kind === 'page_closed'
      && exactKeys(effect, ['kind', 'page', 'reason'])
    ) {
      return {
        kind,
        page: strictPageGuid(effect.page, `expectedEffects[${index}].page`),
        reason: strictTransactionString(
          effect.reason,
          `expectedEffects[${index}].reason`,
          true,
        ),
      };
    }
    throw new BrowserHostError(`expectedEffects[${index}] 形状无效`, {
      code: 'invalid_transaction',
    });
  });
}

function expectedDialogTimeoutMs(value: unknown, deadlineValue?: unknown): number {
  let requested = ACTION_TIMEOUT_MS;
  if (value !== undefined) {
    if (
      typeof value !== 'number'
      || !Number.isFinite(value)
      || value <= 0
      || value > Number.MAX_SAFE_INTEGER
    ) {
      throw new BrowserHostError('command_timeout_ms 必须是正有限数', {
        code: 'invalid_expected_dialogs',
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

/**
 * Capture the first FileChooser emitted after `arm()`.
 *
 * The timeout intentionally starts only after the click resolves. Starting it
 * before Playwright's actionability wait would drop a legitimate chooser when
 * a transiently moving button takes longer than the grace period to become
 * clickable.
 */
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

  /**
   * Called by the Desktop transport when a recording frame could not be put on
   * the authenticated loopback socket.  EventEmitter listeners are synchronous,
   * so the originating tab/ledger still exists while this is called.
   */
  markRecordingIncomplete(targetId: string, recordingId = ''): void {
    const entry = this.tabsByTarget.get(String(targetId || ''));
    const ledger = entry?.tab.recording?.ledger;
    if (!ledger || (recordingId && ledger.recordingId !== recordingId)) return;
    ledger.incomplete = true;
    ledger.dropped += 1;
  }

  private markRecordingStateIncomplete(state: RecordingState): void {
    state.ledger.incomplete = true;
    state.ledger.dropped += 1;
  }

  async handleRpc(requestValue: unknown): Promise<unknown> {
    const request = asRecord(requestValue, 'RPC 请求') as unknown as BrowserRpcRequest;
    const key = runtimeKey(request.runtime_key);
    const method = asString(request.method, 'RPC method', 80).trim();
    const params = asRecord(request.params ?? {}, 'RPC params');
    if (!method) throw new BrowserHostError('RPC method 不能为空', { code: 'invalid_request' });

    this.assertUsable();
    if (method === 'capabilities') {
      return {
        recordingEventSchemas: [10, 11],
        replayArtifactSchemas: [
          'crew.browser.replay.v2',
          'crew.browser.replay.v3',
        ],
        atomicReplayEffects: true,
      };
    }
    if (method === 'deny_downloads') return this.denyDownloads(key, params);
    if (method === 'close_owner') return this.closeOwner(key, params);
    if (method === 'clear_owner_data') return this.clearOwnerData(key, params);

    return this.enqueue(key, async () => {
      this.assertUsable();
      switch (method) {
        case 'execute':
          return this.execute(key, params);
        case 'execute_transaction':
          return this.executeTransaction(key, params);
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
        case 'set_recording':
          return this.setRecording(key, params);
        case 'doctor':
          return {
            ok: true,
            runtime: 'electron',
            engine: 'WebContentsView',
            recordingEventSchemas: [10, 11],
            replayArtifactSchemas: [
              'crew.browser.replay.v2',
              'crew.browser.replay.v3',
            ],
            atomicReplayEffects: true,
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
      this.recordViewportResize(
        popupPanel.tab,
        popupPanel.tab.recording,
        bounds.width,
        bounds.height,
      );
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
    if (mode === 'human') {
      this.recordViewportResize(
        tab,
        tab.recording,
        bounds.width,
        bounds.height,
      );
    }
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
      expectedDialogRuns: new Map(),
      pendingModalActions: new Map(),
      modalWaiters: new Map(),
      atomicTransactions: new Map(),
      atomicReplayEpochs: new Map(),
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
          this.closeTab(owner, tab, false);
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

  private atomicEffectEqual(left: AtomicEffect, right: AtomicEffect): boolean {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  private appendAtomicJournal(
    owner: BrowserOwner,
    epoch: AtomicReplayEpoch,
    event: AtomicJournalEvent,
  ): AtomicJournalEntry {
    const entry: AtomicJournalEntry = {
      sequence: ++epoch.nextEventSequence,
      event,
      consumed: false,
    };
    epoch.journal.push(entry);
    const run = owner.atomicTransactions.get(epoch.sessionHash);
    if (run && run.epoch === epoch) this.consumeAtomicJournal(owner, run);
    // Keep a bounded tail. Consumed entries have no replay value; unconsumed
    // lifecycle events are retained even across many short action RPCs.
    if (epoch.journal.length > 2_048) {
      const removable = epoch.journal.length - 2_048;
      let removed = 0;
      epoch.journal = epoch.journal.filter((candidate) => {
        if (removed < removable && candidate.consumed) {
          removed += 1;
          return false;
        }
        return true;
      });
    }
    return entry;
  }

  private atomicEpochOwnsTab(
    epoch: AtomicReplayEpoch,
    tab: BrowserTab,
  ): boolean {
    return epoch.pageGuidByTargetId.has(tab.targetId)
      || Boolean(
        tab.openerTargetId
        && (
          epoch.pageGuidByTargetId.has(tab.openerTargetId)
          || epoch.journal.some((entry) => (
            entry.event.kind === 'popup'
            && entry.event.targetId === tab.openerTargetId
          ))
        )
      )
      || epoch.journal.some((entry) => (
        entry.event.kind === 'popup'
        && entry.event.targetId === tab.targetId
      ));
  }

  private atomicJournalCandidate(
    run: AtomicTransactionRun,
    entry: AtomicJournalEntry,
    expected: AtomicEffect,
  ): AtomicEffect | null {
    const observed = entry.event;
    if (expected.kind !== observed.kind) return null;
    if (observed.kind === 'popup' && expected.kind === 'popup') {
      const openerPage = run.pageGuidByTargetId.get(observed.openerTargetId);
      if (!openerPage) return null;
      const base = run.popupOrdinalBases.get(observed.openerTargetId) ?? 0;
      return {
        kind: 'popup',
        page: expected.page,
        opener_page: openerPage,
        popup_index: observed.popupOrdinal - base,
        activate: observed.activate,
        disposition: observed.disposition,
      };
    }
    const targetId = observed.targetId;
    const page = run.pageGuidByTargetId.get(targetId);
    if (!page) return null;
    if (observed.kind === 'navigation' && expected.kind === 'navigation') {
      return { kind: 'navigation', page, url: observed.url };
    }
    if (observed.kind === 'download' && expected.kind === 'download') {
      return {
        kind: 'download',
        page,
        alias: expected.alias,
        ordinal: observed.ordinal,
        suggested_filename: observed.suggestedFilename,
      };
    }
    if (observed.kind === 'dialog' && expected.kind === 'dialog') {
      return {
        kind: 'dialog',
        page,
        alias: expected.alias,
        type: observed.type,
        accept: observed.accept,
        text: observed.text,
      };
    }
    if (observed.kind === 'page_closed' && expected.kind === 'page_closed') {
      return { kind: 'page_closed', page, reason: observed.reason };
    }
    return null;
  }

  private consumeAtomicJournal(owner: BrowserOwner, run: AtomicTransactionRun): void {
    while (!run.settled) {
      const expected = run.expectedEffects[run.effectCursor];
      if (!expected) {
        this.settleAtomicTransaction(owner, run);
        return;
      }
      let matched: AtomicJournalEntry | undefined;
      for (const entry of run.epoch.journal) {
        if (
          entry.consumed
          || (
            entry.sequence <= (
              run.allowHistoricalEffects
                ? run.historicalFloor
                : run.armSequence
            )
          )
        ) continue;
        const candidate = this.atomicJournalCandidate(run, entry, expected);
        if (!candidate) continue;
        if (this.atomicEffectEqual(expected, candidate)) {
          matched = entry;
          break;
        }
        // Redirect hops are observations, not terminal failures. Keep waiting
        // for the exact final URL (POST -> 302 -> destination is common).
        if (expected.kind === 'navigation') continue;
        const appearsLater = run.expectedEffects
          .slice(run.effectCursor + 1)
          .some((later) => {
            const laterCandidate = this.atomicJournalCandidate(run, entry, later);
            return Boolean(
              laterCandidate
              && this.atomicEffectEqual(later, laterCandidate),
            );
          });
        if (appearsLater) {
          this.settleAtomicTransaction(
            owner,
            run,
            new BrowserHostError('原子事务 effect 顺序与录制不一致', {
              code: 'transaction_effect_mismatch',
              partial: run.effectCursor > 0,
            }),
          );
          return;
        }
      }
      if (!matched) return;
      matched.consumed = true;
      if (matched.event.kind === 'popup' && expected.kind === 'popup') {
        run.pageGuidByTargetId.set(matched.event.targetId, expected.page);
        run.targetIdByPageGuid.set(expected.page, matched.event.targetId);
        run.popupOrdinalBases.set(
          matched.event.targetId,
          owner.popupOrdinals.get(
            `${run.sessionHash}\u0000${matched.event.targetId}`,
          ) ?? 0,
        );
      } else if (
        matched.event.kind === 'download'
        && expected.kind === 'download'
      ) {
        const downloadEvent = matched.event;
        const responseDownload: AtomicDownloadResult = {
          alias: expected.alias,
          pageGuid: expected.page,
          ...downloadEvent.result,
        };
        let completion = downloadEvent.completion.then(() => {
          responseDownload.state = downloadEvent.result.state;
          responseDownload.receivedBytes = downloadEvent.result.receivedBytes;
        });
        if (
          run.downloadDir
          && path.dirname(downloadEvent.result.path) !== run.downloadDir
        ) {
          const original = downloadEvent.result.path;
          const relocated = path.join(
            run.downloadDir,
            `${run.epoch.epochId}-${downloadEvent.ordinal}-${randomUUID()}-${
              path.basename(original)
            }`,
          );
          completion = completion.then(async () => {
            try {
              await rename(original, relocated);
            } catch {
              await copyFile(original, relocated);
              await unlink(original).catch(() => undefined);
            }
            downloadEvent.result.path = relocated;
            responseDownload.path = relocated;
          });
        }
        run.downloads.push(responseDownload);
        run.downloadCompletions.push(completion);
      } else if (
        matched.event.kind === 'page_closed'
        && expected.kind === 'page_closed'
      ) {
        run.closedPageGuids.add(expected.page);
        run.epoch.closedPageGuids.add(expected.page);
      }
      this.advanceAtomicEffect(owner, run, expected);
    }
  }

  private settleAtomicTransaction(
    owner: BrowserOwner,
    run: AtomicTransactionRun,
    error?: BrowserHostError,
  ): void {
    if (run.settled) return;
    run.settled = true;
    clearTimeout(run.timer);
    if (error) run.reject(error);
    else run.resolve();
    // Keep the run installed until executeTransaction's finally block. An
    // already-started download may still append terminal bytes/state there.
    if (error && owner.atomicTransactions.get(run.sessionHash) === run) {
      owner.atomicTransactions.delete(run.sessionHash);
    }
  }

  private advanceAtomicEffect(
    owner: BrowserOwner,
    run: AtomicTransactionRun,
    candidate: AtomicEffect,
  ): boolean {
    if (run.settled) return false;
    const expected = run.expectedEffects[run.effectCursor];
    if (!expected) return false;
    if (this.atomicEffectEqual(expected, candidate)) {
      if (run.effectCursor >= run.reportedEffectStart) {
        // Return the validated wire object, not a runtime-enriched variant.
        run.matchedEffects.push(expected);
      }
      run.effectCursor += 1;
      if (run.effectCursor === run.expectedEffects.length) {
        this.settleAtomicTransaction(owner, run);
      }
      return true;
    }
    const sameIdentity = expected.kind !== 'navigation'
      && expected.kind === candidate.kind && (
      expected.kind === 'popup' && candidate.kind === 'popup'
        ? expected.opener_page === candidate.opener_page
          && expected.popup_index === candidate.popup_index
        : expected.page === candidate.page
      );
    const appearsLater = run.expectedEffects
      .slice(run.effectCursor + 1)
      .some((effect) => this.atomicEffectEqual(effect, candidate));
    if (sameIdentity || appearsLater) {
      this.settleAtomicTransaction(
        owner,
        run,
        new BrowserHostError(
          `原子事务 effect 不一致：期望 ${expected.kind}，实际 ${candidate.kind}`,
          {
            code: 'transaction_effect_mismatch',
            partial: run.effectCursor > 0,
          },
        ),
      );
    }
    // Unrelated ambient lifecycle noise is ignored; page/topology-specific
    // divergence above remains strict.
    return false;
  }

  private observeAtomicPopup(
    owner: BrowserOwner,
    opener: BrowserTab,
    popup: BrowserTab,
    disposition: string,
    activate: boolean,
  ): void {
    const epoch = owner.atomicReplayEpochs.get(opener.sessionHash);
    if (!epoch || !this.atomicEpochOwnsTab(epoch, opener)) return;
    this.appendAtomicJournal(owner, epoch, {
      kind: 'popup',
      openerTargetId: opener.targetId,
      targetId: popup.targetId,
      popupOrdinal: popup.popupOrdinal,
      activate,
      disposition,
    });
  }

  private observeAtomicNavigation(owner: BrowserOwner, tab: BrowserTab): void {
    const epoch = owner.atomicReplayEpochs.get(tab.sessionHash);
    if (!epoch || !this.atomicEpochOwnsTab(epoch, tab)) return;
    this.appendAtomicJournal(owner, epoch, {
      kind: 'navigation',
      targetId: tab.targetId,
      url: this.tabUrl(tab),
    });
  }

  private observeAtomicDialogClosed(
    owner: BrowserOwner,
    tab: BrowserTab,
    dialog: DialogState,
    params: Record<string, unknown>,
  ): void {
    const epoch = owner.atomicReplayEpochs.get(tab.sessionHash);
    if (!epoch || !this.atomicEpochOwnsTab(epoch, tab)) return;
    this.appendAtomicJournal(owner, epoch, {
      kind: 'dialog',
      targetId: tab.targetId,
      type: dialog.type as RecorderDialogType,
      accept: params.result === true,
      text: (
        params.result === true
        && dialog.type === 'prompt'
        && typeof params.userInput === 'string'
      ) ? params.userInput : '',
    });
  }

  private observeAtomicPageClosed(
    owner: BrowserOwner,
    tab: BrowserTab,
    reason: string,
  ): void {
    const epoch = owner.atomicReplayEpochs.get(tab.sessionHash);
    const page = epoch?.pageGuidByTargetId.get(tab.targetId);
    if (!epoch || !this.atomicEpochOwnsTab(epoch, tab)) return;
    if (page && epoch.closedPageGuids.has(page)) return;
    if (page) epoch.closedPageGuids.add(page);
    this.appendAtomicJournal(owner, epoch, {
      kind: 'page_closed',
      targetId: tab.targetId,
      reason,
    });
  }

  private observeAtomicDownload(
    owner: BrowserOwner,
    tab: BrowserTab,
    item: DownloadItem,
  ): boolean {
    const epoch = owner.atomicReplayEpochs.get(tab.sessionHash);
    if (
      !epoch
      || !this.atomicEpochOwnsTab(epoch, tab)
      || !epoch.downloadDir
    ) return false;
    const ordinal = (epoch.downloadOrdinals.get(tab.targetId) ?? 0) + 1;
    epoch.downloadOrdinals.set(tab.targetId, ordinal);
    let suggestedFilename = '';
    try {
      suggestedFilename = item.getFilename();
    } catch {
      suggestedFilename = '';
    }
    const basename = path.basename(suggestedFilename) || 'download';
    const target = path.join(
      epoch.downloadDir,
      `${epoch.epochId}-${ordinal}-${randomUUID()}-${basename}`,
    );
    try {
      item.setSavePath(target);
    } catch {
      return false;
    }
    const result: Omit<AtomicDownloadResult, 'alias' | 'pageGuid'> = {
      ordinal,
      suggestedFilename,
      path: target,
      state: 'progressing',
      receivedBytes: 0,
    };
    const completion = new Promise<void>((resolve) => {
      item.once('done', (_event, state) => {
        result.state = state;
        try {
          result.receivedBytes = item.getReceivedBytes();
        } catch {
          result.receivedBytes = 0;
        }
        resolve();
      });
    });
    this.appendAtomicJournal(owner, epoch, {
      kind: 'download',
      targetId: tab.targetId,
      ordinal,
      suggestedFilename,
      result,
      completion,
    });
    return true;
  }

  private atomicExpectedDialogs(
    run: AtomicTransactionRun,
  ): ExpectedDialog[] {
    return run.expectedEffects
      .slice(run.effectCursor)
      .filter((effect): effect is Extract<AtomicEffect, { kind: 'dialog' }> => (
        effect.kind === 'dialog'
      ))
      .map((effect) => {
        const directTarget = run.targetIdByPageGuid.get(effect.page) ?? '';
        if (directTarget) {
          return {
            type: effect.type,
            action: effect.accept ? 'accept' : 'dismiss',
            text: effect.text,
            targetId: directTarget,
            openerTargetId: '',
            popupOrdinal: null,
          };
        }
        const popup = run.expectedEffects.find(
          (candidate): candidate is Extract<AtomicEffect, { kind: 'popup' }> => (
            candidate.kind === 'popup' && candidate.page === effect.page
          ),
        );
        const openerTargetId = popup
          ? run.targetIdByPageGuid.get(popup.opener_page) ?? ''
          : '';
        if (!popup || !openerTargetId) {
          throw new BrowserHostError('dialog effect 引用了无法绑定的页面', {
            code: 'invalid_transaction',
          });
        }
        return {
          type: effect.type,
          action: effect.accept ? 'accept' : 'dismiss',
          text: effect.text,
          targetId: '',
          openerTargetId,
          popupOrdinal: (
            run.popupOrdinalBases.get(openerTargetId) ?? 0
          ) + popup.popup_index,
        };
      });
  }

  private async withAtomicSelectorRef<T>(
    ctx: ActionContext,
    selector: string,
    operation: (ref: string) => Promise<T>,
  ): Promise<T> {
    const ref = `@transaction-${randomUUID()}`;
    try {
      await pwActions.locateBySelector(ctx, ref, selector, ctx.hash);
      return await operation(ref);
    } finally {
      ctx.refs.delete(ref);
    }
  }

  private async dispatchAtomicAction(
    owner: BrowserOwner,
    run: AtomicTransactionRun,
    sourcePageGuid: string,
    sourceTab: BrowserTab | null,
    action: AtomicAction,
  ): Promise<{ tab: BrowserTab | null; snapshot?: string }> {
    let tab = sourceTab;
    if (action.name === 'openPage') {
      if (!tab || run.targetIdByPageGuid.size > 0 && !run.targetIdByPageGuid.has(sourcePageGuid)) {
        const anchorTarget = run.targetIdByPageGuid.values().next().value as string | undefined;
        const anchor = anchorTarget ? this.targetTab(owner, anchorTarget) : undefined;
        if (!anchor) {
          throw new BrowserHostError('openPage 缺少会话锚点', {
            code: 'invalid_transaction_source',
          });
        }
        tab = this.createTab(
          owner,
          `s${anchor.sessionHash}-${owner.tabCounter + 1}`,
          anchor.sessionHash,
          '',
          'ai',
        );
        await this.initializeNewTab(tab, run.deadlineAt);
      }
      run.pageGuidByTargetId.set(tab.targetId, sourcePageGuid);
      run.targetIdByPageGuid.set(sourcePageGuid, tab.targetId);
      run.popupOrdinalBases.set(
        tab.targetId,
        owner.popupOrdinals.get(`${tab.sessionHash}\u0000${tab.targetId}`) ?? 0,
      );
      owner.activeTabId = tab.tabId;
      if (action.viewport) {
        const viewport = action.viewport as { width: number; height: number };
        tab.visualEpoch = null;
        await pwActions.resize(
          await this.actionContext(
            tab,
            remainingCommandTimeoutMs(run.deadlineAt),
          ),
          viewport.width,
          viewport.height,
        );
      }
      await this.navigate(
        owner,
        tab,
        String(action.url),
        run.deadlineAt,
      );
      return { tab };
    }
    if (action.name.startsWith('x-crew-wait')) {
      // A page_closed wait is intentionally executable from an immutable epoch
      // tombstone after Electron has already destroyed the WebContents.
      return { tab };
    }
    if (!tab) {
      throw new BrowserHostError('transaction source 页面未绑定', {
        code: 'invalid_transaction_source',
      });
    }
    if (action.name === 'closePage') {
      this.closeTab(owner, tab);
      return { tab };
    }
    if (action.name === 'x-crew-activatePage') {
      owner.activeTabId = tab.tabId;
      return { tab };
    }
    if (action.name === 'x-crew-snapshot') {
      const captured = await this.snapshot(
        tab,
        true,
        true,
        remainingCommandTimeoutMs(run.deadlineAt),
      );
      return { tab, snapshot: String(captured.snapshot ?? '') };
    }
    if (action.name === 'navigate') {
      await this.navigate(owner, tab, String(action.url), run.deadlineAt);
      return { tab };
    }
    const ctx = await this.actionContext(
      tab,
      remainingCommandTimeoutMs(run.deadlineAt),
    );
    if (action.name === 'x-crew-resize') {
      tab.visualEpoch = null;
      await pwActions.resize(
        ctx,
        Number(action.width),
        Number(action.height),
      );
      return { tab };
    }
    if (action.name === 'x-crew-navigate') {
      const operation = String(action.operation);
      if (operation === 'goto') {
        await this.navigate(owner, tab, String(action.url), run.deadlineAt);
      } else if (operation === 'back') {
        await pwActions.goBack(ctx);
      } else if (operation === 'forward') {
        await pwActions.goForward(ctx);
      } else {
        await pwActions.reload(ctx);
      }
      this.clearDocumentState(tab);
      return { tab };
    }
    if (action.name === 'click') {
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.click(ctx, ref, {
          button: action.button as pwActions.ClickButton,
          clickCount: Number(action.clickCount),
          modifiers: action.modifiers as NonNullable<ClickOptions['modifiers']>,
          ...(action.position
            ? { position: action.position as { x: number; y: number } }
            : {}),
        });
      });
      return { tab };
    }
    if (action.name === 'hover') {
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.hover(
          ctx,
          ref,
          action.position as { x: number; y: number } | null ?? undefined,
        );
      });
      return { tab };
    }
    if (action.name === 'fill') {
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.fill(ctx, ref, String(action.text), { submit: false });
      });
      return { tab };
    }
    if (action.name === 'check' || action.name === 'uncheck') {
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.setChecked(ctx, ref, action.name === 'check');
      });
      return { tab };
    }
    if (action.name === 'handle_overlay') {
      // 直接用 selector，不走 withAtomicSelectorRef：处理器要跨越整场回放，
      // 解析成一个当代的 ref 反而会让它在下一次快照后失效。
      await pwActions.registerOverlayHandler(ctx, String(action.selector));
      return { tab };
    }
    if (action.name === 'assert_state') {
      // 断言不改变页面，所以它**不该**参与 withActionCompletion 那套
      // "等导航/网络收束"的语义——那是给会产生副作用的动作用的。
      // 断言只回答一个问题：当前页面是不是预期的样子。
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.assertState(ctx, ref, String(action.state));
      });
      return { tab };
    }
    if (action.name === 'select') {
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.selectOption(ctx, ref, action.options as string[]);
      });
      return { tab };
    }
    if (action.name === 'press') {
      const modifiers = action.modifiers as string[];
      const key = [...modifiers, String(action.key)].join('+');
      if (String(action.selector)) {
        await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
          await pwActions.press(ctx, key, ref);
        });
      } else {
        await pwActions.press(ctx, key, undefined);
      }
      return { tab };
    }
    if (action.name === 'setInputFiles') {
      const files = await this.approvedUploadFiles(
        this.ownerOfTab(tab),
        action.files as string[],
      );
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.upload(ctx, ref, files);
      });
      return { tab };
    }
    if (action.name === 'x-crew-drop') {
      const files = await this.approvedUploadFiles(
        this.ownerOfTab(tab),
        action.files as string[],
      );
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.drop(ctx, ref, {
          files,
          data: action.data as Record<string, string>,
        });
      });
      return { tab };
    }
    if (action.name === 'x-crew-drag') {
      const sourceRef = `@transaction-source-${randomUUID()}`;
      const targetRef = `@transaction-target-${randomUUID()}`;
      try {
        await pwActions.locateBySelector(
          ctx,
          sourceRef,
          String(action.sourceSelector),
          ctx.hash,
        );
        await pwActions.locateBySelector(
          ctx,
          targetRef,
          String(action.targetSelector),
          ctx.hash,
        );
        await pwActions.drag(ctx, sourceRef, targetRef, {
          ...(action.sourcePosition
            ? {
                sourcePosition: action.sourcePosition as {
                  x: number;
                  y: number;
                },
              }
            : {}),
          ...(action.targetPosition
            ? {
                targetPosition: action.targetPosition as {
                  x: number;
                  y: number;
                },
              }
            : {}),
        });
      } finally {
        ctx.refs.delete(sourceRef);
        ctx.refs.delete(targetRef);
      }
      return { tab };
    }
    if (action.name === 'x-crew-pointerGesture') {
      await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
        await pwActions.pointerGesture(ctx, ref, {
          pointerType: action.pointerType as pwActions.PointerDeviceType,
          button: action.button as pwActions.ClickButton,
          modifiers: action.modifiers as Array<'Alt' | 'Control' | 'Meta' | 'Shift'>,
          start: action.start as { x: number; y: number },
          points: action.points as pwActions.PointerGesturePoint[],
        });
      });
      return { tab };
    }
    if (action.name === 'x-crew-scroll') {
      if (String(action.selector)) {
        await this.withAtomicSelectorRef(ctx, String(action.selector), async (ref) => {
          await pwActions.hover(ctx, ref);
          await pwActions.scrollDelta(
            ctx,
            Number(action.deltaX),
            Number(action.deltaY),
          );
        });
      } else {
        await pwActions.scrollDelta(
          ctx,
          Number(action.deltaX),
          Number(action.deltaY),
        );
      }
      return { tab };
    }
    throw new BrowserHostError(`无法派发 transaction action：${action.name}`, {
      code: 'unsupported_transaction_action',
    });
  }

  private primaryAtomicEffect(
    sourcePageGuid: string,
    action: AtomicAction,
  ): AtomicEffect | null {
    if (action.name === 'x-crew-waitPopup') {
      return {
        kind: 'popup',
        page: String(action.popupPageGuid),
        opener_page: sourcePageGuid,
        popup_index: Number(action.popupIndex),
        activate: action.activate === true,
        disposition: String(action.disposition),
      };
    }
    if (action.name === 'x-crew-waitNavigation') {
      return {
        kind: 'navigation',
        page: sourcePageGuid,
        url: String(action.url),
      };
    }
    if (action.name === 'x-crew-waitDownload') {
      return {
        kind: 'download',
        page: sourcePageGuid,
        alias: String(action.alias),
        ordinal: Number(action.ordinal),
        suggested_filename: String(action.suggestedFilename),
      };
    }
    if (action.name === 'x-crew-waitDialog') {
      return {
        kind: 'dialog',
        page: sourcePageGuid,
        alias: String(action.alias),
        type: action.type as RecorderDialogType,
        accept: action.accept === true,
        text: String(action.text),
      };
    }
    if (action.name === 'x-crew-waitPageClosed') {
      return {
        kind: 'page_closed',
        page: sourcePageGuid,
        reason: String(action.reason),
      };
    }
    return null;
  }

  private async executeTransaction(
    key: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const required = [
      'profile_dir',
      'proxy_url',
      'download_dir',
      'schemaVersion',
      'transactionId',
      'source',
      'knownPages',
      'action',
      'expectedEffects',
      'timeoutMs',
    ];
    if (!exactKeys(params, required, ['max_transfer_bytes'])) {
      throw new BrowserHostError('execute_transaction params 形状无效', {
        code: 'invalid_transaction',
      });
    }
    if (params.schemaVersion !== 1) {
      throw new BrowserHostError('execute_transaction schemaVersion 无效', {
        code: 'invalid_transaction',
      });
    }
    const transactionId = strictTransactionInteger(
      params.transactionId,
      'transactionId',
    );
    const timeoutMs = strictTransactionInteger(params.timeoutMs, 'timeoutMs');
    const deadlineAt = Date.now() + timeoutMs;
    const profile = profilePath(params.profile_dir, key);
    const proxy = strictTransactionString(
      params.proxy_url,
      'proxy_url',
      true,
    ).trim();
    const owner = await this.ensureOwner(key, profile, proxy);
    const downloadDirRaw = strictTransactionString(
      params.download_dir,
      'download_dir',
      true,
    );
    const downloadDir = downloadDirRaw ? canonicalPath(downloadDirRaw) : '';
    const maxTransferBytes = transferLimit(params.max_transfer_bytes);
    if (downloadDirRaw && !path.isAbsolute(downloadDirRaw)) {
      throw new BrowserHostError('download_dir 必须是绝对路径', {
        code: 'invalid_transaction',
      });
    }
    const source = asRecord(params.source, 'transaction source');
    if (!exactKeys(source, ['pageGuid'], ['targetId'])) {
      throw new BrowserHostError('transaction source 形状无效', {
        code: 'invalid_transaction',
      });
    }
    const sourcePageGuid = strictPageGuid(source.pageGuid, 'source.pageGuid');
    const sourceTargetId = source.targetId === undefined
      ? ''
      : strictTransactionString(source.targetId, 'source.targetId');
    const action = parseAtomicAction(params.action);
    if (!Array.isArray(params.knownPages)) {
      throw new BrowserHostError('knownPages 必须是数组', {
        code: 'invalid_transaction',
      });
    }
    let targetIdByPageGuid = new Map<string, string>();
    let pageGuidByTargetId = new Map<string, string>();
    let tombstoneEpoch: AtomicReplayEpoch | undefined;
    for (const [index, raw] of params.knownPages.entries()) {
      const binding = asRecord(raw, `knownPages[${index}]`);
      if (!exactKeys(binding, ['pageGuid', 'targetId'])) {
        throw new BrowserHostError(`knownPages[${index}] 形状无效`, {
          code: 'invalid_transaction',
        });
      }
      const pageGuid = strictPageGuid(binding.pageGuid, `knownPages[${index}].pageGuid`);
      const targetId = strictTransactionString(
        binding.targetId,
        `knownPages[${index}].targetId`,
      );
      if (targetIdByPageGuid.has(pageGuid) || pageGuidByTargetId.has(targetId)) {
        throw new BrowserHostError('knownPages 包含重复绑定', {
          code: 'invalid_transaction',
        });
      }
      targetIdByPageGuid.set(pageGuid, targetId);
      pageGuidByTargetId.set(targetId, pageGuid);
      const live = this.tabsByTarget.get(targetId);
      const tab = live?.owner === owner ? live.tab : null;
      if (!tab) {
        const historical = [...owner.atomicReplayEpochs.values()].find(
          (candidate) => (
            candidate.targetIdByPageGuid.get(pageGuid) === targetId
            && (
              candidate.closedPageGuids.has(pageGuid)
              || candidate.journal.some((entry) => (
                !entry.consumed
                && entry.event.kind === 'page_closed'
                && entry.event.targetId === targetId
              ))
            )
          ),
        );
        if (action.name !== 'x-crew-waitPageClosed' || !historical) {
          throw new BrowserHostError('knownPages 引用了不存在的页面', {
            code: 'foreign_tab',
          });
        }
        tombstoneEpoch = historical;
      } else if (tab.crashed || tab.view.webContents.isDestroyed()) {
        throw new BrowserHostError('knownPages 引用了已停止页面', {
          code: 'tab_stopped',
        });
      } else {
        tab.downloadMaxBytes = maxTransferBytes;
      }
    }
    if (sourceTargetId) {
      const known = targetIdByPageGuid.get(sourcePageGuid);
      if (known && known !== sourceTargetId) {
        throw new BrowserHostError('source 与 knownPages 绑定冲突', {
          code: 'invalid_transaction_source',
        });
      }
      const live = this.tabsByTarget.get(sourceTargetId);
      if (live?.owner !== owner) {
        const historical = [...owner.atomicReplayEpochs.values()].find(
          (candidate) => (
            candidate.targetIdByPageGuid.get(sourcePageGuid) === sourceTargetId
            && (
              candidate.closedPageGuids.has(sourcePageGuid)
              || candidate.journal.some((entry) => (
                !entry.consumed
                && entry.event.kind === 'page_closed'
                && entry.event.targetId === sourceTargetId
              ))
            )
          ),
        );
        if (action.name !== 'x-crew-waitPageClosed' || !historical) {
          throw new BrowserHostError('transaction source 页面不存在', {
            code: 'invalid_transaction_source',
          });
        }
        tombstoneEpoch = historical;
      }
      targetIdByPageGuid.set(sourcePageGuid, sourceTargetId);
      pageGuidByTargetId.set(sourceTargetId, sourcePageGuid);
    }
    const additionalEffects = parseAtomicEffects(params.expectedEffects);
    const primaryEffect = this.primaryAtomicEffect(sourcePageGuid, action);
    const expectedEffects = [
      ...(primaryEffect ? [primaryEffect] : []),
      ...additionalEffects,
    ];
    if (
      expectedEffects.some((effect) => effect.kind === 'download')
      && !downloadDir
    ) {
      throw new BrowserHostError('下载事务必须提供 download_dir', {
        code: 'invalid_transaction',
      });
    }
    const liveTab = (targetId: string): BrowserTab | null => {
      const found = this.tabsByTarget.get(targetId);
      return found?.owner === owner ? found.tab : null;
    };
    let sourceTab = sourceTargetId
      ? liveTab(sourceTargetId)
      : targetIdByPageGuid.has(sourcePageGuid)
        ? liveTab(targetIdByPageGuid.get(sourcePageGuid)!)
        : null;
    let anchor = sourceTab
      ?? (
        targetIdByPageGuid.size
          ? [...targetIdByPageGuid.values()]
              .map((targetId) => liveTab(targetId))
              .find((candidate): candidate is BrowserTab => Boolean(candidate))
          : owner.tabs.get(owner.activeTabId) ?? (
              owner.tabs.size === 1 ? owner.tabs.values().next().value : undefined
            )
      );
    if (
      !anchor
      && transactionId === 1
      && action.name === 'openPage'
      && targetIdByPageGuid.size === 0
    ) {
      const freshSessionHash = sessionHash(
        `replay-v3:${key}:${randomUUID()}`,
      );
      anchor = this.createTab(
        owner,
        `s${freshSessionHash}-${owner.tabCounter + 1}`,
        freshSessionHash,
        '',
        'ai',
      );
      await this.initializeNewTab(anchor, deadlineAt);
      anchor.downloadMaxBytes = maxTransferBytes;
      sourceTab = anchor;
    }
    const atomicSessionHash = anchor?.sessionHash ?? tombstoneEpoch?.sessionHash ?? '';
    if (!atomicSessionHash) {
      throw new BrowserHostError('原子事务缺少浏览器会话锚点', {
        code: 'invalid_transaction_source',
      });
    }
    // The first openPage reuses the already-created manager tab. Later root
    // pages omit targetId while knownPages is non-empty and are created anew.
    if (
      action.name === 'openPage'
      && !sourceTab
      && targetIdByPageGuid.size === 0
      && anchor
    ) {
      sourceTab = anchor;
    }
    if (
      [...targetIdByPageGuid.values()].some(
        (targetId) => {
          const tab = liveTab(targetId);
          if (tab) return tab.sessionHash !== atomicSessionHash;
          return tombstoneEpoch?.sessionHash !== atomicSessionHash;
        },
      )
    ) {
      throw new BrowserHostError('knownPages 必须属于同一浏览器会话', {
        code: 'invalid_transaction_source',
      });
    }
    if (owner.atomicTransactions.has(atomicSessionHash)) {
      throw new BrowserHostError('浏览器会话已有原子事务', {
        code: 'transaction_busy',
      });
    }
    if (owner.downloadGrant) {
      throw new BrowserHostError('显式下载与原子事务不能并发', {
        code: 'download_busy',
      });
    }
    let epoch = owner.atomicReplayEpochs.get(atomicSessionHash);
    if (transactionId === 1) {
      epoch = {
        sessionHash: atomicSessionHash,
        epochId: randomUUID(),
        lastTransactionId: 0,
        nextEventSequence: 0,
        historicalFloor: 0,
        journal: [],
        pageGuidByTargetId: new Map(),
        targetIdByPageGuid: new Map(),
        popupOrdinalBases: new Map(),
        closedPageGuids: new Set(),
        downloadOrdinals: new Map(),
        downloadDir,
      };
      owner.atomicReplayEpochs.set(atomicSessionHash, epoch);
    } else if (!epoch) {
      throw new BrowserHostError('replay epoch 不存在，必须从 transactionId=1 开始', {
        code: 'transaction_epoch_missing',
      });
    }
    if (transactionId !== epoch.lastTransactionId + 1) {
      throw new BrowserHostError(
        `transactionId 必须连续递增（期望 ${epoch.lastTransactionId + 1}）`,
        { code: 'transaction_sequence_invalid' },
      );
    }
    epoch.downloadDir = downloadDir || epoch.downloadDir;
    for (const [pageGuid, targetId] of targetIdByPageGuid) {
      const previousTarget = epoch.targetIdByPageGuid.get(pageGuid);
      const previousPage = epoch.pageGuidByTargetId.get(targetId);
      if (
        previousTarget && previousTarget !== targetId
        || previousPage && previousPage !== pageGuid
      ) {
        throw new BrowserHostError('knownPages 与 replay epoch 绑定冲突', {
          code: 'invalid_transaction_source',
        });
      }
      epoch.targetIdByPageGuid.set(pageGuid, targetId);
      epoch.pageGuidByTargetId.set(targetId, pageGuid);
      if (!epoch.popupOrdinalBases.has(targetId)) {
        epoch.popupOrdinalBases.set(
          targetId,
          owner.popupOrdinals.get(
            `${atomicSessionHash}\u0000${targetId}`,
          ) ?? 0,
        );
      }
    }
    targetIdByPageGuid = epoch.targetIdByPageGuid;
    pageGuidByTargetId = epoch.pageGuidByTargetId;
    const popupOrdinalBases = epoch.popupOrdinalBases;
    let resolveCompletion!: () => void;
    let rejectCompletion!: (error: BrowserHostError) => void;
    const completion = new Promise<void>((resolve, reject) => {
      resolveCompletion = resolve;
      rejectCompletion = reject;
    });
    const run: AtomicTransactionRun = {
      transactionId,
      sessionHash: atomicSessionHash,
      epoch,
      armSequence: epoch.nextEventSequence,
      allowHistoricalEffects: action.name.startsWith('x-crew-wait'),
      historicalFloor: epoch.historicalFloor,
      deadlineAt,
      downloadDir,
      expectedEffects,
      matchedEffects: [],
      effectCursor: 0,
      reportedEffectStart: primaryEffect ? 1 : 0,
      pageGuidByTargetId,
      targetIdByPageGuid,
      popupOrdinalBases,
      closedPageGuids: new Set(),
      downloads: [],
      downloadCompletions: [],
      completion,
      resolve: resolveCompletion,
      reject: rejectCompletion,
      settled: false,
      timer: setTimeout(() => undefined, 1),
    };
    clearTimeout(run.timer);
    run.timer = setTimeout(() => {
      this.settleAtomicTransaction(
        owner,
        run,
        new BrowserHostError(
          `原子事务未在期限内匹配全部 effect（${run.effectCursor}/${run.expectedEffects.length}）`,
          {
            code: 'transaction_effect_timeout',
            uncertain: true,
            partial: run.effectCursor > 0,
          },
        ),
      );
    }, remainingCommandTimeoutMs(deadlineAt));
    run.timer.unref();
    owner.atomicTransactions.set(atomicSessionHash, run);
    if (!expectedEffects.length) {
      this.settleAtomicTransaction(owner, run);
    } else {
      this.consumeAtomicJournal(owner, run);
      if (
        !run.settled
        && primaryEffect?.kind === 'navigation'
        && sourceTab
        && this.tabUrl(sourceTab) === primaryEffect.url
      ) {
        this.appendAtomicJournal(owner, epoch, {
          kind: 'navigation',
          targetId: sourceTab.targetId,
          url: primaryEffect.url,
        });
      }
    }

    let snapshot: string | undefined;
    try {
      const dialogs = this.atomicExpectedDialogs(run);
      const dispatch = async (): Promise<void> => {
        const result = await this.dispatchAtomicAction(
          owner,
          run,
          sourcePageGuid,
          sourceTab,
          action,
        );
        sourceTab = result.tab;
        snapshot = result.snapshot;
      };
      if (dialogs.length) {
        if (!anchor) {
          throw new BrowserHostError('dialog 事务缺少存活页面锚点', {
            code: 'invalid_transaction_source',
          });
        }
        await this.withExpectedDialogs(
          anchor,
          dialogs,
          dispatch,
          remainingCommandTimeoutMs(deadlineAt),
        );
      } else {
        await dispatch();
      }
      await run.completion;
      if (run.downloadCompletions.length) {
        await withDeadline(
          Promise.all(run.downloadCompletions).then(() => undefined),
          remainingCommandTimeoutMs(deadlineAt),
          () => new BrowserHostError('下载未在原子事务截止时间内完成', {
            code: 'transaction_download_timeout',
            uncertain: true,
            partial: true,
          }),
        );
      }
      const activeTarget = owner.tabs.get(owner.activeTabId)?.targetId ?? '';
      const response = {
        matchedEffects: run.matchedEffects,
        pageBindings: [...run.targetIdByPageGuid]
          .filter(([pageGuid]) => (
            !run.epoch.closedPageGuids.has(pageGuid)
            || run.closedPageGuids.has(pageGuid)
          ))
          .map(([pageGuid, targetId]) => ({
            pageGuid,
            targetId,
          })),
        downloads: run.downloads,
        activePageGuid: run.pageGuidByTargetId.get(activeTarget) ?? '',
        closedPageGuids: [...run.closedPageGuids],
        ...(snapshot !== undefined ? { snapshot } : {}),
      };
      epoch.lastTransactionId = transactionId;
      epoch.historicalFloor = run.armSequence;
      return response;
    } catch (error) {
      const failure = error instanceof BrowserHostError
        ? error
        : new BrowserHostError(
            error instanceof Error ? error.message : '原子事务失败',
            {
              code: 'transaction_failed',
              uncertain: true,
              partial: run.effectCursor > 0,
            },
          );
      this.settleAtomicTransaction(
        owner,
        run,
        failure,
      );
      await run.completion.catch(() => undefined);
      throw failure;
    } finally {
      clearTimeout(run.timer);
      if (owner.atomicTransactions.get(atomicSessionHash) === run) {
        owner.atomicTransactions.delete(atomicSessionHash);
      }
    }
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
          // execute_transaction is the only producer of a live replay epoch.
          // A later ordinary execute proves replay ownership has ended; clear
          // its journal before a normal page download can be claimed as atomic.
          if (!owner.atomicTransactions.has(tab.sessionHash)) {
            owner.atomicReplayEpochs.delete(tab.sessionHash);
          }
          const inner = operation;
          operation = () => this.withGenericDownloadCapture(
            owner,
            tab,
            expectedDialogTimeoutMs(
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
    let commandTimeoutMs = expectedDialogTimeoutMs(
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
    if (Object.prototype.hasOwnProperty.call(params, 'expected_dialogs')) {
      if (
        !new Set([
          'open', 'back', 'forward', 'reload', 'click', 'fill', 'fill_form',
          'drag', 'select', 'check', 'scroll', 'press', 'keydown', 'keyup',
          'mouse', 'drop', 'upload', 'upload_with_trigger', 'eval',
          'run_code_unsafe',
        ]).has(command)
      ) {
        throw new BrowserHostError('该命令不支持原子对话框回放', {
          code: 'invalid_expected_dialogs',
        });
      }
      const dialogs = parseExpectedDialogs(params.expected_dialogs);
      const timeoutMs = expectedDialogTimeoutMs(
        params.command_timeout_ms,
        params.command_deadline_ms,
      );
      const innerParams = { ...params };
      delete innerParams.expected_dialogs;
      return this.withExpectedDialogs(
        tab,
        dialogs,
        () => this.executeCommand(owner, command, args, innerParams, true),
        timeoutMs,
      );
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
        return this.navigate(owner, tab, args[0], commandDeadlineAt, true);
      case 'preview':
        return this.previewArtifact(owner, tab, args, commandDeadlineAt);
      case 'back': {
        if (args.length) invalidCommandArgs();
        const ctx = await this.actionContext(tab, commandTimeoutMs);
        const recordedNavigation = this.beginRecordedNavigation(tab, 'back', '');
        try {
          await pwActions.goBack(ctx);
        } catch (error) {
          this.cancelRecordedNavigation(tab, recordedNavigation);
          // page.goBack() === null proves that no navigation was dispatched;
          // keep the current ref generation usable in that deterministic case.
          if (!(error instanceof pwActions.ActionError && error.code === 'no_history')) {
            this.clearDocumentState(tab);
          }
          BrowserHost.rethrowAction(error);
        }
        await this.completeRecordedNavigation(tab, recordedNavigation, ctx.page.url());
        this.clearDocumentState(tab);
        return {};
      }
      case 'forward': {
        if (args.length) invalidCommandArgs();
        const ctx = await this.actionContext(tab, commandTimeoutMs);
        const recordedNavigation = this.beginRecordedNavigation(tab, 'forward', '');
        try {
          await pwActions.goForward(ctx);
        } catch (error) {
          this.cancelRecordedNavigation(tab, recordedNavigation);
          if (!(error instanceof pwActions.ActionError && error.code === 'no_history')) {
            this.clearDocumentState(tab);
          }
          BrowserHost.rethrowAction(error);
        }
        await this.completeRecordedNavigation(tab, recordedNavigation, ctx.page.url());
        this.clearDocumentState(tab);
        return {};
      }
      case 'reload': {
        if (args.length) invalidCommandArgs();
        const ctx = await this.actionContext(tab, commandTimeoutMs);
        this.clearDocumentState(tab);
        const recordedNavigation = this.beginRecordedNavigation(tab, 'reload', '');
        try {
          await pwActions.reload(ctx);
        } catch (error) {
          this.cancelRecordedNavigation(tab, recordedNavigation);
          BrowserHost.rethrowAction(error);
        }
        await this.completeRecordedNavigation(tab, recordedNavigation, ctx.page.url());
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
        // 参数是 selector（不是 ref）：处理器要跨越整场回放存活，而 ref 表
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
      case 'locate':
        // 回放入口：技能里存盘的稳定选择器 → 可执行 ref。
        if (args.length !== 1 || !args[0]) invalidCommandArgs();
        return this.locateBySelector(tab, args[0], commandTimeoutMs);
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

  /**
   * Join a pre-existing human tab to the one unambiguous active recording in
   * its Crew session, but only when the user explicitly selects or closes it.
   *
   * Existing background tabs are intentionally not enrolled at recording
   * start: loading timers and unsolicited navigations on a tab the user never
   * visits are not part of the demonstration. `startRecording` emits this
   * page's openPage + current CSS viewport before the caller records activate
   * or close, preserving replay topology and global ledger order.
   */
  private async lazyJoinHumanRecording(
    owner: BrowserOwner,
    tab: BrowserTab,
    timeoutMs: number,
  ): Promise<RecordingState | null> {
    if (tab.recording) {
      return (
        !tab.recording.ledger.closing
        && tab.recording.accepting
        && !tab.recording.paused
      ) ? tab.recording : null;
    }
    if (tab.mode !== 'human') return null;
    const peersByLedger = new Map<RecordingLedger, RecordingState>();
    for (const candidate of owner.tabs.values()) {
      const state = candidate.recording;
      if (
        candidate === tab
        || candidate.sessionHash !== tab.sessionHash
        || !state
        || state.ledger.closing
        || !state.accepting
        || state.paused
      ) {
        continue;
      }
      peersByLedger.set(state.ledger, state);
    }
    if (peersByLedger.size !== 1) return null;
    const peer = peersByLedger.values().next().value as RecordingState;
    try {
      await this.startRecording(
        owner,
        tab,
        peer.ledger.recordingId,
        peer.ledger,
        0,
        timeoutMs,
      );
    } catch (error) {
      // Never block the user's native tab operation because recorder bootstrap
      // failed. The shared trace is explicitly non-compilable instead.
      this.markRecordingStateIncomplete(peer);
      this.emit('browser-error', {
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        code: 'recorder_lazy_join_failed',
        error: error instanceof Error ? error.message : '无法录制已存在标签页',
      });
      return null;
    }
    // startRecording mutates the tab through a private helper; force a fresh
    // read because TypeScript otherwise preserves the earlier null narrowing.
    const joined = (tab as BrowserTab).recording as RecordingState | null;
    return (
      joined?.ledger === peer.ledger
      && joined.accepting
      && !joined.paused
    ) ? joined : null;
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
          // Historical owner-side counter for direct children of this page.
          // Unlike the live tab rows, this survives closed popups and therefore
          // lets replay translate recording-local popup ordinals exactly.
          popupOrdinalBase: owner.popupOrdinals.get(
            `${tab.sessionHash}\u0000${tab.targetId}`,
          ) ?? 0,
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
      const inheritedRecording = userCreated
        ? [...owner.tabs.values()].find((candidate) => (
            candidate.sessionHash === match[1]
            && candidate.recording
            && !candidate.recording.ledger.closing
          ))?.recording ?? null
        : null;
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
        if (inheritedRecording) {
          await this.startRecording(
            owner,
            tab,
            inheritedRecording.ledger.recordingId,
            inheritedRecording.ledger,
            0,
            remainingCommandTimeoutMs(commandDeadlineAt),
          );
        }
      } catch (error) {
        this.closeTab(owner, tab, false);
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
      if (args[0] === 'close-user') {
        await this.lazyJoinHumanRecording(
          owner,
          tab,
          remainingCommandTimeoutMs(commandDeadlineAt),
        );
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
      const changed = owner.activeTabId !== tab.tabId;
      if (changed && tab.mode === 'human') {
        await this.lazyJoinHumanRecording(
          owner,
          tab,
          remainingCommandTimeoutMs(commandDeadlineAt),
        );
      }
      if (downloadDir) this.setTabDownloadDir(tab, downloadDir);
      tab.downloadMaxBytes = downloadMaxBytes;
      if (!owner.atomicTransactions.has(tab.sessionHash)) {
        owner.atomicReplayEpochs.delete(tab.sessionHash);
      }
      owner.activeTabId = tab.tabId;
      if (
        changed
        && tab.mode === 'human'
        && tab.recording?.ledger.schemaVersion === 11
        && tab.recording.accepting
        && !tab.recording.paused
      ) {
        this.appendV11HostAction(
          tab,
          tab.recording,
          { name: 'x-crew-activatePage' },
        );
      }
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
      locateCounter: 0,
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
      nativeInputProofs: [],
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
      recording: null,
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
    contents.on('before-input-event', (event, input) => {
      const native = input as unknown as Record<string, unknown>;
      if (
        tab.mode === 'human'
        && tab.automationDepth === 0
        && String(native.type || '') === 'keyDown'
      ) {
        const key = String(native.key || '');
        this.recordNativeInputProof(tab, 'keyboard', ['key', 'input', 'submit'], key);
      }
      // AI 模式下仍然拦截按键：租约外的原生输入会和自动化抢同一个页面。
      // 但**不再**据此推断接管——控制权只由显式动作改变。
      if (tab.mode !== 'human' && tab.automationDepth === 0) {
        event.preventDefault();
      }
    });
    // Electron 43 类型定义未包含 before-mouse-event，但运行时与单测均依赖它。
    // 用 any 绕过类型检查，保留原有行为。
    (contents as any).on('before-mouse-event', (event: Electron.Event, input: { type?: string }) => {
      const type = String(input.type || '');
      if (tab.mode === 'human' && tab.automationDepth === 0) {
        if (type === 'mouseDown') {
          this.recordNativeInputProof(
            tab,
            'pointer',
            ['click', 'dblclick', 'drag', 'drop', 'input', 'upload', 'submit'],
          );
        } else if (type === 'mouseWheel') {
          this.recordNativeInputProof(tab, 'scroll', ['scroll', 'wheel']);
        }
      }
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
      const createdByCausalId = tab.recording
        ? this.activeRecorderCausal(tab.recording, tab, '')?.causalId ?? 0
        : 0;
      return {
        action: 'allow',
        // Crew models every opened browsing context as a browser tab. Closing
        // an opener must therefore not let Electron tear down an independent
        // OAuth/result/child tab before Playwright or replay can bind it.
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
          this.observeAtomicPopup(
            owner,
            tab,
            popup,
            details.disposition || 'default',
            !opensInBackground,
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
          const inheritedLedger = tab.recording?.ledger;
          if (inheritedLedger && !inheritedLedger.closing) {
            // A popup is part of the same human demonstration, not a second
            // recording. Its CDP bindings/controllers remain per-WebContents,
            // while ordering, step numbers and stop semantics share one ledger.
            const popupRecording = this.attachRecordingState(
              popup,
              inheritedLedger.recordingId,
              inheritedLedger,
              createdByCausalId,
            );
            // Persist the page-open anchor before createWindow returns. The
            // popup may synchronously navigate, document.write(), show a
            // dialog, or even close before the next native turn.
            if (popupRecording && !popupRecording.initialPageRecorded) {
              popupRecording.initialPageRecorded = true;
              if (popupRecording.ledger.schemaVersion === 11 && tab.recording) {
                const transaction = this.appendV11Signal(
                  tab,
                  tab.recording,
                  { name: 'popup', popupPageGuid: popupRecording.pageId },
                  {
                    openerPageGuid: tab.recording.pageId,
                    popupIndex: popupRecording.popupOrdinal,
                    disposition: details.disposition || 'default',
                    activate: !opensInBackground,
                  },
                  {
                    causalId: popupRecording.createdByCausalId,
                    timestamp: Date.now(),
                  },
                );
                popupRecording.createdByTransaction = transaction;
              } else {
                this.recordNavigation(owner, popup);
              }
            }
            setImmediate(() => {
              void this.startRecording(
                owner,
                popup,
                inheritedLedger.recordingId,
                inheritedLedger,
                createdByCausalId,
              ).catch((error: unknown) => {
                inheritedLedger.incomplete = true;
                inheritedLedger.dropped += 1;
                this.emit('browser-error', {
                  runtimeKey: owner.runtimeKey,
                  targetId: popup.targetId,
                  code: 'recorder_popup_partial',
                  error: error instanceof Error
                    ? error.message
                    : 'popup recorder installation failed',
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
      if (isMainFrame === true) {
        this.freezeRecorderNavigationCausal(tab);
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
    contents.on('did-navigate', (_event, committedUrl) => {
      tab.navigationEpoch += 1;
      tab.navigationPending = false;
      this.emit('tab-updated', this.publicTabEvent(owner, tab));
      this.observeAtomicNavigation(owner, tab);
      this.recordNavigation(
        owner,
        tab,
        this.takeRecorderNavigationCausal(tab),
        false,
        undefined,
        typeof committedUrl === 'string' && committedUrl ? committedUrl : undefined,
      );
    });
    contents.on('did-navigate-in-page', (details, _legacyUrl, legacyIsMainFrame) => {
      const isMainFrame = navigationFlag(details, 'isMainFrame', legacyIsMainFrame);
      if (isMainFrame === true) {
        tab.navigationEpoch += 1;
        tab.navigationPending = false;
        this.emit('tab-updated', this.publicTabEvent(owner, tab));
        this.observeAtomicNavigation(owner, tab);
        // hash 路由（Vue/React）走的正是这条：整条路由在 fragment 里，
        // 不记就等于 SPA 上的每一次页面切换都没录。
        const committedUrl = (
          details
          && typeof details === 'object'
          && typeof (details as unknown as Record<string, unknown>).url === 'string'
        )
          ? String((details as unknown as Record<string, unknown>).url)
          : typeof _legacyUrl === 'string'
            ? _legacyUrl
            : undefined;
        this.recordNavigation(
          owner,
          tab,
          this.takeRecorderNavigationCausal(tab),
          false,
          undefined,
          committedUrl || undefined,
        );
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
      if (tab.recording) tab.recording.pendingNavigationCausal = null;
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
      if (tab.recording) tab.recording.pendingNavigationCausal = null;
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
      if (tab.recording) tab.recording.pendingNavigationCausal = null;
      this.recoverPanelAfterTabFailure(owner, tab);
      // Renderer-initiated close (window.close/OAuth popup) bypasses closeTab().
      // unregisterTab is idempotent, so the normal close path may safely reach
      // this handler after already unregistering.
      this.observeAtomicPageClosed(owner, tab, 'window.close');
      this.recordPageClosed(owner, tab, 'window.close', false);
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


  private recordNativeInputProof(
    tab: BrowserTab,
    kind: NativeProofKind,
    allowed: NativeProofEvent[],
    key = '',
  ): void {
    const now = Date.now();
    // 连续打字会产生几十个 keyDown。只保留每种来源的最新一次，避免积攒出一批
    // 可被页面脚本事后消费的“授权券”。
    tab.nativeInputProofs = tab.nativeInputProofs.filter(
      (proof) => proof.kind !== kind && proof.expiresAt >= now,
    );
    tab.nativeInputProofs.push({
      kind,
      expiresAt: now + NATIVE_INPUT_PROOF_TTL_MS,
      remaining: new Set(allowed),
      key,
    });
  }

  private consumeNativeInputProof(tab: BrowserTab, event: RecorderEvent): boolean {
    if (event.type === 'navigate') return true;
    const now = Date.now();
    tab.nativeInputProofs = tab.nativeInputProofs.filter((proof) => proof.expiresAt >= now);
    for (let index = tab.nativeInputProofs.length - 1; index >= 0; index -= 1) {
      const proof = tab.nativeInputProofs[index];
      if (!proof?.remaining.has(event.type)) continue;
      if (
        event.type === 'key'
        && proof.kind === 'keyboard'
        && proof.key
        && !event.key.endsWith(proof.key)
      ) {
        continue;
      }
      proof.remaining.delete(event.type);
      if (proof.remaining.size === 0) tab.nativeInputProofs.splice(index, 1);
      return true;
    }
    return false;
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

  /**
   * Keep recorder installation aligned with Chromium's real flattened target
   * graph. The transport does not expose `Target.attachedToTarget` to
   * Playwright until this method settles, so an OOPIF cannot run past
   * `Runtime.runIfWaitingForDebugger` before its document-world binding/script
   * exist.
   */
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
      const state = tab.recording;
      if (state) {
        const recorderSession = state.sessions.get(context.sessionId);
        if (recorderSession) {
          recorderSession.cancelled = true;
          recorderSession.installed = false;
        }
        state.sessions.delete(context.sessionId);
        this.clearRecorderContextsForSession(state, context.sessionId);
      }
      return;
    }

    tab.childSessions.set(context.sessionId, { ...context.targetInfo });
    tab.childSessionParents.set(context.sessionId, context.parentSessionId);
    if (String(context.targetInfo.type ?? '') !== 'iframe') return;
    const state = tab.recording;
    if (!state || !state.accepting) return;
    try {
      await this.installRecorderSession(tab, state, context.sessionId, context.signal);
    } catch (error) {
      // An OOPIF may disappear while its debugger session is being installed.
      // That frame is a partial-capability hole, but stopping the whole recording
      // loses the main document and every healthy frame. Surface the degradation
      // explicitly and keep recording everywhere that remains instrumented.
      if (tab.recording === state) {
        this.markRecordingStateIncomplete(state);
        this.emit('browser-error', {
          runtimeKey: owner.runtimeKey,
          targetId: tab.targetId,
          code: 'recorder_child_session_failed',
          childSessionId: context.sessionId,
          childTargetId: normalizedText(context.targetInfo.targetId, 256),
          error: error instanceof Error
            ? error.message
            : 'OOPIF recorder installation failed',
        });
      }
    }
  }

  private clearRecorderContextsForSession(
    state: RecordingState,
    sessionId: string,
  ): void {
    const prefix = `${sessionId}\u0000`;
    for (const key of state.contexts) {
      if (key.startsWith(prefix)) state.contexts.delete(key);
    }
    for (const key of state.contextFrames.keys()) {
      if (key.startsWith(prefix)) state.contextFrames.delete(key);
    }
  }

  private clearRecorderCausals(
    state: RecordingState,
    tab: BrowserTab,
    childSessionId?: string,
    executionContextId?: number,
  ): void {
    const base = `${tab.targetId}\u0000${childSessionId ?? ''}\u0000`;
    const exact = executionContextId === undefined
      ? ''
      : `${base}${executionContextId}`;
    for (const key of state.ledger.activeCausals.keys()) {
      if (exact ? key === exact : key.startsWith(base)) {
        state.ledger.activeCausals.delete(key);
      }
    }
    const tokenBase = `${childSessionId ?? ''}\u0000`;
    const tokenExact = executionContextId === undefined
      ? ''
      : `${tokenBase}${executionContextId}\u0000`;
    for (const key of state.causalTokens.keys()) {
      if (tokenExact ? key.startsWith(tokenExact) : key.startsWith(tokenBase)) {
        state.causalTokens.delete(key);
      }
    }
  }

  private recorderContextKey(sessionId: string, executionContextId: number): string {
    return `${sessionId}\u0000${executionContextId}`;
  }

  private activeRecorderCausal(
    state: RecordingState,
    tab: BrowserTab,
    childSessionId: string,
  ): { causalId: number; capturedAt: number } | undefined {
    const tabPrefix = `${tab.targetId}\u0000`;
    const sessionPrefix = `${tabPrefix}${childSessionId}\u0000`;
    const actionable = (
      entries: Array<[string, {
        causalId: number;
        capturedAt: number;
        eventType: RecorderEventType;
      }]>,
    ) => entries.filter(([, entry]) => entry.eventType !== 'submit');
    const sameSession = actionable(
      [...state.ledger.activeCausals.entries()]
        .filter(([key]) => key.startsWith(sessionPrefix)),
    );
    if (sameSession.length === 1) return sameSession[0][1];
    if (sameSession.length > 1) return undefined;

    // Page.javascriptDialogOpening is normally emitted on the top-level page
    // session even when an OOPIF action opened it. Permit that one exact active
    // task only; concurrent tasks in multiple frames are deliberately left
    // standalone instead of guessing by timestamp.
    const sameTab = actionable(
      [...state.ledger.activeCausals.entries()]
        .filter(([key]) => key.startsWith(tabPrefix)),
    );
    if (sameTab.length === 1) return sameTab[0][1];
    if (sameTab.length > 1 || state.createdByCausalId <= 0) return undefined;
    // A freshly-created popup has no local execution context yet. While the
    // opener's exact task remains active, route bootstrap navigation/dialog
    // events to that frozen causal id. Once causal-end removes the opener task,
    // later timers on the popup remain standalone instead of being guessed.
    const inherited = actionable(
      [...state.ledger.activeCausals.entries()]
        .filter(([, entry]) => entry.causalId === state.createdByCausalId),
    );
    return inherited.length === 1 ? inherited[0][1] : undefined;
  }

  /**
   * Freeze the current task identity at navigation start, while the originating
   * document and its recorder execution context still exist.
   *
   * A zero identity is intentional: it prevents an unrelated action that starts
   * while the destination is committing from being attached retroactively.
   */
  private freezeRecorderNavigationCausal(tab: BrowserTab): void {
    const state = tab.recording;
    if (!state || state.paused || !state.accepting) return;
    const active = this.activeRecorderCausal(state, tab, '');
    state.pendingNavigationCausal = {
      causalId: active?.causalId ?? 0,
      capturedAt: Date.now(),
    };
  }

  private takeRecorderNavigationCausal(
    tab: BrowserTab,
  ): { causalId: number; capturedAt: number } | undefined {
    const state = tab.recording;
    if (!state) return undefined;
    const causal = state.pendingNavigationCausal ?? undefined;
    state.pendingNavigationCausal = null;
    return causal;
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
      const activeCausal = tab.recording
        ? this.activeRecorderCausal(tab.recording, tab, childSessionId)
        : undefined;
      tab.dialog = {
        type: normalizedText(params.type),
        message: normalizedText(params.message),
        defaultValue: normalizedText(params.defaultPrompt),
        owner: tab.dialogForwarding ? 'playwright' : 'native',
        causalId: activeCausal?.causalId ?? 0,
      };
      this.notifySessionModal(owner, tab, 'dialog');
      const expectedRun = owner.expectedDialogRuns.get(tab.sessionHash);
      if (expectedRun) {
        void this.handleExpectedDialogOpening(owner, tab, expectedRun, tab.dialog.type);
        return;
      }
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
    if (method === 'Runtime.executionContextCreated') {
      const state = tab.recording;
      if (!state) return;
      const context = asOptionalRecord(params.context);
      const auxData = asOptionalRecord(context.auxData);
      const executionContextId = Number(context.id);
      const frameId = normalizedText(auxData.frameId, 256);
      if (
        Number.isSafeInteger(executionContextId)
        && executionContextId > 0
        && frameId
        && auxData.isDefault !== false
      ) {
        state.contextFrames.set(
          this.recorderContextKey(childSessionId, executionContextId),
          frameId,
        );
      }
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
      if (tab.recording) {
        this.clearRecorderContextsForSession(tab.recording, childSessionId);
        this.clearRecorderCausals(tab.recording, tab, childSessionId);
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
      if (tab.recording && Number.isSafeInteger(executionContextId)) {
        const contextKey = this.recorderContextKey(childSessionId, executionContextId);
        tab.recording.contexts.delete(contextKey);
        tab.recording.contextFrames.delete(contextKey);
        this.clearRecorderCausals(
          tab.recording,
          tab,
          childSessionId,
          executionContextId,
        );
      }
      return;
    }
    if (method === 'Runtime.bindingCalled') {
      if (!tab.recording) return;
      const recordingState = tab.recording;
      // Electron OOPIF 的稳定路径只能使用 document world。绑定名因此不是信任边界；
      // 每段录制使用不可预测名称，并继续严格校验 session/schema/bounds/isTrusted。
      if (normalizedText(params.name, 120) !== recordingState.bindingName) return;
      const contextId = Number(params.executionContextId);
      if (!Number.isSafeInteger(contextId) || contextId <= 0) return;
      const recorderSession = recordingState.sessions.get(childSessionId);
      if (!recorderSession?.installed) return;
      // **接受当前已安装 session 的所有 frame 上下文，不只第一个。**
      //
      // 注入脚本会进入每一个 frame，主页面与每个 iframe 各有一份 document 上下文。
      // 早先只认第一个上报的，于是主页面和 iframe 必然有一方被整个丢掉——工单
      // 详情页的附件清单就在 iframe 里，用户在那里的操作一条都录不到；
      // 更糟的情况是 iframe 先上报，主页面的所有操作反而全没了。
      //
      // Electron 的 OOPIF 路径不能可靠创建自定义隔离世界。严格 schema/bounds 和
      // Event.isTrusted 是持久化基线；原生输入关联是非阻断审计信号。
      recordingState.contexts.add(this.recorderContextKey(childSessionId, contextId));
      const rawPayload = String(params.payload ?? '');
      const causalKey = `${tab.targetId}\u0000${this.recorderContextKey(
        childSessionId,
        contextId,
      )}`;
      const control = parseRecorderControlEvent(rawPayload);
      if (control) {
        if (control.type === 'causal-begin') {
          const tokenKey = `${this.recorderContextKey(
            childSessionId,
            contextId,
          )}\u0000${control.token}`;
          let token = recordingState.causalTokens.get(tokenKey);
          if (!token) {
            token = {
              causalId: ++recordingState.ledger.causalCounter,
              capturedAt: Date.now(),
            };
            recordingState.causalTokens.set(tokenKey, token);
          }
          recordingState.ledger.activeCausals.set(causalKey, {
            seq: control.seq,
            causalId: token.causalId,
            capturedAt: token.capturedAt,
            eventType: 'input',
            scope: 'input',
          });
          return;
        }
        const active = recordingState.ledger.activeCausals.get(causalKey);
        const controlScope = control.token > 0 ? 'input' : 'event';
        if (active?.seq === control.seq && active.scope === controlScope) {
          recordingState.ledger.activeCausals.delete(causalKey);
        }
        return;
      }
      const event = parseRecorderEvent(rawPayload);
      if (!event) {
        // This is the recorder's own random binding on a session whose bootstrap
        // completed. An undecodable packet means Host and injected recorder lost
        // their schema contract; silently skipping it would produce a trace that
        // looks complete but is missing an observed browser event.
        this.markRecordingStateIncomplete(recordingState);
        return;
      }
      // Selector normalization, file resolution and earlier ledger entries can take
      // seconds. Causal navigation matching must use when the browser event arrived,
      // not when this event eventually reaches recordStep.
      event.capturedAt = Date.now();
      const causalToken = event.causalToken ?? 0;
      const tokenKey = causalToken > 0
        ? `${this.recorderContextKey(
          childSessionId,
          contextId,
        )}\u0000${causalToken}`
        : '';
      const token = tokenKey
        ? recordingState.causalTokens.get(tokenKey)
        : undefined;
      if (tokenKey) recordingState.causalTokens.delete(tokenKey);
      if (causalToken > 0 && !token) {
        // Preserve the action, but never certify a trace whose modal/action
        // association was lost through a protocol or lifecycle mismatch.
        this.markRecordingStateIncomplete(recordingState);
      }
      event.causalId = token?.causalId ?? ++recordingState.ledger.causalCounter;
      if (recordingState.ledger.schemaVersion === 11) {
        this.reserveV11Action(recordingState, event);
      }
      recordingState.ledger.activeCausals.set(causalKey, {
        seq: event.seq,
        causalId: event.causalId,
        capturedAt: event.capturedAt,
        eventType: event.type,
        scope: 'event',
      });
      const ordinarilyAccepting = recordingState.accepting && !recordingState.paused;
      if (!ordinarilyAccepting && !(recordingState.drainingFlush && event.lifecycleFlush)) {
        return;
      }
      // 原生输入关联只做审计统计，不再作为持久化门槛。IME、粘贴、浏览器自动填充、
      // 辅助技术与系统级文本服务不一定产生一一对应的 before-input-event；把证明当
      // 硬门会系统性丢掉真实表单填写，录制结果无法通用复现。
      const nativeCorrelated = this.consumeNativeInputProof(tab, event);
      if (!nativeCorrelated) {
        recordingState.ledger.forged += 1;
      }
      event.schemaVersion = RECORDER_EVENT_SCHEMA_VERSION;
      event.provenance = {
        ...(event.provenance ?? {
          schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
          source: 'document-world',
          capturePhase: 'event-callback',
          browserTrusted: true,
          targetEvidence: event.target ? 'synchronous' : 'none',
        }),
        nativeInput: nativeCorrelated ? 'correlated' : 'unverified',
      };
      const clickIdentity = JSON.stringify([
        childSessionId,
        event.target?.framePath ?? [],
        event.target?.cssPath ?? '',
        event.target?.id ?? '',
        event.clickButton ?? '',
        event.modifiers ?? [],
      ]);
      if (event.type === 'click') {
        const previous = recordingState.pendingClicks.get(clickIdentity);
        if ((event.clickCount ?? 1) > 1 && previous) {
          recordingState.supersededClicks.add(previous);
        }
        recordingState.pendingClicks.set(clickIdentity, event);
      } else if (event.type === 'dblclick') {
        const previous = recordingState.pendingClicks.get(clickIdentity);
        if (previous) recordingState.supersededClicks.add(previous);
        recordingState.pendingClicks.delete(clickIdentity);
      }
      // Start the dblclick grace period at event arrival, not after every
      // previous queued recordStep. Timers run concurrently while the ledger
      // still commits events in arrival order, so 100 clicks cost ~275 ms
      // rather than serially stalling stop/pause for ~27.5 seconds.
      const clickReady = event.type === 'click'
        ? new Promise<void>((resolve) => setTimeout(resolve, 500))
        : Promise.resolve();
      // 串行处理：recordStep 里有 await（取 backendNodeId、打快照），并发跑
      // 会让步骤按完成顺序而非发生顺序落盘。把它们排成一条链，并把链尾存下来，
      // 停止录制时 await 它——否则最后几个动作会在 stop 之后才处理完，而那时
      // tab.recording 已经是 null，整段被丢掉。
      const chain = recordingState.ledger.queue.then(async () => {
        if (event.type === 'click') {
          await clickReady;
          if (recordingState.supersededClicks.has(event)) return;
          if (recordingState.pendingClicks.get(clickIdentity) === event) {
            recordingState.pendingClicks.delete(clickIdentity);
          }
        }
        await this.recordStep(owner, tab, recordingState, event, {
          sessionId: childSessionId,
          executionContextId: contextId,
        });
      }).catch(() => {
        // Never break the browser interaction or the remainder of the queue,
        // but never call a trace complete after silently losing one action.
        this.markRecordingStateIncomplete(recordingState);
      });
      recordingState.ledger.queue = chain;
      return;
    }
    if (method === 'Page.javascriptDialogClosed') {
      const closedDialog = tab.dialog;
      if (closedDialog) {
        this.recordDialogDecision(owner, tab, closedDialog, params);
        this.observeAtomicDialogClosed(owner, tab, closedDialog, params);
      }
      this.handleExpectedDialogClosed(owner, tab, params);
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
          // receives no main-world replay, so locator utility worlds keep
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
    recordExplicitOperation = false,
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
    let recordedNavigation: PendingRecordedNavigation | null = null;
    try {
      try {
        if (recordExplicitOperation) {
          recordedNavigation = this.beginRecordedNavigation(tab, 'goto', url);
        }
        await ctx.page.goto(url, {
          waitUntil: 'domcontentloaded',
          timeout: remainingCommandTimeoutMs(commandDeadlineAt),
        });
        await this.completeRecordedNavigation(tab, recordedNavigation, ctx.page.url());
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (/Download is starting/i.test(message)) {
          this.cancelRecordedNavigation(tab, recordedNavigation);
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
        this.cancelRecordedNavigation(tab, recordedNavigation);
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
      this.cancelRecordedNavigation(tab, recordedNavigation);
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
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async sendInSession(
    tab: BrowserTab,
    childSessionId: string,
    method: string,
    params?: Record<string, unknown>,
  ): Promise<any> {
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
   * `register` 为 false 时（录制期观察）不动 tab.refs —— 录制不能冲掉 AI 正在用的 ref 表。
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

  /**
   * 把技能里存盘的稳定选择器解析成当前页面上的一个 ref。
   *
   * 登记出来的 ref 用 `@sN` 前缀与快照 ref（`@eN`）区分，但走的是同一张 ref 表，
   * 因此后续 click/fill 直接复用同一套 Playwright Locator 动作与错误映射。
   *
   * 与快照 ref 一样，它在下一次 snapshot 时随整张表被替换。
   */
  private async locateBySelector(
    tab: BrowserTab,
    selector: string,
    timeoutMs = ACTION_TIMEOUT_MS,
  ): Promise<Record<string, unknown>> {
    const value = asString(selector, 'selector', 4096);
    if (!value) throw new BrowserHostError('选择器不能为空', { code: 'invalid_selector' });
    const ctx = await this.actionContext(tab, timeoutMs);
    tab.locateCounter += 1;
    const nativeRef = `@s${tab.locateCounter}`;
    const record = await pwActions
      .locateBySelector(ctx, nativeRef, value, ctx.hash)
      .catch(BrowserHost.rethrowAction);
    return {
      ref: nativeRef,
      role: publicConsoleText(record.role),
      name: publicConsoleText(record.name),
      action: record.action,
      action_kind: record.actionKind,
      tag: record.tag,
      input_type: record.inputType,
      content_editable: record.contentEditable,
      tier: record.fieldTier,
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
    // native event order this lets lifecycle commands (notably recording stop)
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
      // popup modals and completion settling). Expected transactions own their
      // own sequence. The action-level race remains available to direct users
      // of playwright-actions and its parity contracts.
      raceDialogs: (
        tab.modalRaceDepth === 0
        && !owner.modalWaiters.has(tab.sessionHash)
        && !owner.expectedDialogRuns.has(tab.sessionHash)
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
   * Replay one recorded upload without a click/file_upload RPC gap.
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
        await pwActions.locateBySelector(
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
        await pwActions.locateBySelector(
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

  private settleExpectedDialogs(
    owner: BrowserOwner,
    run: ExpectedDialogRun,
    error?: BrowserHostError,
  ): void {
    if (run.settled) return;
    run.settled = true;
    clearTimeout(run.timer);
    if (error) run.reject(error);
    else run.resolve();
    // The transaction remains installed until its triggering action settles,
    // so an additional chained dialog cannot escape through a short clear gap.
    if (owner.expectedDialogRuns.get(run.sessionHash) !== run) {
      clearTimeout(run.timer);
    }
  }

  private maybeCompleteExpectedDialogs(owner: BrowserOwner, run: ExpectedDialogRun): void {
    if (
      !run.settled
      && run.actionFinished
      && run.opened === run.dialogs.length
      && run.index === run.dialogs.length
    ) {
      this.settleExpectedDialogs(owner, run);
    }
  }

  private completeExpectedDialogObservation(
    owner: BrowserOwner,
    run: ExpectedDialogRun,
  ): void {
    // Independent pages can surface their dialogs in a different scheduler
    // order while still matching the exact recorded page topology. Count
    // completed slots instead of forcing unrelated tabs through one FIFO.
    run.index = run.observations.filter(
      (observation) => observation.handlerDone && observation.closeDone,
    ).length;
    this.maybeCompleteExpectedDialogs(owner, run);
  }

  private expectedDialogMatches(tab: BrowserTab, expected: ExpectedDialog): boolean {
    if (expected.targetId) return tab.targetId === expected.targetId;
    if (expected.openerTargetId && tab.openerTargetId !== expected.openerTargetId) {
      return false;
    }
    if (
      expected.popupOrdinal !== null
      && tab.popupOrdinal !== expected.popupOrdinal
    ) {
      return false;
    }
    return true;
  }

  private async handleExpectedDialogOpening(
    owner: BrowserOwner,
    tab: BrowserTab,
    run: ExpectedDialogRun,
    actualType: string,
  ): Promise<void> {
    if (owner.expectedDialogRuns.get(run.sessionHash) !== run) return;
    const assigned = new Set(
      run.observations.map((observation) => observation.expectedIndex),
    );
    const expectedIndex = run.dialogs.findIndex(
      (expected, index) => !assigned.has(index) && this.expectedDialogMatches(tab, expected),
    );
    if (run.settled || expectedIndex < 0) {
      const error = new BrowserHostError(
        `动作触发了未录制的额外 JavaScript 对话框（${actualType || 'unknown'}）`,
        {
          code: 'replay_dialog_mismatch',
          partial: true,
        },
      );
      this.settleExpectedDialogs(owner, run, error);
      // Always release the renderer. Leaving an unexpected modal open would
      // turn a classified replay failure into an unrelated action timeout.
      await this.ownerOfTab(tab).engine.handleDialog(tab.view, {
        accept: false,
        timeoutMs: run.timeoutMs,
      })
        .catch(() => undefined);
      return;
    }
    const expected = run.dialogs[expectedIndex];
    const observation: ExpectedDialogObservation = {
      expectedIndex,
      actualType,
      targetId: tab.targetId,
      handlerDone: false,
      closeDone: false,
    };
    // Reserve the slot before the first await. Closing the current modal can
    // synchronously resume page JavaScript and open the next one before
    // Dialog.accept()/dismiss() resolves.
    run.opened += 1;
    run.observations.push(observation);
    try {
      const observed = await this.ownerOfTab(tab).engine.handleDialog(tab.view, {
        accept: expected.action === 'accept',
        expectedType: expected.type,
        ...(expected.type === 'prompt' && expected.action === 'accept'
          ? { promptText: expected.text }
          : {}),
        timeoutMs: run.timeoutMs,
      });
      const mismatch = !observed.matched
        || observation.actualType !== expected.type
        || observed.type !== expected.type;
      if (mismatch) {
        this.settleExpectedDialogs(
          owner,
          run,
          new BrowserHostError(
            `JavaScript 对话框类型不一致：录制为 ${expected.type}，实际为 ${observed.type || actualType || 'unknown'}`,
            {
              code: 'replay_dialog_mismatch',
              partial: true,
            },
          ),
        );
        return;
      }
      observation.handlerDone = true;
      this.completeExpectedDialogObservation(owner, run);
    } catch (error) {
      this.settleExpectedDialogs(
        owner,
        run,
        new BrowserHostError(
          `无法处理 JavaScript 对话框：${error instanceof Error ? error.message : 'unknown'}`,
          {
            code: 'replay_dialog_failed',
            uncertain: true,
            partial: true,
          },
        ),
      );
    }
  }

  private handleExpectedDialogClosed(
    owner: BrowserOwner,
    tab: BrowserTab,
    params: Record<string, unknown>,
  ): void {
    const run = owner.expectedDialogRuns.get(tab.sessionHash);
    if (!run || run.settled) return;
    const observation = run.observations.find((candidate) => (
      candidate.targetId === tab.targetId && !candidate.closeDone
    ));
    if (!observation) {
      this.settleExpectedDialogs(
        owner,
        run,
        new BrowserHostError('动作关闭了未观察到的 JavaScript 对话框', {
          code: 'replay_dialog_mismatch',
          partial: true,
        }),
      );
      return;
    }
    const expected = run.dialogs[observation.expectedIndex];
    const accepted = params.result === true;
    const userInput = typeof params.userInput === 'string' ? params.userInput : '';
    if (
      accepted !== (expected.action === 'accept')
      || (
        expected.type === 'prompt'
        && expected.action === 'accept'
        && userInput !== expected.text
      )
    ) {
      this.settleExpectedDialogs(
        owner,
        run,
        new BrowserHostError('JavaScript 对话框结果与录制不一致', {
          code: 'replay_dialog_mismatch',
          partial: true,
        }),
      );
      return;
    }
    observation.closeDone = true;
    this.completeExpectedDialogObservation(owner, run);
  }

  private async withExpectedDialogs<T>(
    tab: BrowserTab,
    dialogs: ExpectedDialog[],
    operation: () => Promise<T>,
    timeoutMs: number,
  ): Promise<T> {
    const owner = this.ownerOfTab(tab);
    if (owner.expectedDialogRuns.has(tab.sessionHash)) {
      throw new BrowserHostError('该浏览器会话已有进行中的对话框事务', {
        code: 'replay_dialog_busy',
      });
    }
    let resolveCompletion!: () => void;
    let rejectCompletion!: (error: BrowserHostError) => void;
    const completion = new Promise<void>((resolve, reject) => {
      resolveCompletion = resolve;
      rejectCompletion = reject;
    });
    const run: ExpectedDialogRun = {
      sessionHash: tab.sessionHash,
      triggerTargetId: tab.targetId,
      timeoutMs,
      dialogs,
      opened: 0,
      index: 0,
      observations: [],
      actionFinished: false,
      settled: false,
      resolve: resolveCompletion,
      reject: rejectCompletion,
      timer: setTimeout(() => undefined, 1),
    };
    clearTimeout(run.timer);
    run.timer = setTimeout(() => {
      this.settleExpectedDialogs(
        owner,
        run,
        new BrowserHostError(
          `动作未在期限内触发完整的 JavaScript 对话框序列（${run.index}/${dialogs.length}）`,
          {
            code: 'replay_dialog_timeout',
            partial: true,
          },
        ),
      );
    }, timeoutMs);
    owner.expectedDialogRuns.set(tab.sessionHash, run);

    // A timer-driven dialog can open after the triggering transaction returned
    // and before its observation-only wait RPC is armed. The CDP opening event
    // has already populated tab.dialog, so adopt that pending modal into this
    // run instead of waiting for an opening event that will never repeat.
    const pendingDialogs = Promise.all(
      this.sessionDialogTabs(owner, tab.sessionHash).map((candidate) => (
        this.handleExpectedDialogOpening(
          owner,
          candidate,
          run,
          candidate.dialog?.type ?? '',
        )
      )),
    );
    const action = operation().then(
      (result) => {
        run.actionFinished = true;
        this.maybeCompleteExpectedDialogs(owner, run);
        return result;
      },
      (error) => {
        run.actionFinished = true;
        if (!run.settled) {
          this.settleExpectedDialogs(
            owner,
            run,
            error instanceof BrowserHostError
              ? error
              : new BrowserHostError(
                error instanceof Error ? error.message : '浏览器动作失败',
                { code: 'command_failed', uncertain: true },
              ),
          );
        }
        throw error;
      },
    );
    try {
      const [result] = await Promise.all([action, completion, pendingDialogs]);
      return result;
    } catch (error) {
      // A mismatch handler dismisses the modal immediately, so the action
      // normally settles at once. Await it to avoid returning while a
      // Playwright mutation is still live in the background.
      await action.catch(() => undefined);
      throw error;
    } finally {
      clearTimeout(run.timer);
      if (owner.expectedDialogRuns.get(tab.sessionHash) === run) {
        owner.expectedDialogRuns.delete(tab.sessionHash);
      }
    }
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

  /**
   * 录制开关。`action` ∈ start | pause | resume | stop。
   *
   * 录制只能由用户从浏览器面板发起（见设计文档：模型不持有任何录制控制工具），
   * 所以这里不做模型可达性检查——它压根不在模型的工具表里。
   */
  private async setRecording(
    key: string,
    params: Record<string, unknown>,
    modalRaceArmed = false,
  ): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    const action = asString(params.action, 'recording action', 20).trim();
    const commandTimeoutMs = expectedDialogTimeoutMs(
      params.command_timeout_ms,
      params.command_deadline_ms,
    );
    const commandDeadlineAt = Date.now() + commandTimeoutMs;
    const requestedRecordingId = asString(params.recording_id, 'recording id', 64).trim().toLowerCase();
    // pause/resume/stop 必须作用在**正在录制的那个标签页**上，不是当前活动的。
    //
    // 录制态挂在单个 BrowserTab 上，而用户完全可能在 A 页开录、随手打开 B 页
    // （或被 target=_blank 带走）再点停止。按活动标签页发，停的是 B，A 还在录，
    // 而 UI 已经显示「已停止」——一个还在写盘的隐形录制器。
    const recording = [...owner.tabs.values()].find((candidate) => candidate.recording);
    const tab = action === 'start'
      ? this.targetTab(owner, params.target_id)
      : (recording ?? this.targetTab(owner, params.target_id));
    if (action === 'start' && recording) {
      // 已经在录了就明确报错，**不能静默 no-op**：Crew 侧每次 start 都会换一个
      // 新的 recording_id，宿主这边却什么都不做，于是后续事件被写进新目录、
      // 前半段留在旧目录——同一段演示被拆成两半，编译时谁也拼不回去。
      throw new BrowserHostError(
        recording === tab
          ? '该标签页已在录制中；如需重新开始请先停止'
          : '已有其它标签页正在录制；请先停止那一段再开始新的录制',
        { code: 'recording_conflict' },
      );
    }
    if (!modalRaceArmed) {
      this.releaseSettledModalAction(owner, tab.sessionHash);
      if (this.sessionDialogTabs(owner, tab.sessionHash).length) {
        throw new BrowserHostError(
          '浏览器会话有待处理的 JavaScript 对话框；处理后可继续切换录制状态',
          { code: 'dialog_pending' },
        );
      }
      if (this.sessionFileChooserTabs(owner, tab.sessionHash).length) {
        throw new BrowserHostError(
          '浏览器会话有待处理的文件选择器；处理后可继续切换录制状态',
          { code: 'file_chooser_pending' },
        );
      }
      return this.withSessionModalRace(
        owner,
        tab,
        () => this.setRecording(key, params, true),
      );
    }
    type StoppedRecording = {
      steps: number;
      forged: number;
      incomplete: boolean;
      dropped: number;
      recordingId: string;
    };
    // Validate the absolute budget before the async operation is constructed:
    // an already-expired pause/resume/stop must not synchronously flip recorder
    // state and only then discover that the caller has gone away.
    const recordingTimeoutMs = remainingCommandTimeoutMs(commandDeadlineAt);
    const operation = (async (): Promise<StoppedRecording | null> => {
      switch (action) {
        case 'start':
          if (!RECORDING_ID_RE.test(requestedRecordingId)) {
            throw new BrowserHostError('录制 ID 无效', { code: 'invalid_recording_id' });
          }
          await this.startRecording(
            owner,
            tab,
            requestedRecordingId,
            undefined,
            0,
            remainingCommandTimeoutMs(commandDeadlineAt),
          );
          return null;
        case 'pause':
          // Flush the focused dirty control while the binding still accepts packets, then make
          // both current and future documents dormant. Typing followed by an immediate panel
          // pause must not lose the final value merely because blur/change never fired.
          if (tab.recording && !tab.recording.paused) {
            const ledger = tab.recording.ledger;
            const members = [...ledger.members].filter(
              (member) => member.recording?.ledger === ledger,
            );
            for (const member of members) {
              const state = member.recording!;
              state.paused = true;
              state.drainingFlush = true;
            }
            const paused = await Promise.allSettled(
              members.map((member) =>
                this.setRecorderCaptureEnabled(member, member.recording!, false)),
            );
            const pauseFailures = paused.filter(
              (result): result is PromiseRejectedResult => result.status === 'rejected',
            );
            if (pauseFailures.length) {
              ledger.incomplete = true;
              ledger.dropped += pauseFailures.length;
            }
            await ledger.queue.catch(() => undefined);
            for (const member of members) {
              if (member.recording?.ledger === ledger) member.recording.drainingFlush = false;
            }
          }
          for (const member of tab.recording?.ledger.members ?? [tab]) {
            member.nativeInputProofs = [];
          }
          return null;
        case 'resume':
          if (tab.recording && tab.recording.paused) {
            const ledger = tab.recording.ledger;
            const members = [...ledger.members].filter(
              (member) => member.recording?.ledger === ledger,
            );
            for (const member of members) {
              member.nativeInputProofs = [];
              const state = member.recording!;
              state.drainingFlush = false;
            }
            const resumed = await Promise.allSettled(
              members.map((member) =>
                this.setRecorderCaptureEnabled(member, member.recording!, true)),
            );
            const resumeFailures = resumed.filter(
              (result): result is PromiseRejectedResult => result.status === 'rejected',
            );
            if (resumeFailures.length) {
              ledger.incomplete = true;
              ledger.dropped += resumeFailures.length;
              // Host-side paused remains authoritative even if one document was
              // reactivated before another failed. Best-effort deactivate every
              // member so a later retry starts from a coherent state.
              await Promise.allSettled(
                members.map((member) =>
                  this.setRecorderCaptureEnabled(member, member.recording!, false)),
              );
              throw new BrowserHostError('无法在所有录制页面恢复事件捕获', {
                code: 'recorder_control_failed',
                partial: true,
              });
            }
            for (const member of members) {
              if (member.recording?.ledger !== ledger) continue;
              member.recording.paused = false;
              const bounds = member.view.getBounds();
              this.recordViewportResize(
                member,
                member.recording,
                bounds.width,
                bounds.height,
              );
            }
            await ledger.queue;
          }
          return null;
        case 'stop':
          return await this.stopRecordingGroup(tab);
        default:
          throw new BrowserHostError('不支持的录制操作', { code: 'invalid_request' });
      }
    })();
    const stopped = await withDeadline(
      operation,
      recordingTimeoutMs,
      () => new BrowserHostError(`录制${action}操作超过命令截止时间`, {
        code: 'command_timeout',
        uncertain: true,
        partial: true,
        phase: 'dispatching',
      }),
    );
    return {
      recording: Boolean(tab.recording),
      paused: Boolean(tab.recording?.paused),
      // stop 之后 tab.recording 已清空，统计只能取停止时留存下来的那份。
      steps: stopped ? stopped.steps : (tab.recording?.ledger.steps ?? 0),
      // 未关联到原生输入的事件数（非阻断审计）。IME/粘贴也可能增加该值。
      forged: stopped ? stopped.forged : (tab.recording?.ledger.forged ?? 0),
      incomplete: stopped ? stopped.incomplete : (tab.recording?.ledger.incomplete ?? false),
      dropped: stopped ? stopped.dropped : (tab.recording?.ledger.dropped ?? 0),
      recordingId: stopped?.recordingId ?? tab.recording?.ledger.recordingId ?? '',
    };
  }

  /**
   * 开始录制：注入捕获脚本并注册绑定。
   *
   * 脚本走 `addScriptToEvaluateOnNewDocument`，因此翻页、跳转后会自动重新注入，
   * 不需要在每次导航后补一刀。Electron 的 OOPIF 兼容约束与来源校验见
   * `installRecorderInCurrentDocuments`。
   */
  private async startRecording(
    owner: BrowserOwner,
    tab: BrowserTab,
    recordingId: string,
    inheritedLedger?: RecordingLedger,
    createdByCausalId = 0,
    timeoutMs = 0,
  ): Promise<void> {
    const state = tab.recording ?? this.attachRecordingState(
      tab,
      recordingId,
      inheritedLedger,
      createdByCausalId,
    );
    if (!state || state.ledger.closing) return;
    if (state.installation) {
      await state.installation;
      return;
    }
    const installation = (async (): Promise<void> => {
    try {
      await this.ensureDebugger(tab);
      await this.installRecorderSession(tab, state, '', undefined, timeoutMs);
      const existingOopifs = [...tab.childSessions.entries()]
        .filter(([, targetInfo]) => String(targetInfo.type ?? '') === 'iframe')
        .map(([sessionId]) => sessionId);
      await Promise.all(
        existingOopifs.map((sessionId) =>
          this.installRecorderSession(
            tab,
            state,
            sessionId,
            undefined,
            timeoutMs,
            true,
          )),
      );
    } catch (error) {
      state.accepting = false;
      state.paused = true;
      await this.stopRecording(tab);
      throw new BrowserHostError(
        `无法安装浏览器录制器：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'recorder_unavailable' },
      );
    }

    // 起始快照。
    //
    // 没有它，「已经在详情页上开始录制、只阅读、然后停止」这种纯查阅演示会得到
    // **零步**——而那恰恰是工单场景最典型的一段。事件驱动的录制只在用户动手时
    // 才记东西，可用户这次什么都没动，他要教的就是"看这一页的这些字段"。
    //
    // 走的是 recordNavigation 的同一条路：它本来就是"记录当前页面态"，
    // 起始点与一次导航在语义上没有区别。
    if (!state.initialPageRecorded) {
      state.initialPageRecorded = true;
      let viewport: { width: number; height: number };
      try {
        viewport = await this.currentCssViewport(owner, tab, timeoutMs);
      } catch (error) {
        state.accepting = false;
        state.paused = true;
        await this.stopRecording(tab);
        throw new BrowserHostError(
          `无法读取录制初始视口：${error instanceof Error ? error.message : 'unknown'}`,
          { code: 'recorder_viewport_unavailable' },
        );
      }
      this.recordNavigation(owner, tab, undefined, true, viewport);
      this.recordViewportResize(
        tab,
        state,
        viewport.width,
        viewport.height,
      );
      // Make start's response deterministic and prove the initial page anchor
      // and viewport have either been emitted or marked incomplete before the
      // UI reports recording active.
      await state.ledger.queue;
    }
    })();
    state.installation = installation;
    try {
      await installation;
    } finally {
      if (state.installation === installation) state.installation = null;
    }
  }

  /**
   * Join a page to a recording synchronously.
   *
   * Popup createWindow callbacks cannot await debugger/script installation.
   * Publishing membership and the recording-local page identity here, before
   * returning the WebContents to Chromium, ensures early navigation/dialog
   * events enter the correct ledger even while installation completes.
   */
  private attachRecordingState(
    tab: BrowserTab,
    recordingId: string,
    inheritedLedger?: RecordingLedger,
    createdByCausalId = 0,
  ): RecordingState | null {
    if (tab.recording) return tab.recording;
    if (inheritedLedger?.closing) return null;
    tab.nativeInputProofs = [];
    const ledger: RecordingLedger = inheritedLedger ?? {
      recordingId,
      schemaVersion: recordingV11Enabled() ? 11 : 10,
      startedAt: Date.now(),
      steps: 0,
      forged: 0,
      incomplete: false,
      dropped: 0,
      closing: false,
      queue: Promise.resolve(),
      eventIndex: 0,
      transactionCounter: 0,
      transactionsByCausalId: new Map(),
      reservedActions: new WeakMap(),
      dialogCounter: 0,
      downloadCounter: 0,
      downloadOrdinals: new Map(),
      members: new Set<BrowserTab>(),
      causalCounter: 0,
      pageCounter: 0,
      popupOrdinals: new Map(),
      activeCausals: new Map(),
    };
    const inheritedPeer = inheritedLedger
      ? [...inheritedLedger.members].find(
        (member) => member.recording?.ledger === inheritedLedger,
      )?.recording
      : null;
    const inheritedPaused = inheritedPeer?.paused === true;
    const openerEntry = tab.openerTargetId
      ? this.tabsByTarget.get(tab.openerTargetId)
      : undefined;
    const openerState = openerEntry?.tab.recording?.ledger === ledger
      ? openerEntry.tab.recording
      : null;
    const popupOrdinal = tab.openerTargetId
      ? (ledger.popupOrdinals.get(tab.openerTargetId) ?? 0) + 1
      : 0;
    if (tab.openerTargetId) {
      ledger.popupOrdinals.set(tab.openerTargetId, popupOrdinal);
    }
    const state: RecordingState = {
      ledger,
      pageId: `p${++ledger.pageCounter}`,
      openerPageId: openerState?.pageId ?? '',
      popupOrdinal,
      createdByCausalId: Number.isSafeInteger(createdByCausalId) && createdByCausalId > 0
        ? createdByCausalId
        : 0,
      createdByTransaction: null,
      initialPageRecorded: false,
      installation: null,
      paused: inheritedPaused,
      accepting: true,
      drainingFlush: false,
      captureEnabled: !inheritedPaused,
      bindingName: `${RECORDER_BINDING}_${randomUUID().replaceAll('-', '')}`,
      targetStashName: `${RECORDER_TARGET_STASH}_${randomUUID().replaceAll('-', '')}`,
      controlName: `${RECORDER_CONTROL}_${randomUUID().replaceAll('-', '')}`,
      sessions: new Map(),
      lastPageDigest: '',
      lastRecordedViewport: null,
      contexts: new Set<string>(),
      contextFrames: new Map<string, string>(),
      causalTokens: new Map(),
      pendingClicks: new Map(),
      supersededClicks: new WeakSet(),
      pendingNavigationCausal: null,
      pendingRecordedNavigation: null,
      ignoredRecordedNavigationUrls: [],
      pageCloseRecorded: false,
    };
    tab.recording = state;
    ledger.members.add(tab);
    return state;
  }

  private async installRecorderSession(
    tab: BrowserTab,
    state: RecordingState,
    childSessionId: string,
    lifecycleSignal?: AbortSignal,
    timeoutMs = 0,
    activateCurrentDocument = false,
  ): Promise<void> {
    const existing = state.sessions.get(childSessionId);
    if (existing) {
      return await this.waitForRecorderSessionInstallation(
        tab,
        childSessionId,
        existing,
        lifecycleSignal,
        timeoutMs,
      );
    }

    const session: RecorderSessionState = {
      scriptId: '',
      bindingName: state.bindingName,
      bindingAdded: false,
      installed: false,
      cancelled: false,
      installation: Promise.resolve(),
    };
    session.installation = (async () => {
      try {
        const source = this.recorderSource(state);
        await this.sendInSession(tab, childSessionId, 'Runtime.addBinding', {
          name: state.bindingName,
        });
        session.bindingAdded = true;
        if (session.cancelled) {
          await this.cleanupRecorderSession(tab, childSessionId, session);
          return;
        }
        const injected = (await this.sendInSession(
          tab,
          childSessionId,
          'Page.addScriptToEvaluateOnNewDocument',
          {
            source,
          },
        )) as { identifier?: string };
        session.scriptId = String(injected?.identifier ?? '');
        if (session.cancelled) {
          await this.cleanupRecorderSession(tab, childSessionId, session);
          return;
        }
        const childTargetUrl = childSessionId
          ? normalizedText(tab.childSessions.get(childSessionId)?.url, 2048)
          : '';
        // Target.attachedToTarget commonly reports url="" for an OOPIF and
        // that stored attach-time TargetInfo does not receive later URL
        // updates. A recording started after the page loaded must therefore
        // activate every enumerated existing OOPIF explicitly. The live
        // attach-before-resume path keeps activateCurrentDocument=false so it
        // never evaluates into a debugger-paused provisional target; its
        // addScriptToEvaluateOnNewDocument bootstrap covers the commit.
        if (
          !childSessionId
          || activateCurrentDocument
          || Boolean(childTargetUrl)
        ) {
          await this.installRecorderInCurrentDocuments(
            tab,
            childSessionId,
            source,
          );
        }
        if (session.cancelled) {
          await this.cleanupRecorderSession(tab, childSessionId, session);
          return;
        }
        session.installed = true;
      } catch (error) {
        session.cancelled = true;
        await this.cleanupRecorderSession(tab, childSessionId, session);
        throw error;
      }
    })();
    // Publish before awaiting so a duplicate attach/start path joins the same
    // installation instead of registering duplicate listeners.
    state.sessions.set(childSessionId, session);
    await this.waitForRecorderSessionInstallation(
      tab,
      childSessionId,
      session,
      lifecycleSignal,
      timeoutMs,
    );
  }

  /**
   * Install the recorder into documents that already existed when recording
   * started. `Page.addScriptToEvaluateOnNewDocument` covers future documents.
   *
   * Do not use `runImmediately` or create another isolated world here. Real
   * Electron 43 / Chromium 150 can leave the main renderer's Runtime channel
   * permanently unresponsive when either path is used on a page containing an
   * OOPIF. Current main-session documents are activated through Playwright's
   * existing frame contexts; a newly attached OOPIF is activated directly in
   * that child session before the transport releases it to Playwright.
   *
   * The recorder therefore lives in the document world on this Electron path.
   * The binding is not a trust boundary: BrowserHost accepts packets only for
   * an installed session with a matching random name and validated schema/bounds.
   */
  private async installRecorderInCurrentDocuments(
    tab: BrowserTab,
    childSessionId: string,
    source: string,
  ): Promise<void> {
    if (childSessionId) {
      const evaluated = await this.sendInSession(
        tab,
        childSessionId,
        'Runtime.evaluate',
        {
          expression: source,
          returnByValue: true,
          awaitPromise: false,
        },
      ) as { exceptionDetails?: unknown };
      if (evaluated.exceptionDetails) {
        throw new Error(`recorder activation failed: ${childSessionId}`);
      }
      return;
    }

    // Runtime.evaluate through Playwright's flattened main session can deadlock after a
    // document-start script is registered on a page that already owns an OOPIF (reproduced on
    // Electron 43 / Chromium 150). Electron's WebFrameMain executes in the same document world
    // without entering that CDP routing cycle. Child attach-before-resume still uses its direct
    // child Runtime session in the branch above.
    const mainFrame = tab.view.webContents.mainFrame;
    // `framesInSubtree` also contains OOPIF WebFrameMain proxies. Executing through
    // those proxies while Playwright owns the flattened child target can wedge the
    // renderer's Runtime channel even though executeJavaScript itself resolves.
    // Main-session activation therefore stays inside the main renderer process;
    // each OOPIF is activated exactly once through its direct child CDP session.
    const frames = [...mainFrame.framesInSubtree]
      .filter((frame) => frame.processId === mainFrame.processId);
    if (!frames.length) throw new Error('recorder page has no current frame');
    for (const frame of frames) {
      if (frame.detached) continue;
      try {
        await frame.executeJavaScript(source, false);
      } catch (error) {
        if (frame === mainFrame) throw error;
        // A subframe can navigate/detach during the pass. Its replacement is covered by the
        // init script already registered before this snapshot.
      }
    }
  }

  private recorderSource(state: RecordingState): string {
    return recorderScript({
      bindingName: state.bindingName,
      targetStashName: state.targetStashName,
      controlName: state.controlName,
      initiallyActive: state.captureEnabled,
      recordingSchemaVersion: state.ledger.schemaVersion,
      officialSelectorSource: officialInjectedScriptSource().source,
    });
  }

  /**
   * Keep future documents in the same active/dormant state as the current ones.
   *
   * Add the replacement first, then remove the old script. If navigation lands in the short
   * overlap, recorderScript's idempotent controller branch applies the newer active state;
   * there is no uninstrumented document gap.
   */
  private async replaceRecorderBootstrap(
    tab: BrowserTab,
    state: RecordingState,
  ): Promise<void> {
    const source = this.recorderSource(state);
    await Promise.allSettled(
      [...state.sessions.entries()].map(async ([childSessionId, session]) => {
        if (!session.installed || session.cancelled) return;
        const injected = await this.sendInSession(
          tab,
          childSessionId,
          'Page.addScriptToEvaluateOnNewDocument',
          { source },
        ) as { identifier?: string };
        const replacementId = String(injected?.identifier ?? '');
        if (!replacementId) return;
        const previousId = session.scriptId;
        session.scriptId = replacementId;
        if (previousId) {
          await this.sendInSession(
            tab,
            childSessionId,
            'Page.removeScriptToEvaluateOnNewDocument',
            { identifier: previousId },
          ).catch(() => undefined);
        }
      }),
    );
  }

  /** Activate/deactivate the permanent page listeners in every live document. */
  private async evaluateRecorderControl(
    tab: BrowserTab,
    state: RecordingState,
    method: 'activate' | 'deactivate',
  ): Promise<void> {
    const expression = `globalThis[${JSON.stringify(state.controlName)}]?.${method}?.()`;
    const operations: Array<Promise<unknown>> = [];
    const mainFrame = tab.view.webContents.mainFrame;
    for (const frame of mainFrame.framesInSubtree) {
      if (!frame.detached && frame.processId === mainFrame.processId) {
        operations.push(frame.executeJavaScript(expression, false));
      }
    }
    // Child-session direct evaluation additionally covers an OOPIF attach that has not yet
    // appeared in WebFrameMain.framesInSubtree.
    for (const [childSessionId, session] of state.sessions) {
      if (!childSessionId || !session.installed || session.cancelled) continue;
      operations.push(this.sendInSession(tab, childSessionId, 'Runtime.evaluate', {
        expression,
        returnByValue: true,
        awaitPromise: false,
      }));
    }
    const results = await Promise.allSettled(operations);
    const failures = results.filter(
      (result): result is PromiseRejectedResult => result.status === 'rejected',
    );
    if (failures.length) {
      throw new AggregateError(
        failures.map((result) => result.reason),
        `recorder control ${method} failed in ${failures.length} document(s)`,
      );
    }
  }

  private async setRecorderCaptureEnabled(
    tab: BrowserTab,
    state: RecordingState,
    enabled: boolean,
  ): Promise<void> {
    state.captureEnabled = enabled;
    await this.replaceRecorderBootstrap(tab, state);
    await this.evaluateRecorderControl(tab, state, enabled ? 'activate' : 'deactivate');
  }

  private async waitForRecorderSessionInstallation(
    tab: BrowserTab,
    childSessionId: string,
    session: RecorderSessionState,
    lifecycleSignal?: AbortSignal,
    timeoutMs = 0,
  ): Promise<void> {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let removeAbortListener = (): void => {};
    const deadline = new Promise<never>((_, reject) => {
      const fail = (reason: unknown): void => {
        const error = reason instanceof Error
          ? reason
          : new Error(`recorder session installation cancelled: ${childSessionId || '<main>'}`);
        session.cancelled = true;
        session.installed = false;
        void this.cleanupRecorderSession(tab, childSessionId, session);
        reject(error);
      };
      if (timeoutMs > 0) {
        timer = setTimeout(() => {
          fail(new Error(
            `recorder session installation timeout: ${childSessionId || '<main>'}`,
          ));
        }, timeoutMs);
      }
      if (lifecycleSignal) {
        const onAbort = (): void => fail(lifecycleSignal.reason);
        if (lifecycleSignal.aborted) onAbort();
        else {
          lifecycleSignal.addEventListener('abort', onAbort, { once: true });
          removeAbortListener = () => lifecycleSignal.removeEventListener('abort', onAbort);
        }
      }
    });
    try {
      await Promise.race([session.installation, deadline]);
      if (session.cancelled || !session.installed) {
        throw new Error(
          `recorder session installation cancelled: ${childSessionId || '<main>'}`,
        );
      }
    } finally {
      if (timer) clearTimeout(timer);
      removeAbortListener();
    }
  }

  private async cleanupRecorderSession(
    tab: BrowserTab,
    childSessionId: string,
    session: RecorderSessionState,
  ): Promise<void> {
    session.installed = false;
    if (childSessionId && !tab.childSessions.has(childSessionId)) return;
    const scriptId = session.scriptId;
    if (scriptId) {
      await this.sendInSession(
        tab,
        childSessionId,
        'Page.removeScriptToEvaluateOnNewDocument',
        { identifier: scriptId },
      ).catch(() => undefined);
      if (session.scriptId === scriptId) session.scriptId = '';
    }
    if (session.bindingAdded) {
      await this.sendInSession(
        tab,
        childSessionId,
        'Runtime.removeBinding',
        { name: session.bindingName },
      ).catch(() => undefined);
      session.bindingAdded = false;
    }
  }

  /**
   * Stop the opener/popup recording as one transaction. Mark every member
   * draining before the first await so a still-live popup cannot append ordinary
   * actions while another tab is already stopping.
   */
  private async stopRecordingGroup(
    tab: BrowserTab,
  ): Promise<{
    steps: number;
    forged: number;
    incomplete: boolean;
    dropped: number;
    recordingId: string;
  }> {
    const ledger = tab.recording?.ledger;
    if (!ledger) {
      return {
        steps: 0,
        forged: 0,
        incomplete: false,
        dropped: 0,
        recordingId: '',
      };
    }
    // Close membership before the first await. createWindow/startRecording checks
    // this bit synchronously, so a popup racing stop cannot inherit a ledger that
    // has already begun teardown.
    ledger.closing = true;
    while (true) {
      const members = [...ledger.members].filter(
        (member) => member.recording?.ledger === ledger,
      );
      if (!members.length) break;
      for (const member of members) {
        const state = member.recording!;
        state.accepting = false;
        state.paused = true;
        state.drainingFlush = true;
      }
      await Promise.allSettled(members.map((member) => this.stopRecording(member)));
    }
    await ledger.queue.catch(() => undefined);
    return {
      steps: ledger.steps,
      forged: ledger.forged,
      incomplete: ledger.incomplete,
      dropped: ledger.dropped,
      recordingId: ledger.recordingId,
    };
  }

  private tabHasLiveContents(tab: BrowserTab): boolean {
    try {
      const contents = tab.view.webContents;
      return Boolean(contents && !contents.isDestroyed());
    } catch {
      return false;
    }
  }

  private tabUrl(tab: BrowserTab): string {
    try {
      return tab.view.webContents.getURL() || 'about:blank';
    } catch {
      return 'about:blank';
    }
  }

  /** 停止录制：撤销注入与绑定。失败只记不抛——停止是用户动作，不该因清理失败而卡住。 */
  private async stopRecording(
    tab: BrowserTab,
  ): Promise<{
    steps: number;
    forged: number;
    incomplete: boolean;
    dropped: number;
    recordingId: string;
  }> {
    const state = tab.recording;
    if (!state) {
      return {
        steps: 0,
        forged: 0,
        incomplete: false,
        dropped: 0,
        recordingId: '',
      };
    }
    // First synchronously flush each document's focused dirty control while the binding still
    // accepts packets. Setting accepting=false first loses the most common final step: type,
    // then click the panel's Stop button without a page blur/change event.
    state.accepting = false;
    state.paused = true;
    state.drainingFlush = true;
    try {
      if (this.tabHasLiveContents(tab)) {
        await this.setRecorderCaptureEnabled(tab, state, false).catch(() => undefined);
      }
      // Every queued task captures this exact RecordingState. Drain flush
      // packets and previously observed actions before removing registrations.
      await state.ledger.queue.catch(() => undefined);
    } finally {
      state.drainingFlush = false;
      tab.nativeInputProofs = [];
      // Do not await installation promises here: a debugger command can hang
      // forever. Mark cancellation first, then issue idempotent cleanup now.
      for (const session of state.sessions.values()) {
        session.cancelled = true;
        session.installed = false;
      }
      if (tab.recording === state) tab.recording = null;
      state.ledger.members.delete(tab);
      if (this.tabHasLiveContents(tab)) {
        await Promise.allSettled(
          [...state.sessions.entries()].map(async ([childSessionId, session]) => {
            await this.cleanupRecorderSession(tab, childSessionId, session);
          }),
        );
      }
      state.sessions.clear();
      state.contexts.clear();
      state.contextFrames.clear();
      this.clearRecorderCausals(state, tab);
    }
    return {
      steps: state.ledger.steps,
      forged: state.ledger.forged,
      incomplete: state.ledger.incomplete,
      dropped: state.ledger.dropped,
      recordingId: state.ledger.recordingId,
    };
  }

  /**
   * Persist the human's exact JavaScript-dialog decision from CDP.
   *
   * The page event recorder cannot observe native dialog buttons, and a later
   * standalone replay command would deadlock behind the triggering Playwright
   * action. The compiler therefore attaches this row to the causal action and
   * Host replays the pair as one armed transaction.
   */
  private recordDialogDecision(
    owner: BrowserOwner,
    tab: BrowserTab,
    dialog: DialogState,
    params: Record<string, unknown>,
  ): void {
    const state = tab.recording;
    if (!state || state.paused || !state.accepting) return;
    if (!['alert', 'confirm', 'prompt', 'beforeunload'].includes(dialog.type)) {
      this.markRecordingStateIncomplete(state);
      return;
    }
    const accepted = params.result === true;
    if (state.ledger.schemaVersion === 11) {
      const dialogAlias = `dlg${++state.ledger.dialogCounter}`;
      this.appendV11Signal(
        tab,
        state,
        { name: 'dialog', dialogAlias },
        {
          type: dialog.type,
          action: accepted ? 'accept' : 'dismiss',
          promptText: (
            accepted
            && dialog.type === 'prompt'
            && typeof params.userInput === 'string'
          ) ? params.userInput : '',
        },
        {
          causalId: dialog.causalId,
          timestamp: Date.now(),
        },
      );
      return;
    }
    const event: RecorderEvent = {
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      provenance: {
        schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
        source: 'browser-host',
        capturePhase: 'host',
        browserTrusted: false,
        targetEvidence: 'none',
        nativeInput: 'host',
      },
      seq: 0,
      capturedAt: Date.now(),
      causalId: dialog.causalId,
      type: 'dialog',
      url: this.tabUrl(tab),
      hint: '',
      target: null,
      tier: 'plain',
      value: '',
      values: [],
      valueTruncated: false,
      key: '',
      clickCount: 0,
      position: null,
      dragSourcePosition: null,
      dragTargetPosition: null,
      modifiers: [],
      dialogAction: accepted ? 'accept' : 'dismiss',
      dialogType: dialog.type as RecorderDialogType,
      dialogText: (
        accepted
        && dialog.type === 'prompt'
        && typeof params.userInput === 'string'
      ) ? params.userInput : '',
      scrollX: 0,
      scrollY: 0,
      uploadMode: '',
      paths: [],
      fileCount: 0,
      multiple: false,
      accept: '',
      dropData: {},
    };
    state.ledger.queue = state.ledger.queue
      .then(() => this.recordStep(owner, tab, state, event, {
        sessionId: '',
        executionContextId: 0,
      }))
      .catch(() => {
        this.markRecordingStateIncomplete(state);
      });
  }

  /**
   * Read the renderer's actual CSS viewport at recording start.
   *
   * The panel supplies Electron bounds in DIP, but the durable replay contract
   * is CSS pixels. At default zoom those coordinate spaces coincide; querying
   * `innerWidth/innerHeight` through the public Playwright Page makes that an
   * observed fact instead of an assumption and remains correct if Chromium's
   * embedding behavior changes.
   */
  private async currentCssViewport(
    owner: BrowserOwner,
    tab: BrowserTab,
    timeoutMs = 0,
  ): Promise<{ width: number; height: number }> {
    const boundedTimeoutMs = Math.max(
      1,
      Math.min(ACTION_TIMEOUT_MS, timeoutMs > 0 ? timeoutMs : ACTION_TIMEOUT_MS),
    );
    const page = await owner.engine.pageForView(tab.view, boundedTimeoutMs);
    const viewport = await withDeadline(
      page.evaluate(() => ({
        width: window.innerWidth,
        height: window.innerHeight,
      })),
      boundedTimeoutMs,
      () => new BrowserHostError('读取页面 CSS viewport 超时', {
        code: 'recorder_viewport_unavailable',
      }),
    );
    if (
      !viewport
      || typeof viewport.width !== 'number'
      || typeof viewport.height !== 'number'
      || !Number.isFinite(viewport.width)
      || !Number.isFinite(viewport.height)
    ) {
      throw new BrowserHostError('页面 CSS viewport 无效', {
        code: 'recorder_viewport_unavailable',
      });
    }
    return { width: viewport.width, height: viewport.height };
  }

  /**
   * Persist a CSS viewport transition as an ordinary v11 action.
   *
   * `WebContentsView.setBounds()` uses display-independent pixels, the same
   * coordinate space Chromium exposes to layout as CSS viewport pixels. Only
   * width/height participate in deduplication, so dragging an unchanged panel
   * around the desktop is intentionally silent. Product-side magnitude caps
   * do not belong here; replay delegates browser validity to Playwright's
   * public `page.setViewportSize()`.
   */
  private recordViewportResize(
    tab: BrowserTab,
    state: RecordingState | null,
    width: number,
    height: number,
  ): void {
    if (
      !state
      || state.ledger.schemaVersion !== 11
      || state.paused
      || !state.accepting
    ) {
      return;
    }
    if (!Number.isFinite(width) || !Number.isFinite(height)) {
      this.markRecordingStateIncomplete(state);
      return;
    }
    if (
      state.lastRecordedViewport?.width === width
      && state.lastRecordedViewport.height === height
    ) {
      return;
    }
    state.lastRecordedViewport = { width, height };
    this.appendV11HostAction(
      tab,
      state,
      { name: 'x-crew-resize', width, height },
    );
  }

  /**
   * Arm an explicit human navigation at the exact public Playwright dispatch
   * boundary. No v11 identity is reserved yet: a deterministic `no_history` or
   * a pre-dispatch failure must leave neither a ghost action nor a step gap.
   */
  private beginRecordedNavigation(
    tab: BrowserTab,
    operation: RecordedNavigationOperation,
    url: string,
  ): PendingRecordedNavigation | null {
    const state = tab.recording;
    if (
      !state
      || state.ledger.schemaVersion !== 11
      || state.paused
      || !state.accepting
    ) {
      return null;
    }
    const previous = state.pendingRecordedNavigation;
    if (previous && !previous.committed) {
      // Owner commands are serialized. Reaching this branch means an earlier
      // dispatch escaped its completion boundary, so do not guess which native
      // commit belongs to which command.
      state.pendingRecordedNavigation = null;
      previous.cancelled = true;
      previous.settleObserved();
      this.markRecordingStateIncomplete(state);
    }
    let settleObserved!: () => void;
    const observed = new Promise<void>((resolve) => {
      settleObserved = resolve;
    });
    const pending: PendingRecordedNavigation = {
      operation,
      url,
      state,
      capturedAt: Date.now(),
      committed: false,
      cancelled: false,
      observed,
      settleObserved,
    };
    state.pendingRecordedNavigation = pending;
    return pending;
  }

  /**
   * Commit the explicit action and its authoritative browser navigation signal
   * into one v11 transaction. Electron's did-navigate hook normally calls this;
   * the successful public Playwright return path is a bounded fallback for
   * embedders that omit the Electron observer.
   */
  private commitRecordedNavigation(
    tab: BrowserTab,
    pending: PendingRecordedNavigation,
    actualUrl: string,
  ): boolean {
    const state = pending.state;
    if (pending.committed) return true;
    if (
      tab.recording !== state
      || state.pendingRecordedNavigation !== pending
      || state.ledger.schemaVersion !== 11
      || state.paused
      || !state.accepting
    ) {
      if (state.pendingRecordedNavigation === pending) {
        state.pendingRecordedNavigation = null;
      }
      return false;
    }
    pending.committed = true;
    state.pendingRecordedNavigation = null;
    pending.settleObserved();
    const committedUrl = actualUrl || this.tabUrl(tab);
    const transaction = this.appendV11HostAction(
      tab,
      state,
      {
        name: 'x-crew-navigate',
        operation: pending.operation,
        url: pending.url,
      },
      {
        timestamp: pending.capturedAt,
        url: committedUrl,
      },
    );
    this.appendV11Signal(
      tab,
      state,
      { name: 'navigation', url: committedUrl },
      {},
      {
        transaction,
        timestamp: pending.capturedAt,
      },
    );
    state.createdByTransaction = null;
    return true;
  }

  private async completeRecordedNavigation(
    tab: BrowserTab,
    pending: PendingRecordedNavigation | null,
    actualUrl: string,
  ): Promise<void> {
    if (!pending || pending.committed) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    try {
      await Promise.race([
        pending.observed,
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, 500);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (pending.committed || pending.cancelled) return;
    // Electron normally observes the same main-frame commit before this grace
    // expires. Keep one exact URL tombstone if an embedder delays that event so
    // it cannot be mistaken for the next explicit command.
    pending.state.ignoredRecordedNavigationUrls.push({
      url: actualUrl,
      expiresAt: Date.now() + 5_000,
    });
    this.commitRecordedNavigation(tab, pending, actualUrl);
  }

  private cancelRecordedNavigation(
    tab: BrowserTab,
    pending: PendingRecordedNavigation | null,
  ): void {
    if (!pending || pending.committed) return;
    pending.cancelled = true;
    pending.settleObserved();
    if (pending.state.pendingRecordedNavigation === pending) {
      pending.state.pendingRecordedNavigation = null;
    }
    // Keep this helper tab-scoped: a stale completion from a detached tab must
    // never clear a new recording attached to another page.
    void tab;
  }

  /**
   * 记录一次导航。
   *
   * 注入脚本捕获不到这些：地址栏输入、前进/后退、刷新、以及 hash 路由的
   * `pushState`——它们不产生任何 DOM 事件。而「读工单」这类流程主干**就是**
   * 导航，不记等于纯阅读的演示录出来是空的。
   *
   * 与页面事件走同一条串行队列，保证与点击、输入的先后顺序不乱。
   */
  private recordNavigation(
    owner: BrowserOwner,
    tab: BrowserTab,
    frozenCausal?: { causalId: number; capturedAt: number },
    initialPage = false,
    initialViewport?: { width: number; height: number },
    observedUrl?: string,
  ): void {
    const state = tab.recording;
    if (!state || state.paused || !state.accepting) return;
    const activeCausal = frozenCausal ?? this.activeRecorderCausal(state, tab, '');
    if (state.ledger.schemaVersion === 11) {
      const url = observedUrl || this.tabUrl(tab);
      const now = Date.now();
      state.ignoredRecordedNavigationUrls = state.ignoredRecordedNavigationUrls
        .filter((entry) => entry.expiresAt >= now);
      const ignoredIndex = state.ignoredRecordedNavigationUrls
        .findIndex((entry) => entry.url === url);
      if (ignoredIndex >= 0) {
        state.ignoredRecordedNavigationUrls.splice(ignoredIndex, 1);
        return;
      }
      const explicit = state.pendingRecordedNavigation;
      if (explicit && this.commitRecordedNavigation(tab, explicit, url)) {
        return;
      }
      const causalId = activeCausal?.causalId ?? 0;
      const transaction = (
        causalId > 0
          ? state.ledger.transactionsByCausalId.get(causalId)
          : undefined
      ) ?? state.createdByTransaction ?? undefined;
      if (initialPage && !state.openerPageId) {
        if (!initialViewport) {
          this.markRecordingStateIncomplete(state);
          return;
        }
        state.lastRecordedViewport = { ...initialViewport };
        this.appendV11HostAction(
          tab,
          state,
          { name: 'openPage', url, viewport: initialViewport },
          {
            causalId,
            url,
            ...(activeCausal ? { timestamp: activeCausal.capturedAt } : {}),
          },
        );
        return;
      }
      if (transaction) {
        this.appendV11Signal(
          tab,
          state,
          { name: 'navigation', url },
          {},
          {
            causalId,
            transaction,
            ...(activeCausal ? { timestamp: activeCausal.capturedAt } : {}),
          },
        );
        state.createdByTransaction = null;
        return;
      }
      this.appendV11HostAction(
        tab,
        state,
        { name: 'navigate', url },
        {
          causalId,
          url,
          ...(activeCausal ? { timestamp: activeCausal.capturedAt } : {}),
        },
      );
      return;
    }
    const event: RecorderEvent = {
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      provenance: {
        schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
        source: 'host-navigation',
        capturePhase: 'host',
        browserTrusted: false,
        targetEvidence: 'none',
        nativeInput: 'host',
      },
      seq: 0,
      capturedAt: activeCausal?.capturedAt ?? Date.now(),
      causalId: activeCausal?.causalId ?? 0,
      type: 'navigate',
      url: this.tabUrl(tab),
      hint: '',
      target: null,
      tier: 'plain',
      value: '',
      values: [],
      valueTruncated: false,
      key: '',
      position: null,
      dragSourcePosition: null,
      dragTargetPosition: null,
      modifiers: [],
      dialogAction: '',
      dialogType: '',
      dialogText: '',
      scrollX: 0,
      scrollY: 0,
      uploadMode: '',
      paths: [],
      fileCount: 0,
      multiple: false,
      accept: '',
      dropData: {},
    };
    state.ledger.queue = state.ledger.queue
      .then(() => this.recordStep(owner, tab, state, event, {
        sessionId: '',
        executionContextId: 0,
      }))
      .catch(() => {
        this.markRecordingStateIncomplete(state);
      });
  }

  private recordPageClosed(
    owner: BrowserOwner,
    tab: BrowserTab,
    reason: string,
    explicit: boolean,
  ): void {
    const state = tab.recording;
    if (
      !state
      || state.pageCloseRecorded
      || state.ledger.schemaVersion !== 11
      || state.paused
      || !state.accepting
    ) {
      return;
    }
    state.pageCloseRecorded = true;
    const activeCausal = this.activeRecorderCausal(state, tab, '');
    const causalId = activeCausal?.causalId ?? 0;
    let transaction: V11TransactionIdentity | undefined;
    if (explicit) {
      transaction = this.appendV11HostAction(
        tab,
        state,
        { name: 'closePage' },
        { causalId, timestamp: Date.now() },
      );
    } else {
      transaction = (
        causalId > 0
          ? state.ledger.transactionsByCausalId.get(causalId)
          : undefined
      ) ?? state.createdByTransaction ?? undefined;
    }
    this.appendV11Signal(
      tab,
      state,
      {
        name: 'x-crew-pageClosed',
        closedPageGuid: state.pageId,
        reason,
      },
      {},
      {
        causalId,
        timestamp: Date.now(),
        ...(transaction ? { transaction } : {}),
      },
    );
    // Keep the owner reference in this synchronous lifecycle hook. It also
    // proves that a foreign/detached tab can never append into another owner.
    if (this.tabsByTarget.get(tab.targetId)?.owner !== owner) {
      this.markRecordingStateIncomplete(state);
    }
  }

  /**
   * Build a persistent selector from evidence captured synchronously in the
   * trusted event callback.
   *
   * Navigation can destroy the old document before Playwright normalize/count
   * completes, and live locator probing is especially prone to stalling an
   * OOPIF Runtime channel. Prefer the same durable signals codegen prefers
   * (test id, id/name, role + accessible label) and retain the exact unique
   * CSS path only as a last resort. Replay still resolves the stored selector
   * through Playwright and rejects zero/multiple matches.
   */
  private async recorderFrameOwnerSelector(
    tab: BrowserTab,
    state: RecordingState,
    ownerSessionId: string,
    frameId: string,
  ): Promise<string> {
    const owner = await this.sendInSession(
      tab,
      ownerSessionId,
      'DOM.getFrameOwner',
      { frameId },
    );
    const backendNodeId = Number(owner?.backendNodeId ?? 0);
    if (!Number.isSafeInteger(backendNodeId) || backendNodeId <= 0) return '';
    const resolved = await this.sendInSession(
      tab,
      ownerSessionId,
      'DOM.resolveNode',
      { backendNodeId },
    );
    const objectId = String(resolved?.object?.objectId ?? '');
    if (!objectId) return '';
    try {
      const evaluated = await this.sendInSession(
        tab,
        ownerSessionId,
        'Runtime.callFunctionOn',
        {
          objectId,
          functionDeclaration: `function(controlName){
            const control = globalThis[controlName];
            return control && typeof control.selectorFor === 'function'
              ? control.selectorFor(this)
              : '';
          }`,
          arguments: [{ value: state.controlName }],
          returnByValue: true,
          awaitPromise: false,
          userGesture: false,
        },
      );
      const selector = String(evaluated?.result?.value ?? '');
      return selector
        && selector !== 'error:notconnected'
        ? selector
        : '';
    } finally {
      await this.sendInSession(
        tab,
        ownerSessionId,
        'Runtime.releaseObject',
        { objectId },
      ).catch(() => undefined);
    }
  }

  private async recorderFrameInfo(
    tab: BrowserTab,
    sessionId: string,
    frameId: string,
  ): Promise<{ id: string; parentId: string; rootId: string } | null> {
    const result = await this.sendInSession(tab, sessionId, 'Page.getFrameTree');
    const root = asOptionalRecord(result?.frameTree);
    const rootFrame = asOptionalRecord(root.frame);
    const rootId = normalizedText(rootFrame.id, 256);
    if (!rootId) return null;
    const visit = (node: Record<string, unknown>): Record<string, unknown> | null => {
      const frame = asOptionalRecord(node.frame);
      if (normalizedText(frame.id, 256) === frameId) return frame;
      const children = Array.isArray(node.childFrames) ? node.childFrames : [];
      for (const child of children) {
        if (!child || typeof child !== 'object' || Array.isArray(child)) continue;
        const found = visit(child as Record<string, unknown>);
        if (found) return found;
      }
      return null;
    };
    const found = visit(root);
    if (!found) return null;
    return {
      id: frameId,
      parentId: normalizedText(found.parentId, 256),
      rootId,
    };
  }

  /**
   * Rebuild the exact frame locator chain from the binding execution context.
   *
   * URL matching is insufficient: dashboards routinely contain several
   * same-src iframes, and an OOPIF's `window.frameElement` is intentionally
   * unavailable. CDP's context frameId + flattened parent-session topology
   * identifies the real iframe owner without guessing by URL.
   */
  private async recorderFramePath(
    tab: BrowserTab,
    state: RecordingState,
    context: RecorderExecutionContext,
    fallback: string[],
  ): Promise<{ path: string[]; nonTop: boolean; resolved: boolean }> {
    const contextKey = this.recorderContextKey(
      context.sessionId,
      context.executionContextId,
    );
    let frameId = state.contextFrames.get(contextKey) ?? '';
    let sessionId = context.sessionId;

    if (!frameId && sessionId) {
      try {
        const tree = await this.sendInSession(tab, sessionId, 'Page.getFrameTree');
        frameId = normalizedText(asOptionalRecord(asOptionalRecord(tree.frameTree).frame).id, 256);
      } catch {
        frameId = '';
      }
    }
    if (!frameId) {
      if (fallback.length) return { path: [...fallback], nonTop: true, resolved: true };
      if (sessionId) return { path: [], nonTop: true, resolved: false };
      if (context.executionContextId > 0) {
        try {
          const topCheck = await this.sendInSession(tab, '', 'Runtime.evaluate', {
            expression: 'window===window.top',
            contextId: context.executionContextId,
            returnByValue: true,
            awaitPromise: false,
          });
          if (topCheck?.result?.value === false) {
            return { path: [], nonTop: true, resolved: false };
          }
        } catch {
          // Synchronous framePath is the only remaining evidence.
        }
      }
      return { path: [], nonTop: false, resolved: true };
    }

    const path: string[] = [];
    let currentFrameId = frameId;
    const visited = new Set<string>();
    while (currentFrameId) {
      const topologyKey = `${sessionId}\u0000${currentFrameId}`;
      if (visited.has(topologyKey)) {
        return { path: [], nonTop: true, resolved: false };
      }
      visited.add(topologyKey);
      let info = await this.recorderFrameInfo(tab, sessionId, currentFrameId)
        .catch(() => null);
      let ownerSessionId = sessionId;
      if (!info && sessionId) {
        ownerSessionId = tab.childSessionParents.get(sessionId) ?? '';
        info = await this.recorderFrameInfo(tab, ownerSessionId, currentFrameId)
          .catch(() => null);
      } else if (info && sessionId && currentFrameId === info.rootId) {
        ownerSessionId = tab.childSessionParents.get(sessionId) ?? '';
        const parentInfo = await this.recorderFrameInfo(
          tab,
          ownerSessionId,
          currentFrameId,
        ).catch(() => null);
        if (parentInfo) info = parentInfo;
      }
      if (!info) return { path: [], nonTop: true, resolved: false };
      if (!info.parentId) {
        return {
          path,
          nonTop: path.length > 0 || Boolean(context.sessionId),
          resolved: true,
        };
      }
      const fragment = await this.recorderFrameOwnerSelector(
        tab,
        state,
        ownerSessionId,
        currentFrameId,
      ).catch(() => '');
      if (!fragment) return { path: [], nonTop: true, resolved: false };
      path.unshift(fragment);
      currentFrameId = info.parentId;
      sessionId = ownerSessionId;
    }
    return { path: [], nonTop: true, resolved: false };
  }

  private heuristicSelectorFor(
    event: RecorderEvent,
    framePath: string[],
    unresolvedFrame: boolean,
  ): string {
    const target = event.target;
    if (!target) return '';
    if (unresolvedFrame) return '';

    const quote = (value: string): string =>
      value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    const inFrames = (fragment: string): string =>
      [...framePath, fragment]
        .filter(Boolean)
        .join(' >> internal:control=enter-frame >> ');

    if (target.testId && target.testIdAttribute) {
      return inFrames(`[${target.testIdAttribute}="${quote(target.testId)}"]`);
    }
    if (target.id) return inFrames(`[id="${quote(target.id)}"]`);
    if (target.name) {
      const tag = target.tag || '*';
      const type = target.inputType ? `[type="${quote(target.inputType)}"]` : '';
      return inFrames(`${tag}${type}[name="${quote(target.name)}"]`);
    }

    const explicitRole = target.role;
    const inferredRole = explicitRole || (() => {
      if (target.tag === 'a' && target.href) return 'link';
      if (target.tag === 'button') return 'button';
      if (target.tag === 'select') return 'combobox';
      if (target.tag === 'textarea') return 'textbox';
      if (target.tag !== 'input') return '';
      if (target.inputType === 'checkbox') return 'checkbox';
      if (target.inputType === 'radio') return 'radio';
      if (target.inputType === 'range') return 'slider';
      if (['button', 'submit', 'reset', 'image'].includes(target.inputType)) return 'button';
      if (!['hidden'].includes(target.inputType)) return 'textbox';
      return '';
    })();
    const accessibleName = target.ariaLabel || target.text;
    if (inferredRole && accessibleName) {
      let fragment = `internal:role=${inferredRole}`;
      fragment += `[name=${JSON.stringify(accessibleName)}i]`;
      // ordinal is captured among same-tag/same-text peers. Applying it to the
      // aria-label selector is wrong because that ordinal used visible text.
      // It is commensurate only when visible text itself supplied the name.
      if (!target.ariaLabel && target.ordinal >= 1) {
        fragment += ` >> nth=${target.ordinal - 1}`;
      }
      return inFrames(fragment);
    }
    if (target.ariaLabel) {
      return inFrames(`[aria-label="${quote(target.ariaLabel)}"]`);
    }
    if (target.href && target.tag === 'a') {
      return inFrames(`a[href="${quote(target.href)}"]`);
    }
    if (inferredRole) return inFrames(`internal:role=${inferredRole}`);
    if (target.cssPath) return inFrames(target.cssPath);
    return '';
  }

  /**
   * Build a replayable selector only from evidence captured synchronously in
   * the trusted DOM event callback.
   *
   * Do not call Locator.count()/normalize() here.  On real Electron + OOPIF,
   * a Playwright Runtime request issued after the document-start recorder
   * binding can wedge indefinitely and block every later action (the recorder
   * contract catches this with keyboard activation).  A timeout race is not a
   * fix because the unresolved protocol request keeps the channel poisoned.
   *
   * Replay still constructs an official Playwright Locator from this selector,
   * so strict resolution/actionability remain Playwright-owned.
   */
  private stableSelectorFor(
    event: RecorderEvent,
    framePath: string[],
    unresolvedFrame: boolean,
    dragTarget = false,
  ): string {
    if (unresolvedFrame) return '';
    if (event.selectorSource === 'playwright') {
      const localSelector = dragTarget
        ? event.recordedDragSelector ?? ''
        : event.recordedSelector ?? '';
      if (!localSelector) return '';
      return [...framePath, localSelector]
        .filter(Boolean)
        .join(' >> internal:control=enter-frame >> ');
    }
    // Unversioned packets are accepted only for rolling-upgrade compatibility.
    // New recordings never silently fall back to Crew's selector heuristic:
    // if the installed Playwright generator is unavailable, the trace is
    // explicitly incomplete and cannot be compiled as a reusable workflow.
    if (event.schemaVersion === 1) {
      return this.heuristicSelectorFor(event, framePath, false);
    }
    return '';
  }

  /**
   * Resolve the exact File wrappers captured synchronously with upload/drop.
   *
   * The injected recorder stores `{ files: File[] | null }` for upload/drop entries.
   * It deliberately does not read fakepath, file names or contents. Each File wrapper is
   * materialised through Runtime.callFunctionOn in the event's own execution context/session,
   * then Chromium's DOM.getFileInfo returns the native path. Any missing wrapper, protocol
   * failure returns null so v10 upload becomes handoff and v11 refuses to
   * persist a partial/wrong executable file list.
   */
  private async recorderFilePaths(
    tab: BrowserTab,
    context: RecorderExecutionContext,
    stashEntryObjectId: string,
    event: RecorderEvent,
  ): Promise<string[] | null> {
    if (event.type !== 'upload' && event.type !== 'drop') return null;
    if (event.fileCount === 0) return [];
    if (!stashEntryObjectId || event.fileCount < 0) return null;

    const paths: string[] = [];
    for (let index = 0; index < event.fileCount; index += 1) {
      let fileObjectId = '';
      try {
        const file = await this.sendInSession(
          tab,
          context.sessionId,
          'Runtime.callFunctionOn',
          {
            objectId: stashEntryObjectId,
            functionDeclaration: `function(index){
              const files = this && this.files;
              return files && index >= 0 && index < files.length ? files[index] : null;
            }`,
            arguments: [{ value: index }],
            returnByValue: false,
            awaitPromise: false,
            userGesture: false,
          },
        );
        fileObjectId = typeof file?.result?.objectId === 'string'
          ? file.result.objectId
          : '';
        if (!fileObjectId) return null;
        const info = await this.sendInSession(
          tab,
          context.sessionId,
          'DOM.getFileInfo',
          { objectId: fileObjectId },
        );
        const nativePath = typeof info?.path === 'string' ? info.path : '';
        if (!nativePath) return null;
        paths.push(nativePath);
      } catch {
        return null;
      } finally {
        if (fileObjectId) {
          await this.sendInSession(
            tab,
            context.sessionId,
            'Runtime.releaseObject',
            { objectId: fileObjectId },
          ).catch(() => undefined);
        }
      }
    }
    return paths.length === event.fileCount ? paths : null;
  }

  private reserveV11Action(
    state: RecordingState,
    event: RecorderEvent,
  ): V11TransactionIdentity {
    const existing = state.ledger.reservedActions.get(event);
    if (existing) return existing;
    const identity: V11TransactionIdentity = {
      transactionId: ++state.ledger.transactionCounter,
      step: ++state.ledger.steps,
      transactionKind: 'action',
    };
    state.ledger.reservedActions.set(event, identity);
    const causalId = event.causalId ?? 0;
    if (causalId > 0) {
      state.ledger.transactionsByCausalId.set(causalId, identity);
    }
    return identity;
  }

  private reserveV11HostAction(
    state: RecordingState,
    causalId = 0,
  ): V11TransactionIdentity {
    const identity: V11TransactionIdentity = {
      transactionId: ++state.ledger.transactionCounter,
      step: ++state.ledger.steps,
      transactionKind: 'action',
    };
    if (causalId > 0) {
      state.ledger.transactionsByCausalId.set(causalId, identity);
    }
    return identity;
  }

  private reserveV11Observation(state: RecordingState): V11TransactionIdentity {
    return {
      transactionId: ++state.ledger.transactionCounter,
      step: ++state.ledger.steps,
      transactionKind: 'observation',
    };
  }

  private v11Provenance(
    source: 'document-world' | 'host-navigation' | 'browser-host',
    event?: RecorderEvent,
  ): Record<string, unknown> {
    if (event?.provenance) {
      return {
        schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
        source: event.provenance.source,
        capturePhase: event.provenance.capturePhase,
        browserTrusted: event.provenance.browserTrusted,
        targetEvidence: event.provenance.targetEvidence,
        nativeInput: event.provenance.nativeInput ?? 'unverified',
      };
    }
    return {
      schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
      source,
      capturePhase: 'host',
      browserTrusted: false,
      targetEvidence: 'none',
      nativeInput: 'host',
    };
  }

  private v11Evidence(
    event?: RecorderEvent,
    url = '',
  ): Record<string, unknown> {
    return {
      url: event?.url || url,
      hint: event?.hint ?? '',
      tier: event?.tier ?? 'plain',
      target: event?.target ?? null,
      dragTarget: event?.dragTarget ?? null,
      snapshot: '',
      snapshotDropped: false,
      backendNodeId: 0,
    };
  }

  private emitV11Row(
    tab: BrowserTab,
    state: RecordingState,
    transaction: V11TransactionIdentity,
    body: Record<string, unknown>,
    timestamp = Date.now(),
  ): void {
    const payload = {
      schemaVersion: 11,
      type: 'recording',
      targetId: tab.targetId,
      recordingId: state.ledger.recordingId,
      step: transaction.step,
      eventIndex: ++state.ledger.eventIndex,
      transactionId: transaction.transactionId,
      transactionKind: transaction.transactionKind,
      recordKind: body.recordKind,
      pageGuid: state.pageId,
      timestamp: Math.max(0, Math.trunc(timestamp)),
      provenance: body.provenance,
      ...(body.recordKind === 'action'
        ? { action: body.action, evidence: body.evidence }
        : { signal: body.signal, details: body.details }),
    };
    this.emit('recording', payload);
  }

  private appendV11HostAction(
    tab: BrowserTab,
    state: RecordingState,
    action: AtomicAction,
    options: {
      causalId?: number;
      timestamp?: number;
      url?: string;
    } = {},
  ): V11TransactionIdentity {
    const transaction = this.reserveV11HostAction(
      state,
      options.causalId ?? 0,
    );
    const chain = state.ledger.queue.then(() => {
      this.emitV11Row(
        tab,
        state,
        transaction,
        {
          recordKind: 'action',
          action,
          evidence: this.v11Evidence(undefined, options.url || this.tabUrl(tab)),
          provenance: this.v11Provenance(
            action.name === 'navigate'
              || action.name === 'openPage'
              || action.name === 'x-crew-navigate'
              ? 'host-navigation'
              : 'browser-host',
          ),
        },
        options.timestamp,
      );
    }).catch(() => {
      this.markRecordingStateIncomplete(state);
    });
    state.ledger.queue = chain;
    return transaction;
  }

  private appendV11Signal(
    tab: BrowserTab,
    state: RecordingState,
    signal: Record<string, unknown>,
    details: Record<string, unknown>,
    options: {
      causalId?: number;
      transaction?: V11TransactionIdentity | null;
      timestamp?: number;
    } = {},
  ): V11TransactionIdentity {
    const causalId = options.causalId ?? 0;
    const transaction = options.transaction
      ?? (
        causalId > 0
          ? state.ledger.transactionsByCausalId.get(causalId)
          : undefined
      )
      ?? this.reserveV11Observation(state);
    const chain = state.ledger.queue.then(() => {
      this.emitV11Row(
        tab,
        state,
        transaction,
        {
          recordKind: 'signal',
          signal,
          details,
          provenance: this.v11Provenance('browser-host'),
        },
        options.timestamp,
      );
    }).catch(() => {
      this.markRecordingStateIncomplete(state);
    });
    state.ledger.queue = chain;
    return transaction;
  }

  private v11ActionFromRecorderEvent(
    event: RecorderEvent,
    selector: string,
    targetSelector: string,
    filePaths: string[] | null,
  ): AtomicAction | null {
    if (event.type === 'click' || event.type === 'dblclick') {
      return {
        name: 'click',
        selector,
        button: event.clickButton ?? 'left',
        modifiers: event.modifiers ?? [],
        clickCount: event.type === 'dblclick'
          ? 2
          : Math.max(1, event.clickCount ?? 1),
        position: event.position ?? null,
      };
    }
    if (event.type === 'drag') {
      return {
        name: 'x-crew-drag',
        sourceSelector: selector,
        targetSelector,
        sourcePosition: event.dragSourcePosition ?? null,
        targetPosition: event.dragTargetPosition ?? null,
      };
    }
    if (event.type === 'drop') {
      if (event.fileCount > 0 && filePaths?.length !== event.fileCount) {
        return null;
      }
      return {
        name: 'x-crew-drop',
        selector,
        files: filePaths ?? [],
        data: { ...event.dropData },
      };
    }
    if (event.type === 'hover') {
      return {
        name: 'hover',
        selector,
        position: event.position ?? null,
      };
    }
    if (event.type === 'pointerGesture') {
      if (!event.gestureStart || !event.gesturePoints?.length) return null;
      return {
        name: 'x-crew-pointerGesture',
        selector,
        button: event.clickButton ?? 'left',
        modifiers: event.modifiers ?? [],
        pointerType: event.pointerType ?? 'mouse',
        start: event.gestureStart,
        points: event.gesturePoints,
      };
    }
    if (event.type === 'upload') {
      if (event.fileCount > 0 && filePaths?.length !== event.fileCount) {
        return null;
      }
      return {
        name: 'setInputFiles',
        selector,
        files: filePaths ?? [],
      };
    }
    if (event.type === 'input') {
      if (event.target?.tag === 'select') {
        return {
          name: 'select',
          selector,
          options: event.target.inputType === 'select-multiple'
            ? [...event.values]
            : [event.value],
        };
      }
      if (
        event.target?.inputType === 'checkbox'
        || event.target?.inputType === 'radio'
      ) {
        return {
          name: event.value === 'checked' ? 'check' : 'uncheck',
          selector,
        };
      }
      return { name: 'fill', selector, text: event.value };
    }
    if (event.type === 'key') {
      let key = event.key;
      const modifiers: string[] = [];
      const prefixes: Array<[string, string]> = [
        ['Ctrl+', 'Control'],
        ['Meta+', 'Meta'],
        ['Alt+', 'Alt'],
        ['Shift+', 'Shift'],
      ];
      for (const [prefix, modifier] of prefixes) {
        if (!key.startsWith(prefix)) continue;
        modifiers.push(modifier);
        key = key.slice(prefix.length);
      }
      if (!key) return null;
      return {
        name: 'press',
        selector,
        key,
        modifiers,
      };
    }
    if (event.type === 'scroll' || event.type === 'wheel') {
      if (event.scrollX === 0 && event.scrollY === 0) return null;
      return {
        name: 'x-crew-scroll',
        selector,
        deltaX: event.scrollX,
        deltaY: event.scrollY,
      };
    }
    if (event.type === 'navigate') {
      return { name: 'navigate', url: event.url };
    }
    return null;
  }

  private async recordStep(
    owner: BrowserOwner,
    tab: BrowserTab,
    state: RecordingState,
    event: RecorderEvent,
    context: RecorderExecutionContext,
  ): Promise<void> {
    // URL is frozen synchronously with the browser event. Re-reading a live
    // execution context here is racy: this queue can be delayed by selector
    // normalization while the frame redirects again, causing several distinct
    // navigation/input rows to collapse onto the final URL.
    const documentUrl = event.url;
    const backendNodeId = 0;
    const fileAction = event.type === 'upload' || event.type === 'drop';
    let filePaths: string[] | null = fileAction && event.fileCount === 0
      ? []
      : null;
    if (
      fileAction
      &&
      Number.isSafeInteger(context.executionContextId)
      && context.executionContextId > 0
    ) {
      let stashEntryObjectId = '';
      try {
        const handle = (await this.sendInSession(tab, context.sessionId, 'Runtime.evaluate', {
          expression: `(() => {
            const stash = globalThis[${JSON.stringify(state.targetStashName)}];
            if (!stash) return undefined;
            const value = stash.get(${event.seq});
            stash.delete(${event.seq});
            return value;
          })()`,
          contextId: context.executionContextId,
        })) as { result?: { objectId?: string } };
        stashEntryObjectId = typeof handle?.result?.objectId === 'string'
          ? handle.result.objectId
          : '';
        if (stashEntryObjectId) {
          filePaths = await this.recorderFilePaths(
            tab,
            context,
            stashEntryObjectId,
            event,
          );
        }
      } catch {
        // 元素可能已经从 DOM 里消失（点完就跳走）。没有 backendNodeId 也要把这一步
        // 记下来，编译期还能靠 hint 与前后快照兜底，丢掉才是真的丢信息。
        // upload 的 File wrapper 解析失败也保留同一步，只降级为 handoff。
      } finally {
        if (stashEntryObjectId) {
          await this.sendInSession(
            tab,
            context.sessionId,
            'Runtime.releaseObject',
            { objectId: stashEntryObjectId },
          ).catch(() => undefined);
        }
      }
    }
    // Do not call Playwright ariaSnapshot while recording. On real Electron 43 /
    // Chromium 150, issuing it after document-start instrumentation on a page
    // with an OOPIF can wedge the renderer Runtime channel; the next ordinary
    // locator operation then times out. Recording must never degrade the browser
    // being demonstrated. Stable selectors and synchronous target evidence are
    // sufficient for deterministic replay; page context can be captured by an
    // explicit snapshot command outside the recorder lifecycle.
    const page = '';
    const pageTruncated = false;
    // ★ 录制→复现的关键一步：把事件回调里同步算出的临时 CSS 路径升级成**可持久化
    // 的稳定选择器**。用的是 Playwright codegen 同一套评分（test id → aria role →
    // 面向用户属性 → 才轮到 CSS），跨 iframe 会自动补出 enter-frame 链。
    //
    // 失败不致命：拿不到稳定选择器就留空，编译期回退到 target 描述符 + 快照对齐的
    // 语义匹配（guided skill 那条路）。但**不能拿 cssPath 顶包写进技能**——它带
    // nth-of-type，页面一改就指到别的元素上，而技能是以后每次都要执行的。
    const frameLocation = event.target
      ? await this.recorderFramePath(
        tab,
        state,
        context,
        event.target.framePath,
      ).catch(() => ({
        path: [...event.target!.framePath],
        nonTop: Boolean(context.sessionId || event.target!.framePath.length),
        resolved: Boolean(event.target!.framePath.length),
      }))
      : { path: [] as string[], nonTop: false, resolved: true };
    const selector = this.stableSelectorFor(
      event,
      frameLocation.path,
      frameLocation.nonTop && !frameLocation.resolved,
    );
    const targetSelector = event.type === 'drag' && event.dragTarget
      ? this.stableSelectorFor(
        event,
        frameLocation.path,
        frameLocation.nonTop && !frameLocation.resolved,
        true,
      )
      : '';
    const targetRequired = [
      'click', 'dblclick', 'drag', 'drop', 'hover', 'input', 'upload', 'pointerGesture',
    ].includes(event.type);
    const selectorRequired = targetRequired
      || (
        (event.type === 'scroll' || event.type === 'wheel')
        && Boolean(event.target)
      );
    const selectorIncomplete = (targetRequired && !event.target)
      || (selectorRequired && !selector)
      || (event.type === 'drag' && (!event.dragTarget || !targetSelector));
    if (selectorIncomplete) {
      // A persisted action without its exact target cannot be replayed
      // faithfully. Keep the diagnostic row, but make the shared recording
      // explicitly incomplete so the UI never offers it as a valid skill.
      this.markRecordingStateIncomplete(state);
      this.emit('browser-error', {
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        code: 'recorder_selector_partial',
        error: frameLocation.nonTop
          ? '录制事件来自 iframe，但无法构造唯一 framePath/selector'
          : '录制动作无法构造唯一 selector',
      });
    }

    const durableUploadMode = event.type !== 'upload'
      ? ''
      : event.fileCount === 0
        ? 'clear'
        : filePaths?.length === event.fileCount
        ? 'paths'
        : 'handoff';
    if (state.ledger.schemaVersion === 11) {
      const action = this.v11ActionFromRecorderEvent(
        event,
        selector,
        targetSelector,
        filePaths,
      );
      if (!action) {
        this.markRecordingStateIncomplete(state);
        this.emit('browser-error', {
          runtimeKey: owner.runtimeKey,
          targetId: tab.targetId,
          code: 'recorder_v11_action_unrepresentable',
          error: `录制动作无法无损写入 v11：${event.type}`,
        });
        return;
      }
      const transaction = this.reserveV11Action(state, event);
      this.emitV11Row(
        tab,
        state,
        transaction,
        {
          recordKind: 'action',
          action,
          evidence: this.v11Evidence(event, documentUrl || this.tabUrl(tab)),
          provenance: this.v11Provenance('document-world', event),
        },
        event.capturedAt,
      );
      return;
    }

    state.ledger.steps += 1;
    const payload = retainRecorderEvidence({
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      recordingId: state.ledger.recordingId,
      causalId: event.causalId ?? 0,
      selector,
      targetSelector,
      // 仅在页面态变化时携带整页快照，其余步骤为空串。
      page,
      pageTruncated,
      // 信封判别位，与 debug 事件的 `type: 'debug'` 同级。动作类型放 `action`，
      // 不要复用 `type`——Crew 侧的事件分发就是按这一位走的。
      type: 'recording',
      runtimeKey: owner.runtimeKey,
      targetId: tab.targetId,
      label: state.pageId,
      openerPage: state.openerPageId,
      popupOrdinal: state.popupOrdinal,
      createdByCausalId: state.createdByCausalId,
      step: state.ledger.steps,
      action: event.type,
      // URL 取宿主侧的权威值，不用页面自报的——页面自报的 URL 是不可信输入。
      // 对 iframe 事件保存元素实际所属 document URL；顶层 webContents URL 会把
      // frame origin 错写成父页面，导致回放的 frame host attestation 永远对不上。
      url: documentUrl || event.url || this.tabUrl(tab),
      hint: event.hint,
      // 事件时刻同步取下的元素身份。跳页的点击拿不到 backendNodeId，编译期靠
      // 这一份与动作前快照对齐。
      target: event.target,
      dragTarget: event.dragTarget ?? null,
      tier: event.tier,
      value: event.value,
      values: event.values,
      valueTruncated: event.valueTruncated === true,
      key: event.key,
      clickButton: event.clickButton ?? '',
      clickCount: event.clickCount ?? 0,
      position: event.position ?? null,
      dragSourcePosition: event.dragSourcePosition ?? null,
      dragTargetPosition: event.dragTargetPosition ?? null,
      modifiers: event.modifiers ?? [],
      dialogAction: event.type === 'dialog' ? event.dialogAction : '',
      dialogType: event.type === 'dialog' ? event.dialogType : '',
      dialogText: event.type === 'dialog' ? event.dialogText : '',
      scrollX: event.scrollX,
      scrollY: event.scrollY,
      uploadMode: durableUploadMode,
      paths: (
        durableUploadMode === 'paths'
        || event.type === 'drop' && filePaths?.length === event.fileCount
      ) ? filePaths ?? [] : [],
      fileCount: fileAction ? event.fileCount : 0,
      multiple: event.type === 'upload' ? event.multiple : false,
      accept: event.type === 'upload' ? event.accept : '',
      dropData: event.type === 'drop' ? { ...event.dropData } : {},
      backendNodeId,
      timestamp: event.capturedAt ?? Date.now(),
      provenance: {
        ...(event.provenance ?? {
          schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
          source: 'document-world',
          capturePhase: 'event-callback',
          browserTrusted: true,
          targetEvidence: event.target ? 'synchronous' : 'none',
        }),
        nativeInput: event.type === 'navigate'
          ? 'host'
          : (event.provenance?.nativeInput ?? 'unverified'),
      },
    }, event.tier);
    this.emit('recording', payload);
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
    const commandTimeoutMs = expectedDialogTimeoutMs(params.command_timeout_ms);
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
    const commandTimeoutMs = expectedDialogTimeoutMs(params.command_timeout_ms);
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
    const commandTimeoutMs = expectedDialogTimeoutMs(
      params.command_timeout_ms,
      params.command_deadline_ms,
    );
    const commandDeadlineAt = Date.now() + commandTimeoutMs;
    const tab = this.targetTab(owner, params.target_id);
    const requestedDownloadDir = taskDownloadDirectory(params.download_dir);
    if (requestedDownloadDir) {
      this.setTabDownloadDir(tab, requestedDownloadDir);
    }
    if (!owner.atomicTransactions.has(tab.sessionHash)) {
      owner.atomicReplayEpochs.delete(tab.sessionHash);
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
    if (owner.expectedDialogRuns.has(tab.sessionHash)) {
      throw new BrowserHostError('该浏览器会话正在执行原子对话框动作，暂不能切换控制模式', {
        code: 'replay_dialog_busy',
      });
    }
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
      tab.nativeInputProofs = [];
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
    owner.atomicReplayEpochs.delete(tab.sessionHash);
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
    const recordingState = found?.owner === owner ? found.tab.recording : null;
    if (
      recordingState
      && recordingState.ledger.schemaVersion === 11
      && recordingState.accepting
      && !recordingState.paused
    ) {
      const activeCausal = this.activeRecorderCausal(
        recordingState,
        found!.tab,
        '',
      );
      const ordinal = (
        recordingState.ledger.downloadOrdinals.get(recordingState.pageId) ?? 0
      ) + 1;
      recordingState.ledger.downloadOrdinals.set(recordingState.pageId, ordinal);
      let suggestedFilename = '';
      try {
        suggestedFilename = item.getFilename();
      } catch {
        suggestedFilename = '';
      }
      this.appendV11Signal(
        found!.tab,
        recordingState,
        {
          name: 'download',
          downloadAlias: `d${++recordingState.ledger.downloadCounter}`,
        },
        { ordinal, suggestedFilename },
        {
          causalId: activeCausal?.causalId ?? 0,
          transaction: recordingState.createdByTransaction,
          timestamp: Date.now(),
        },
      );
    }
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
      // Return before either atomic or generic routing can call setSavePath.
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

    // Second priority: replay.v3 must retain late effects across transaction
    // gaps. Its observer both chooses the transaction path and journals the
    // item, so a true return is an exclusive ownership claim.
    if (!grant && this.observeAtomicDownload(owner, found.tab, item)) {
      owner.engine.registerNativeDownload(found.tab.view, item);
      return;
    }

    // Final fallback: every ordinary browser behavior is persisted to the
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
    if (tab.mode === 'human') {
      await this.lazyJoinHumanRecording(owner, tab, ACTION_TIMEOUT_MS);
    }
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
      for (const tab of [...owner.tabs.values()]) this.closeTab(owner, tab, false);
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
      for (const tab of [...owner.tabs.values()]) this.closeTab(owner, tab, false);
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
    recordExplicitClose = true,
  ): void {
    this.observeAtomicPageClosed(
      owner,
      tab,
      recordExplicitClose ? 'explicit' : 'lifecycle',
    );
    if (recordExplicitClose) {
      this.recordPageClosed(owner, tab, 'explicit', true);
    }
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
    const recording = tab.recording;
    if (recording) {
      // Renderer-initiated window.close can bypass the async recorder stop
      // path. Detach synchronously without touching the already-destroyed
      // WebContents; queued events retain their captured state and can still
      // commit from synchronous evidence.
      recording.accepting = false;
      recording.paused = true;
      recording.drainingFlush = false;
      recording.captureEnabled = false;
      for (const session of recording.sessions.values()) {
        session.cancelled = true;
        session.installed = false;
      }
      recording.sessions.clear();
      recording.contexts.clear();
      recording.contextFrames.clear();
      recording.ledger.members.delete(tab);
      tab.recording = null;
      tab.nativeInputProofs = [];
    }
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
