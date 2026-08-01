import { createHash, randomUUID } from 'node:crypto';
import { constants as fsConstants, realpathSync } from 'node:fs';
import { chmod, open, stat, unlink, writeFile } from 'node:fs/promises';
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
} from 'electron';

const RUNTIME_KEY_RE = /^crew_[0-9a-f]{12}$/;
const LABELED_TAB_RE = /^s([0-9a-f]{32})-([1-9][0-9]*)$/;
const NATIVE_REF_RE = /^@e([1-9][0-9]*)$/;
const GUARD_KEY_RE = /^__crew_guard_[0-9a-f]{32}$/;
const TOKEN_RE = /^[0-9a-f]{32}$/;
const MAX_RPC_ARGS = 64;
const MAX_TEXT = 30_000;
const MAX_RING_ENTRIES = 200;
const MAX_AX_NODES = 2_000;
const MAX_AX_REFS = 500;
const MAX_DOM_NODES = 50_000;
const MAX_SCREENSHOT_BYTES = 50 * 1024 * 1024;
const MAX_ARTIFACT_BYTES = 20 * 1024 * 1024;
const ARTIFACT_SCHEME = 'crew-artifact';
const MAX_TABS_PER_SESSION = 8;
const DEFAULT_DOWNLOAD_TIMEOUT_MS = 25_000;
const MAX_DOWNLOAD_TIMEOUT_MS = 300_000;
const DOWNLOAD_DEADLINE_MARGIN_MS = 500;
const DOWNLOAD_ACTION_WINDOW_MS = 5_000;
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
}

interface AutomationFocusContinuation {
  sourceOrigin: string;
  role: string;
  name: string;
  domFingerprint: string;
  expiresAt: number;
}

interface RefState {
  backendNodeId: number;
  role: string;
  name: string;
  security: string;
  navigation: string;
  pageIdentity: string;
}

interface DownloadGrant {
  tabId: string;
  target: string;
  maxBytes: number;
  claimed: boolean;
  item: DownloadItem | null;
  expectedUrl: string;
  actionActive: boolean;
  actionDeadline: number;
  eventBaseline: number;
  resolve: (value: Record<string, unknown>) => void;
  reject: (error: BrowserHostError) => void;
  timer: NodeJS.Timeout;
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
  view: WebContentsView;
  // Captured at creation: `view.webContents` is undefined after the renderer is
  // destroyed, so the 'destroyed' handler must not read `.id` off it.
  webContentsId: number;
  mode: ControlMode;
  refs: Map<string, RefState>;
  elementSecurity: Record<string, string>;
  elementNavigation: Record<string, string>;
  dialog: DialogState | null;
  console: ConsoleRecord[];
  network: NetworkRecord[];
  mouseX: number;
  mouseY: number;
  takeoverRequestAt: number;
  automationDepth: number;
  debuggerReady: Promise<void> | null;
  guardContextId: number;
  guardFrameId: string;
  guardLoaderId: string;
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
  proxy: ProxyAuthState | null;
  downloadGrant: DownloadGrant | null;
  downloadListener: DownloadListener | null;
  downloadEventSequence: number;
  artifacts: Map<string, ArtifactGrant>;
  artifactProtocolRegistered: boolean;
  lifecycle: 'active' | 'closing' | 'clearing';
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

interface CdpBoxModel {
  model?: {
    content?: number[];
    border?: number[];
    width?: number;
    height?: number;
  };
}

interface DomSecurityState {
  material: string;
  navigation: string;
  downloadNavigation: string;
  action: '' | 'submit';
}

interface IndexedDomNode {
  backendNodeId: number;
  parentBackendNodeId: number;
  nodeName: string;
  attributes: Map<string, string>;
}

interface DomDocumentIndex {
  nodes: Map<number, IndexedDomNode>;
  formsById: Map<string, IndexedDomNode>;
}

interface PreventableEvent {
  preventDefault(): void;
}

/** 逐元素可操作性检查的重试预算与退避。 */
const ACTIONABILITY_TIMEOUT_MS = 1_000;
const ACTIONABILITY_BACKOFF_MS = [20, 50, 100, 100, 200];
/** 只有「瞬时还没就绪」才重试。
 *  - target_focus_lost：联想层/异步渲染会把焦点弹一下，重新聚焦即可稳住。
 *  - not_editable 不在此列：非可编辑元素不会因为等待而变成可编辑，重试只会白等。
 *  - stale_ref_security 更不在此列：那是元素真的变了，必须立刻上报。 */
const RETRYABLE_ACTIONABILITY_CODES = new Set(['target_focus_lost']);

export class BrowserHostError extends Error {
  readonly code: string;
  readonly uncertain: boolean;
  readonly browser_stopped: boolean;
  readonly stop_unconfirmed: boolean;

  constructor(
    message: string,
    options: {
      code?: string;
      uncertain?: boolean;
      browserStopped?: boolean;
      stopUnconfirmed?: boolean;
    } = {},
  ) {
    super(message);
    this.name = 'BrowserHostError';
    this.code = options.code ?? 'browser_host_error';
    this.uncertain = options.uncertain ?? false;
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

function asString(value: unknown, label: string, max = 4096): string {
  if (typeof value !== 'string') {
    throw new BrowserHostError(`${label}必须是字符串`, { code: 'invalid_request' });
  }
  if (value.length > max) {
    throw new BrowserHostError(`${label}过长`, { code: 'invalid_request' });
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

function normalizedText(value: unknown, maximum = 500): string {
  return String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, maximum);
}

function safeUrl(value: unknown, { allowBlank = true }: { allowBlank?: boolean } = {}): string {
  const raw = asString(value, 'url', 16_384).trim();
  if (allowBlank && raw === 'about:blank') return raw;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new BrowserHostError('浏览器 URL 无效', { code: 'invalid_url' });
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new BrowserHostError('浏览器仅允许 HTTP/HTTPS 页面', { code: 'blocked_url' });
  }
  return parsed.toString();
}

function isSafePageUrl(value: string): boolean {
  try {
    safeUrl(value);
    return true;
  } catch {
    return false;
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
  try {
    const parsed = new URL(value);
    parsed.username = '';
    parsed.password = '';
    parsed.hash = '';
    const sensitive = /^(?:access_token|auth|authorization|bearer|client_secret|code|credential|id_token|key|password|refresh_token|secret|session|signature|token)$/i;
    for (const key of [...parsed.searchParams.keys()]) {
      if (sensitive.test(key)) parsed.searchParams.set(key, '***');
    }
    return parsed.toString().slice(0, 4096);
  } catch {
    return value === 'about:blank' ? value : '';
  }
}

function comparableHttpUrl(value: string): string {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
    parsed.hash = '';
    return parsed.toString();
  } catch {
    return '';
  }
}

function publicConsoleText(value: unknown): string {
  return normalizedText(value, 2000)
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, '$1***')
    .replace(
      /\b(access[_-]?token|api[_-]?key|auth(?:orization)?|client[_-]?secret|cookie|credential|id[_-]?token|password|refresh[_-]?token|secret|session|signature|token)\b(\s*[=:]\s*)(["']?)[^\s,;&"']+\3/gi,
      '$1$2***',
    )
    .replace(/\b(?:gh[opusr]_|sk-(?:live-|test-)?|xox[baprs]-)[A-Za-z0-9_-]{8,}/g, '***');
}

function downloadTimeoutMs(params: Record<string, unknown>): number {
  const candidates: number[] = [];
  if (params.timeout_ms !== undefined) {
    candidates.push(asPositiveInteger(params.timeout_ms, 'timeout_ms', MAX_DOWNLOAD_TIMEOUT_MS));
  }
  if (params.deadline_ms !== undefined) {
    const deadline = asPositiveInteger(params.deadline_ms, 'deadline_ms', Number.MAX_SAFE_INTEGER);
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

function pushBounded<T>(items: T[], item: T): void {
  items.push(item);
  if (items.length > MAX_RING_ENTRIES) items.splice(0, items.length - MAX_RING_ENTRIES);
}

function cdpValue(value: AxValue | undefined): unknown {
  return value?.value;
}

function axProperty(node: AxNode, name: string): unknown {
  return cdpValue(node.properties?.find((property) => property.name === name)?.value);
}

function axSignature(role: string, name: string): string {
  return `${role.toLocaleLowerCase()}\0${normalizedText(name).toLocaleLowerCase()}`;
}

/** 可编辑节点在 snapshot 行内附带当前 value（截断 100 字符），密码框一律省略。
 *  让模型在 type 后能直接从观察结果验证输入内容，不再盲猜"没输入上"。
 *  注意：页面内容本就不可信，value 只是页面状态的回声；密码框排除是防泄露底线。 */
export function snapshotEditableValue(node: AxNode, domNode: IndexedDomNode | undefined): string {
  const editable = axProperty(node, 'editable');
  const editableToken = typeof editable === 'string' ? editable.toLocaleLowerCase() : '';
  if (editable !== true && editableToken !== 'plaintext' && editableToken !== 'richtext') return '';
  const inputType = (domNode?.attributes.get('type') ?? '').toLocaleLowerCase();
  if (inputType === 'password') return '';
  const value = normalizedText(cdpValue(node.value), 100);
  return value ? ` value=${JSON.stringify(value)}` : '';
}

function editableValueHash(value: unknown): string {
  return createHash('sha256').update(normalizedText(value, 100), 'utf8').digest('hex');
}

function expectedSnapshotEditableValue(
  node: AxNode,
  domNode: IndexedDomNode | undefined,
  expectedValueHash: string,
): string {
  const suffix = snapshotEditableValue(node, domNode);
  if (!suffix || editableValueHash(cdpValue(node.value)) !== expectedValueHash) return '';
  return suffix;
}

function quoteSnapshot(value: string): string {
  return JSON.stringify(normalizedText(value, 500));
}

/** hit-test 实际命中元素的简短描述（tag#id.class），供错误信息定位遮挡源。 */
export function describeHitNode(node: Record<string, unknown>): string {
  const name = normalizedText(node.nodeName, 40).toLocaleLowerCase() || 'unknown';
  const attrs = Array.isArray(node.attributes) ? node.attributes.map(String) : [];
  const readAttr = (key: string): string => {
    for (let index = 0; index + 1 < attrs.length; index += 2) {
      if (attrs[index].toLocaleLowerCase() === key) return attrs[index + 1];
    }
    return '';
  };
  const id = normalizedText(readAttr('id'), 40);
  const classes = normalizedText(readAttr('class'), 80)
    .split(' ')
    .filter(Boolean)
    .slice(0, 4)
    .join('.');
  return normalizedText(`${name}${id ? `#${id}` : ''}${classes ? `.${classes}` : ''}`, 160);
}

function isActionableAxNode(node: AxNode, role: string): boolean {
  if (!node.backendDOMNodeId || node.ignored) return false;
  if (axProperty(node, 'disabled') === true) return false;
  if (axProperty(node, 'focusable') === true || axProperty(node, 'editable') === true) return true;
  return new Set([
    'button',
    'checkbox',
    'combobox',
    'gridcell',
    'link',
    'listbox',
    'menuitem',
    'menuitemcheckbox',
    'menuitemradio',
    'option',
    'radio',
    'searchbox',
    'slider',
    'spinbutton',
    'switch',
    'tab',
    'textbox',
    'treeitem',
  ]).has(role.toLocaleLowerCase());
}

function refFingerprint(
  node: AxNode,
  role: string,
  name: string,
  navigation: string,
  domSecurity = '',
): string {
  const fields = [
    String(node.backendDOMNodeId ?? 0),
    role.toLocaleLowerCase(),
    normalizedText(name).toLocaleLowerCase(),
    normalizedText(cdpValue(node.value), 200),
    normalizedText(axProperty(node, 'autocomplete'), 100),
    normalizedText(axProperty(node, 'disabled'), 100),
    normalizedText(axProperty(node, 'editable'), 100),
    normalizedText(axProperty(node, 'hasPopup'), 100),
    normalizedText(axProperty(node, 'invalid'), 100),
    normalizedText(axProperty(node, 'readonly'), 100),
    normalizedText(navigation, 1000),
    domSecurity,
  ];
  return createHash('sha256').update(fields.join('\0'), 'utf8').digest('hex');
}

function boxFromQuad(quad: number[] | undefined): Rectangle | null {
  if (!quad || quad.length < 8) return null;
  const xs = [quad[0], quad[2], quad[4], quad[6]].filter((value): value is number => Number.isFinite(value));
  const ys = [quad[1], quad[3], quad[5], quad[7]].filter((value): value is number => Number.isFinite(value));
  if (xs.length !== 4 || ys.length !== 4) return null;
  const left = Math.min(...xs);
  const top = Math.min(...ys);
  const right = Math.max(...xs);
  const bottom = Math.max(...ys);
  return { x: left, y: top, width: right - left, height: bottom - top };
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
          return { ok: true, runtime: 'electron', engine: 'WebContentsView' };
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

    if (this.panel && this.panel.tab !== tab) this.detachPanel(this.panel);
    if (this.panel?.tab !== tab) window.contentView.addChildView(tab.view);
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
      proxy: null,
      downloadGrant: null,
      downloadListener: null,
      downloadEventSequence: 0,
      artifacts: new Map(),
      artifactProtocolRegistered: false,
      lifecycle: 'active',
    };
    this.owners.set(key, owner);
    this.profileBindings.set(bindingKey, key);
    try {
      await this.applyProxy(owner, proxyUrl);
      this.configureSession(owner);
      return owner;
    } catch (error) {
      this.owners.delete(key);
      throw error;
    }
  }

  private configureSession(owner: BrowserOwner): void {
    owner.session.setPermissionCheckHandler(() => false);
    owner.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
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

  private parseProxy(proxyUrl: string): ProxyAuthState {
    if (!proxyUrl) {
      throw new BrowserHostError('浏览器必须使用本机网络策略代理', {
        code: 'proxy_required',
      });
    }
    let parsed: URL;
    try {
      parsed = new URL(proxyUrl);
    } catch {
      throw new BrowserHostError('浏览器代理配置无效', { code: 'invalid_proxy' });
    }
    if (parsed.protocol !== 'http:' || !parsed.port) {
      throw new BrowserHostError('浏览器仅接受带端口的本机 HTTP 前向代理', {
        code: 'invalid_proxy',
      });
    }
    const host = parsed.hostname.replace(/^\[|\]$/g, '').toLocaleLowerCase();
    if (!new Set(['127.0.0.1', '::1', 'localhost']).has(host)) {
      throw new BrowserHostError('浏览器代理必须绑定 loopback', { code: 'invalid_proxy' });
    }
    const port = Number(parsed.port);
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
      owner.proxy?.proxyRules === next.proxyRules &&
      owner.proxy?.username === next.username &&
      owner.proxy?.password === next.password
    ) {
      return;
    }
    try {
      await owner.session.setProxy({
        mode: 'fixed_servers',
        proxyRules: next.proxyRules,
        proxyBypassRules: '<-loopback>',
      });
      await owner.session.closeAllConnections();
    } catch (error) {
      throw new BrowserHostError(
        `无法启用浏览器网络策略代理：${error instanceof Error ? error.message : 'unknown'}`,
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
    const rawArgs = params.args ?? [];
    if (!Array.isArray(rawArgs) || rawArgs.length > MAX_RPC_ARGS) {
      throw new BrowserHostError('浏览器命令参数无效', { code: 'invalid_request' });
    }
    const args = rawArgs.map((item, index) => asString(item, `args[${index}]`, 1_000_000));

    try {
      const data = await this.executeCommand(owner, command, args, params);
      return { success: true, data };
    } catch (error) {
      if (error instanceof BrowserHostError) throw error;
      throw new BrowserHostError(error instanceof Error ? error.message : '浏览器操作失败', {
        code: 'command_failed',
        uncertain: asBoolean(params.mutating),
      });
    }
  }

  private async executeCommand(
    owner: BrowserOwner,
    command: string,
    args: string[],
    params: Record<string, unknown>,
  ): Promise<unknown> {
    if (command === 'tab') return this.tabCommand(owner, args);
    const tab = this.activeTab(owner);
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
    await this.ensureDebugger(tab);
    switch (command) {
      case 'open':
        return this.navigate(owner, tab, args[0]);
      case 'preview':
        return this.previewArtifact(owner, tab, args);
      case 'back':
        return this.history(tab, 'back');
      case 'forward':
        return this.history(tab, 'forward');
      case 'reload':
        this.clearDocumentState(tab);
        tab.view.webContents.reload();
        return {};
      case 'snapshot':
        return this.snapshot(tab, !args.includes('--compact'));
      case 'get':
        return this.getCommand(tab, args);
      case 'click':
        await this.clickRef(tab, this.ref(tab, args[0]));
        return {};
      case 'fill': {
        const fillTarget = this.ref(tab, args[0]);
        const filledState = await this.fillRef(tab, fillTarget, args[1] ?? '');
        // type+submit：填完后在同一 ref 上原子按 Enter 提交，全程在这一个 RPC 内，
        // 没有模型往返、没有页面变化能挤进 fill 与提交之间。Python 侧已用一次性审批
        // 把关。位置参数 args[2]==='--submit'，避免与用户文本（args[1]）混淆。
        //
        // 不能走 press() 的完整 assertRefCurrent——fillRef 刚把元素自身的 value 改了，
        // 而 value 是 ref 指纹的组成字段，整份指纹必然不等。但也不能因此什么都不查：
        // insertText 会同步触发页面的 input handler，页面可以在那里改写表单目的地。
        // 所以用 assertRefStillBoundForSubmit 逐项复核「除 value 外的一切」，再聚焦、回车。
        if (args[2] === '--submit') {
          await this.assertRefStillBoundForSubmit(tab, fillTarget, filledState);
          await this.send(tab, 'DOM.focus', { backendNodeId: fillTarget.backendNodeId });
          await this.assertRefFocused(tab, fillTarget);
          const enter = this.keyDescription('Enter');
          tab.visualEpoch = null;
          await this.dispatchInput(tab, 'Input.dispatchKeyEvent', { type: 'rawKeyDown', ...enter });
          await this.dispatchInput(tab, 'Input.dispatchKeyEvent', { type: 'keyUp', ...enter });
        }
        return {};
      }
      case 'scroll':
        await this.scroll(tab, args[0], Number(args[1]));
        return {};
      case 'press':
        await this.press(tab, args[0], args[1]);
        return {};
      case 'mouse':
        await this.mouse(tab, args);
        return {};
      case 'upload':
        await this.upload(tab, this.ref(tab, args[0]), args.slice(1));
        return {};
      case 'screenshot':
        return this.screenshot(tab, args, params);
      case 'console':
        return this.consoleCommand(tab, args);
      case 'network':
        return this.networkCommand(tab, args);
      case 'dialog':
        return this.dialogCommand(tab, args);
      case 'eval':
        return this.fixedEvaluation(tab, args[0] ?? '');
      default:
        throw new BrowserHostError('不支持的浏览器命令', { code: 'unsupported_command' });
    }
  }

  private async tabCommand(owner: BrowserOwner, args: string[]): Promise<Record<string, unknown>> {
    if (args.length === 1 && args[0] === 'list') {
      return {
        tabs: [...owner.tabs.values()].map((tab) => ({
          tabId: tab.tabId,
          label: tab.label,
          // Reconciliation still needs immutable ids while takeover/paused is
          // active, but page-derived metadata must not cross back to the model.
          title: tab.mode === 'ai' ? normalizedText(tab.view.webContents.getTitle(), 2048) : '',
          url: tab.mode === 'ai' ? publicUrl(tab.view.webContents.getURL() || 'about:blank') : '',
          type: 'page',
          active: owner.activeTabId === tab.tabId,
          targetId: tab.targetId,
          openerTargetId: tab.openerTargetId,
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
      owner.activeTabId = tab.tabId;
      try {
        await this.ensureDebugger(tab);
        await this.navigate(owner, tab, url);
      } catch (error) {
        this.closeTab(owner, tab);
        throw error;
      }
      return { tabId: tab.tabId, targetId: tab.targetId, label: tab.label };
    }
    if ((args[0] === 'close' || args[0] === 'close-user') && args[1]) {
      const tab = this.findTab(owner, args[1]);
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
  ): BrowserTab {
    if (
      [...owner.tabs.values()].filter((candidate) => candidate.sessionHash === tabSessionHash).length
      >= MAX_TABS_PER_SESSION
    ) {
      throw new BrowserHostError(`单会话最多允许 ${MAX_TABS_PER_SESSION} 个标签页`, {
        code: 'tab_limit',
      });
    }
    owner.tabCounter += 1;
    const view = new WebContentsView({
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
    });
    view.setVisible(false);
    view.setBounds({ x: 0, y: 0, ...DEFAULT_VIEWPORT });
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
      view,
      mode: initialMode,
      refs: new Map(),
      elementSecurity: {},
      elementNavigation: {},
      dialog: null,
      console: [],
      network: [],
      mouseX: DEFAULT_VIEWPORT.width / 2,
      mouseY: DEFAULT_VIEWPORT.height / 2,
      takeoverRequestAt: 0,
      automationDepth: 0,
      debuggerReady: null,
      guardContextId: 0,
      guardFrameId: '',
      guardLoaderId: '',
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
    this.attachTabEvents(owner, tab);
    void this.ensureDebugger(tab).catch((error: unknown) => {
      this.emit('browser-error', {
        runtimeKey: owner.runtimeKey,
        targetId: tab.targetId,
        error: error instanceof Error ? error.message.slice(0, 500) : 'debugger attach failed',
      });
    });
    this.emit('tab-created', this.publicTabEvent(owner, tab));
    return tab;
  }

  private attachTabEvents(owner: BrowserOwner, tab: BrowserTab): void {
    const contents = tab.view.webContents;
    contents.on('before-input-event', (event, input) => {
      if (tab.mode !== 'human' && tab.automationDepth === 0) {
        event.preventDefault();
        if (String((input as { type?: string }).type || '') === 'keyDown') {
          this.requestHumanInteraction(owner, tab, 'keyboard');
        }
      }
    });
    // Electron 43 类型定义未包含 before-mouse-event，但运行时与单测均依赖它。
    const mouseAwareContents = contents as typeof contents & {
      on(
        event: 'before-mouse-event',
        listener: (event: Electron.Event, input: { type?: string }) => void,
      ): typeof contents;
    };
    mouseAwareContents.on('before-mouse-event', (event, input) => {
      if (tab.mode !== 'human' && tab.automationDepth === 0) {
        event.preventDefault();
        const type = String(input.type || '');
        if (type === 'mouseDown' || type === 'mouseWheel') {
          this.requestHumanInteraction(owner, tab, 'pointer');
        }
      }
    });
    const blockUnsafeNavigation = (event: { url: string; preventDefault(): void }): void => {
      const artifactNavigationAllowed = this.isAllowedArtifactUrl(owner, tab, event.url);
      if (
        (tab.artifactToken && !artifactNavigationAllowed)
        || (!tab.artifactToken && !isSafePageUrl(event.url))
      ) {
        event.preventDefault();
      }
    };
    contents.on('will-navigate', blockUnsafeNavigation);
    contents.on('will-redirect', blockUnsafeNavigation);
    contents.on('will-frame-navigate', blockUnsafeNavigation);
    contents.setWindowOpenHandler((details) => {
      // Artifact previews are local, user-authored documents. Deny every
      // popup at the browser boundary so inline script cannot smuggle local
      // content into a navigation URL even if a CSP regression occurs.
      if (tab.artifactToken) return { action: 'deny' };
      if (!isSafePageUrl(details.url)) return { action: 'deny' };
      if (tab.mode === 'paused') return { action: 'deny' };
      if (
        [...owner.tabs.values()].filter((candidate) => candidate.sessionHash === tab.sessionHash).length
        >= MAX_TABS_PER_SESSION
      ) {
        return { action: 'deny' };
      }
      return {
        action: 'allow',
        outlivesOpener: false,
        createWindow: () => {
          const popup = this.createTab(owner, '', tab.sessionHash, tab.targetId, tab.mode);
          // Only a human-driven popup may foreground itself. In AI mode an
          // untrusted page must not steer which tab the AI's activeTab-scoped
          // commands observe/drive (prompt-injection redirect); the opener
          // session still adopts the popup by openerTargetId on its next select.
          if (tab.mode === 'human') owner.activeTabId = popup.tabId;
          this.mountHumanPopup(owner, tab, popup);
          return popup.view.webContents;
        },
      };
    });
    contents.on('console-message', (event, level, message, line, sourceId) => {
      if (tab.mode !== 'ai') return;
      const record: ConsoleRecord = {
        level: normalizedText(String(level), 20),
        message: publicConsoleText(message),
        source: publicUrl(sourceId),
        line: Number(line) || 0,
        timestamp: Date.now(),
      };
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
      _errorCode,
      _errorDescription,
      _validatedUrl,
      legacyIsMainFrame,
    ) => {
      const isMainFrame = navigationFlag(details, 'isMainFrame', legacyIsMainFrame);
      if (isMainFrame === true) {
        tab.navigationEpoch += 1;
        // ERR_ABORTED from an older main navigation can race a replacement
        // navigation. Keep pending until did-stop-loading/did-navigate proves
        // the WebContents has converged; otherwise bounded polling rejects it.
        tab.navigationPending = true;
      }
    });
    contents.on('did-stop-loading', () => {
      tab.navigationEpoch += 1;
      tab.navigationPending = false;
    });
    contents.on('login', (event, _details, authInfo, callback) => {
      if (!this.handleLogin(event, contents, authInfo, callback)) {
        event.preventDefault();
        callback();
      }
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
      this.forgetTab(owner, tab);
    });
    contents.debugger.on('detach', () => {
      tab.debuggerReady = null;
      tab.guardContextId = 0;
      tab.guardFrameId = '';
      tab.guardLoaderId = '';
    });
    contents.debugger.on('message', (_event, method: string, params: unknown) => {
      this.handleDebuggerEvent(owner, tab, method, params);
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
    this.emit('user-interaction-requested', {
      runtimeKey: owner.runtimeKey,
      label: tab.label,
      source,
    });
  }

  private publicTabEvent(owner: BrowserOwner, tab: BrowserTab): Record<string, unknown> {
    const exposePageMetadata = tab.mode === 'ai';
    return {
      runtimeKey: owner.runtimeKey,
      tabId: tab.tabId,
      targetId: tab.targetId,
      label: tab.label,
      sessionHash: tab.sessionHash,
      openerTargetId: tab.openerTargetId,
      url: exposePageMetadata ? publicUrl(tab.view.webContents.getURL() || 'about:blank') : '',
      title: exposePageMetadata ? normalizedText(tab.view.webContents.getTitle(), 2048) : '',
    };
  }

  private handleDebuggerEvent(
    owner: BrowserOwner,
    tab: BrowserTab,
    method: string,
    paramsValue: unknown,
  ): void {
    const params =
      paramsValue && typeof paramsValue === 'object' && !Array.isArray(paramsValue)
        ? (paramsValue as Record<string, unknown>)
        : {};
    if (method === 'Page.javascriptDialogOpening') {
      tab.dialog = {
        type: normalizedText(params.type, 50),
        message: tab.mode === 'ai' ? normalizedText(params.message, 2000) : '',
        defaultValue: tab.mode === 'ai' ? normalizedText(params.defaultPrompt, 1000) : '',
      };
      // Human takeover may be used for passwords or other private values. Keep
      // enough state to dismiss/accept the native dialog, but never persist or
      // forward its text while the model is paused.
      if (tab.mode === 'ai') {
        this.emit('dialog', { ...this.publicTabEvent(owner, tab), dialog: { ...tab.dialog } });
      }
      return;
    }
    if (
      method === 'Runtime.executionContextsCleared' ||
      (method === 'Runtime.executionContextDestroyed' &&
        Number(params.executionContextId) === tab.guardContextId)
    ) {
      tab.guardContextId = 0;
      tab.guardFrameId = '';
      tab.guardLoaderId = '';
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
        error: publicConsoleText(params.errorText).slice(0, 500),
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
    if (tab.view.webContents.debugger.isAttached()) return;
    if (tab.debuggerReady) return tab.debuggerReady;
    tab.debuggerReady = (async () => {
      try {
        tab.view.webContents.debugger.attach('1.3');
        await Promise.all([
          tab.view.webContents.debugger.sendCommand('Page.enable'),
          tab.view.webContents.debugger.sendCommand('DOM.enable'),
          tab.view.webContents.debugger.sendCommand('Accessibility.enable'),
          tab.view.webContents.debugger.sendCommand('Network.enable', {
            maxTotalBufferSize: 1_000_000,
            maxResourceBufferSize: 100_000,
          }),
          tab.view.webContents.debugger.sendCommand('Runtime.enable'),
          tab.view.webContents.debugger.sendCommand('Overlay.enable'),
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

  private async navigate(
    owner: BrowserOwner,
    tab: BrowserTab,
    value: unknown,
  ): Promise<Record<string, unknown>> {
    const url = safeUrl(value);
    // Trusted address-bar/tool navigation may leave an artifact preview;
    // document-originated navigation remains blocked in attachTabEvents.
    this.revokeArtifact(owner, tab);
    this.clearDocumentState(tab);
    try {
      await tab.view.webContents.loadURL(url);
      return { url: tab.view.webContents.getURL() };
    } catch (error) {
      throw new BrowserHostError(
        `页面导航失败：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'navigation_failed', uncertain: true },
      );
    }
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
        || descriptorInfo.size > MAX_ARTIFACT_BYTES
      ) {
        throw new BrowserHostError('本地 HTML 文件无效、已变化或超过 20 MB', {
          code: 'invalid_artifact',
        });
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
      await tab.view.webContents.loadURL(url);
    } catch (error) {
      this.revokeArtifact(owner, tab);
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
    tab.elementSecurity = {};
    tab.elementNavigation = {};
    tab.visualEpoch = null;
    tab.lastFilled = null;
    tab.automationFocus = null;
    tab.automationFocusPending = continuation;
    tab.guardContextId = 0;
    tab.guardFrameId = '';
    tab.guardLoaderId = '';
  }

  private async currentPageIdentity(tab: BrowserTab): Promise<string> {
    const frameTree = await this.send(tab, 'Page.getFrameTree');
    const frame = asOptionalRecord(asOptionalRecord(frameTree.frameTree).frame);
    const frameId = asString(frame.id, 'frame id', 256);
    const loaderId = typeof frame.loaderId === 'string' ? frame.loaderId.slice(0, 256) : '';
    // frameId + loaderId identify the document. A same-document history or
    // query-string update must not invalidate an exact element ref while the
    // user is answering an approval prompt. Cross-document navigation changes
    // loaderId, and assertRefCurrent still recomputes the full AX/DOM security
    // fingerprint (including resolved destinations) immediately before input.
    // If a non-conforming CDP implementation omits loaderId, retain the URL as
    // a fail-closed fallback instead of treating every document as identical.
    const documentIdentity = loaderId
      ? `${frameId}\0${loaderId}`
      : `${frameId}\0url:${tab.view.webContents.getURL() || 'about:blank'}`;
    return createHash('sha256')
      .update(documentIdentity, 'utf8')
      .digest('hex');
  }

  private async snapshot(tab: BrowserTab, full: boolean): Promise<Record<string, unknown>> {
    const pageIdentity = await this.currentPageIdentity(tab);
    const lastFilled = tab.lastFilled?.pageIdentity === pageIdentity ? tab.lastFilled : null;
    const result = (await this.send(tab, 'Accessibility.getFullAXTree', { depth: 32 })) as {
      nodes?: AxNode[];
    };
    const nodes = Array.isArray(result.nodes) ? result.nodes.slice(0, MAX_AX_NODES) : [];
    const candidates = nodes
      .map((node) => ({
        node,
        role: normalizedText(cdpValue(node.role), 100) || 'generic',
        name: normalizedText(cdpValue(node.name), 500),
      }))
      .filter(({ node, role, name }) => Boolean(name) && isActionableAxNode(node, role));
    const signatureCounts = new Map<string, number>();
    for (const candidate of candidates) {
      const key = axSignature(candidate.role, candidate.name);
      signatureCounts.set(key, (signatureCounts.get(key) ?? 0) + 1);
    }
    const domIndex = await this.domDocumentIndex(tab);
    const pageUrl = tab.view.webContents.getURL() || 'about:blank';

    tab.refs.clear();
    tab.elementSecurity = {};
    tab.elementNavigation = {};
    const lines: string[] = [];
    const seenBackendIds = new Set<number>();
    let counter = 0;
    for (const { node, role, name } of candidates) {
      const backendNodeId = Number(node.backendDOMNodeId);
      if (!backendNodeId || seenBackendIds.has(backendNodeId)) continue;
      seenBackendIds.add(backendNodeId);
      const signature = axSignature(role, name);
      const rawNavigation = normalizedText(axProperty(node, 'url'), 4096);
      let refSuffix = '';
      if (counter < MAX_AX_REFS && signatureCounts.get(signature) === 1) {
        const dom = this.domSecurityState(domIndex, backendNodeId, pageUrl);
        if (dom) {
          const navigation = rawNavigation && isSafePageUrl(rawNavigation)
            ? rawNavigation
            : dom.navigation;
          counter += 1;
          const nativeRef = `@e${counter}`;
          const security = refFingerprint(node, role, name, navigation, dom.material);
          tab.refs.set(nativeRef, {
            backendNodeId,
            role,
            name,
            security,
            navigation,
            pageIdentity,
          });
          tab.elementSecurity[signature] = security;
          if (navigation) tab.elementNavigation[signature] = navigation;
          refSuffix = ` [ref=${nativeRef}]${dom.action ? ` [action=${dom.action}]` : ''}`;
        }
      }
      lines.push(
        `- ${role} ${quoteSnapshot(name)}${backendNodeId === lastFilled?.backendNodeId ? expectedSnapshotEditableValue(node, domIndex.nodes.get(backendNodeId), lastFilled.expectedValueHash) : ''}${refSuffix}`,
      );
      if (lines.join('\n').length >= MAX_TEXT) break;
    }

    if (full && lines.length < MAX_AX_REFS) {
      for (const node of nodes) {
        const name = normalizedText(cdpValue(node.name), 500);
        const role = normalizedText(cdpValue(node.role), 100) || 'text';
        if (!name || isActionableAxNode(node, role)) continue;
        lines.push(`- ${role} ${quoteSnapshot(name)}`);
        if (lines.length >= MAX_AX_REFS || lines.join('\n').length >= MAX_TEXT) break;
      }
    }
    if (await this.currentPageIdentity(tab) !== pageIdentity) {
      this.clearDocumentState(tab);
      throw new BrowserHostError('页面在生成 snapshot 时已变化，请重新观察', {
        code: 'page_changed',
      });
    }
    // 只回显刚由自动化填写的一个字段，并且只回显一次。这样 type 能验证，
    // 页面原有的邮箱、地址、OTP 等可编辑值不会被普通 snapshot 批量读取。
    tab.lastFilled = null;
    const history = tab.view.webContents.navigationHistory;
    return {
      snapshot: lines.join('\n').slice(0, MAX_TEXT),
      url: publicUrl(tab.view.webContents.getURL() || 'about:blank'),
      title: publicConsoleText(tab.view.webContents.getTitle()),
      can_go_back: history.canGoBack(),
      can_go_forward: history.canGoForward(),
    };
  }

  private async getCommand(tab: BrowserTab, args: string[]): Promise<Record<string, unknown>> {
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
      const box = await this.elementBox(tab, this.ref(tab, args[1]));
      return { box };
    }
    if (kind === 'text') {
      const text = await this.nodeText(tab, this.ref(tab, args[1]));
      return { text };
    }
    if (kind === 'attr') {
      const attribute = args[2] ?? '';
      if (!new Set(['aria-label', 'title', 'alt', 'role', 'type']).has(attribute)) {
        throw new BrowserHostError('不允许读取该元素属性', { code: 'blocked_attribute' });
      }
      return { attribute: await this.nodeAttribute(tab, this.ref(tab, args[1]), attribute) };
    }
    throw new BrowserHostError('不支持的 get 命令', { code: 'unsupported_get' });
  }

  private async describeNode(tab: BrowserTab, ref: RefState): Promise<Record<string, unknown>> {
    const result = await this.send(tab, 'DOM.describeNode', {
      backendNodeId: ref.backendNodeId,
      depth: 0,
      pierce: true,
    });
    const node = asOptionalRecord(result?.node);
    if (!Object.keys(node).length) {
      throw new BrowserHostError('浏览器元素已离开页面', { code: 'stale_ref' });
    }
    return node;
  }

  private domAttributes(node: Record<string, unknown>): Map<string, string> {
    const values = Array.isArray(node.attributes) ? node.attributes.map(String) : [];
    const attributes = new Map<string, string>();
    let total = 0;
    for (let index = 0; index + 1 < values.length && attributes.size < 128; index += 2) {
      const name = values[index].toLocaleLowerCase().slice(0, 100);
      const value = values[index + 1].slice(0, 8_192);
      total += name.length + value.length;
      if (total > 65_536) break;
      attributes.set(name, value);
    }
    return attributes;
  }

  private focusDomFingerprint(node: Record<string, unknown>): string {
    const attributes = this.domAttributes(node);
    const fields = [String(node.nodeName ?? '').toLocaleUpperCase().slice(0, 100)];
    for (const name of ['type', 'name', 'id', 'aria-label', 'placeholder']) {
      fields.push(`${name}\0${normalizedText(attributes.get(name) ?? '', 1_000)}`);
    }
    return createHash('sha256').update(fields.join('\0'), 'utf8').digest('hex');
  }

  private async focusContinuationForRef(
    tab: BrowserTab,
    ref: RefState,
  ): Promise<AutomationFocusContinuation | null> {
    const role = normalizedText(ref.role, 100).toLocaleLowerCase();
    const name = normalizedText(ref.name, 500).toLocaleLowerCase();
    const sourceOrigin = httpOrigin(tab.view.webContents.getURL() || 'about:blank');
    if (!sourceOrigin || !role || !name) return null;
    const node = await this.describeNode(tab, ref);
    return {
      sourceOrigin,
      role,
      name,
      domFingerprint: this.focusDomFingerprint(node),
      expiresAt: Date.now() + AUTOMATION_FOCUS_CONTINUATION_MS,
    };
  }

  private async domDocumentIndex(tab: BrowserTab): Promise<DomDocumentIndex> {
    const document = await this.send(tab, 'DOM.getDocument', { depth: -1, pierce: true });
    const root = asOptionalRecord(document?.root);
    if (!Object.keys(root).length) {
      throw new BrowserHostError('无法读取页面 DOM 安全索引', { code: 'dom_tree_unavailable' });
    }
    const nodes = new Map<number, IndexedDomNode>();
    const formsById = new Map<string, IndexedDomNode>();
    const stack: Array<{ node: Record<string, unknown>; parentBackendNodeId: number }> = [
      { node: root, parentBackendNodeId: 0 },
    ];
    while (stack.length) {
      const entry = stack.pop()!;
      const backendNodeId = Number(entry.node.backendNodeId);
      const nextParent = backendNodeId || entry.parentBackendNodeId;
      if (backendNodeId && !nodes.has(backendNodeId)) {
        if (nodes.size >= MAX_DOM_NODES) {
          throw new BrowserHostError('页面 DOM 规模超过浏览器安全上限', {
            code: 'dom_tree_too_large',
          });
        }
        const indexed: IndexedDomNode = {
          backendNodeId,
          parentBackendNodeId: entry.parentBackendNodeId,
          nodeName: String(entry.node.nodeName ?? '').toLocaleUpperCase().slice(0, 100),
          attributes: this.domAttributes(entry.node),
        };
        nodes.set(backendNodeId, indexed);
        if (indexed.nodeName === 'FORM') {
          const id = indexed.attributes.get('id');
          if (id && !formsById.has(id)) formsById.set(id, indexed);
        }
      }
      for (const field of ['children', 'shadowRoots', 'pseudoElements']) {
        const children = entry.node[field];
        if (!Array.isArray(children)) continue;
        for (let index = children.length - 1; index >= 0; index -= 1) {
          const child = asOptionalRecord(children[index]);
          if (Object.keys(child).length) stack.push({ node: child, parentBackendNodeId: nextParent });
        }
      }
      for (const field of ['contentDocument', 'templateContent']) {
        const child = asOptionalRecord(entry.node[field]);
        if (Object.keys(child).length) stack.push({ node: child, parentBackendNodeId: nextParent });
      }
    }
    return { nodes, formsById };
  }

  private domSecurityState(
    index: DomDocumentIndex,
    backendNodeId: number,
    pageUrl: string,
  ): DomSecurityState | null {
    const node = index.nodes.get(backendNodeId);
    if (!node) return null;
    const own = node.attributes;
    const securityNames = new Set([
      'action',
      'disabled',
      'download',
      'enctype',
      'form',
      'formaction',
      'formenctype',
      'formmethod',
      'formnovalidate',
      'formtarget',
      'href',
      'method',
      'name',
      'novalidate',
      'readonly',
      'rel',
      'role',
      'src',
      'target',
      'type',
      'value',
    ]);
    const parts = [`tag\0${node.nodeName}`];
    for (const [name, value] of [...own].sort(([left], [right]) => left.localeCompare(right))) {
      if (securityNames.has(name) || name.startsWith('aria-') || name.startsWith('on')) {
        parts.push(`attr\0${name}\0${value}`);
      }
    }

    // A submit control's destination commonly lives on its nearest ancestor
    // <form>, not on the control itself. Bind that form's security-relevant
    // attributes without exposing their raw values outside this hash input.
    let form: IndexedDomNode | undefined = own.get('form')
      ? index.formsById.get(own.get('form')!)
      : undefined;
    let parentBackendNodeId = node.parentBackendNodeId;
    const visited = new Set<number>();
    for (
      let depth = 0;
      !form && parentBackendNodeId && depth < 32 && !visited.has(parentBackendNodeId);
      depth += 1
    ) {
      visited.add(parentBackendNodeId);
      const parent = index.nodes.get(parentBackendNodeId);
      if (!parent) break;
      if (parent.nodeName === 'FORM') {
        form = parent;
        break;
      }
      parentBackendNodeId = parent.parentBackendNodeId;
    }
    if (form) {
      parts.push('form\0present\0true');
      for (const name of ['action', 'method', 'target', 'enctype', 'novalidate', 'rel']) {
        if (form.attributes.has(name)) parts.push(`form\0${name}\0${form.attributes.get(name)}`);
      }
    }

    const tag = node.nodeName;
    const navigationRaw = (tag === 'A' || tag === 'AREA') ? (own.get('href') ?? '') : '';
    let navigation = '';
    if (navigationRaw) {
      try {
        const resolved = new URL(navigationRaw, pageUrl || 'about:blank').toString();
        if (isSafePageUrl(resolved)) navigation = resolved;
      } catch {
        // Invalid destinations still affect `material`; they are simply not
        // exposed as an actionable HTTP(S) navigation hint.
      }
    }
    const type = (own.get('type') ?? (tag === 'BUTTON' ? 'submit' : '')).toLocaleLowerCase();
    const action = (
      (tag === 'BUTTON' && type === 'submit' && Boolean(form || own.has('form') || own.has('formaction')))
      || (tag === 'INPUT' && new Set(['submit', 'image']).has(type))
    ) ? 'submit' : '';
    const downloadRaw = navigationRaw
      || own.get('formaction')
      || (action === 'submit' ? (form?.attributes.get('action') ?? pageUrl) : '');
    let downloadNavigation = '';
    if (downloadRaw) {
      try {
        const resolved = new URL(downloadRaw, pageUrl || 'about:blank').toString();
        if (isSafePageUrl(resolved)) downloadNavigation = resolved;
      } catch {
        // A download without a deterministic HTTP(S) destination cannot claim
        // a one-shot grant later.
      }
    }
    return { material: parts.join('\n'), navigation, downloadNavigation, action };
  }

  private async assertRefCurrent(tab: BrowserTab, ref: RefState): Promise<DomSecurityState> {
    const beforeIdentity = await this.currentPageIdentity(tab);
    if (!ref.pageIdentity || beforeIdentity !== ref.pageIdentity) {
      throw new BrowserHostError('页面身份已变化，snapshot ref 已失效', {
        code: 'stale_ref_security',
      });
    }
    const axResult = (await this.send(tab, 'Accessibility.getPartialAXTree', {
      backendNodeId: ref.backendNodeId,
      fetchRelatives: false,
    })) as { nodes?: AxNode[] };
    const current = axResult.nodes?.find(
      (node) => Number(node.backendDOMNodeId) === ref.backendNodeId,
    );
    const role = normalizedText(cdpValue(current?.role), 100) || 'generic';
    const name = normalizedText(cdpValue(current?.name), 500);
    if (!current || role !== ref.role || name !== ref.name || !isActionableAxNode(current, role)) {
      throw new BrowserHostError('浏览器元素语义已变化，snapshot ref 已失效', {
        code: 'stale_ref_security',
      });
    }
    const domIndex = await this.domDocumentIndex(tab);
    const pageUrl = tab.view.webContents.getURL() || 'about:blank';
    const dom = this.domSecurityState(domIndex, ref.backendNodeId, pageUrl);
    if (!dom) {
      throw new BrowserHostError('浏览器元素已离开当前 DOM', {
        code: 'stale_ref_security',
      });
    }
    const rawNavigation = normalizedText(axProperty(current, 'url'), 4096);
    const navigation = rawNavigation && isSafePageUrl(rawNavigation)
      ? rawNavigation
      : dom.navigation;
    const security = refFingerprint(current, role, name, navigation, dom.material);
    if (security !== ref.security || navigation !== ref.navigation) {
      throw new BrowserHostError('浏览器元素安全属性已变化，snapshot ref 已失效', {
        code: 'stale_ref_security',
      });
    }
    if (await this.currentPageIdentity(tab) !== beforeIdentity) {
      throw new BrowserHostError('页面在 ref 安全校验期间已变化', {
        code: 'stale_ref_security',
      });
    }
    return dom;
  }

  private async withNodeObject<T>(
    tab: BrowserTab,
    ref: RefState,
    operation: (objectId: string) => Promise<T>,
  ): Promise<T> {
    const resolved = await this.send(tab, 'DOM.resolveNode', { backendNodeId: ref.backendNodeId });
    const objectId = String(resolved?.object?.objectId ?? '');
    if (!objectId) throw new BrowserHostError('浏览器元素已失效', { code: 'stale_ref' });
    try {
      return await operation(objectId);
    } finally {
      await this.send(tab, 'Runtime.releaseObject', { objectId }).catch(() => undefined);
    }
  }

  private async nodeText(tab: BrowserTab, ref: RefState): Promise<string> {
    const result = (await this.send(tab, 'Accessibility.getPartialAXTree', {
      backendNodeId: ref.backendNodeId,
      fetchRelatives: false,
    })) as { nodes?: AxNode[] };
    const current = result.nodes?.find(
      (node) => Number(node.backendDOMNodeId) === ref.backendNodeId,
    );
    if (!current) throw new BrowserHostError('浏览器元素已失效', { code: 'stale_ref' });
    return normalizedText(cdpValue(current.name), 500);
  }

  private async nodeAttribute(tab: BrowserTab, ref: RefState, name: string): Promise<string> {
    const node = await this.describeNode(tab, ref);
    const attributes = Array.isArray(node.attributes) ? node.attributes.map(String) : [];
    for (let index = 0; index + 1 < attributes.length; index += 2) {
      if (attributes[index]?.toLocaleLowerCase() === name.toLocaleLowerCase()) {
        return normalizedText(attributes[index + 1], 500);
      }
    }
    return '';
  }

  private async elementBox(tab: BrowserTab, ref: RefState): Promise<Rectangle> {
    await this.send(tab, 'DOM.scrollIntoViewIfNeeded', { backendNodeId: ref.backendNodeId });
    let result: CdpBoxModel;
    try {
      result = (await this.send(tab, 'DOM.getBoxModel', {
        backendNodeId: ref.backendNodeId,
      })) as CdpBoxModel;
    } catch {
      throw new BrowserHostError('浏览器元素不可见或已失效', { code: 'stale_ref' });
    }
    const box = boxFromQuad(result.model?.border ?? result.model?.content);
    if (!box || box.width <= 0 || box.height <= 0) {
      throw new BrowserHostError('浏览器元素没有可交互区域', { code: 'not_interactable' });
    }
    await this.highlightTarget(tab, ref.backendNodeId);
    return box;
  }

  private async highlightTarget(tab: BrowserTab, backendNodeId: number): Promise<void> {
    try {
      await this.send(tab, 'Overlay.highlightNode', {
        backendNodeId,
        highlightConfig: {
          showInfo: false,
          showStyles: false,
          contentColor: { r: 49, g: 130, b: 246, a: 0.12 },
          borderColor: { r: 49, g: 130, b: 246, a: 0.95 },
        },
      });
      const timer = setTimeout(() => {
        if (!tab.view.webContents.isDestroyed()) {
          void this.send(tab, 'Overlay.hideHighlight').catch(() => undefined);
        }
      }, 1_200);
      timer.unref();
    } catch {
      // Highlighting is presentation only; never make the browser action fail.
    }
  }

  private async pointBlockerForRef(
    tab: BrowserTab,
    ref: RefState,
    x: number,
    y: number,
  ): Promise<string> {
    // 返回 '' 表示该点属于目标；否则返回实际命中元素的简短描述（fail-closed：
    // 探测失败也返回非空，绝不因探测异常放行点击）。描述进错误信息，让模型
    // 知道目标被什么遮挡，能自行决定先关弹层还是重新 snapshot。
    let located: Record<string, unknown>;
    try {
      located = await this.nodeForViewportLocation(tab, x, y);
    } catch {
      return '位置探测失败';
    }
    const hitBackendNodeId = Number(located.backendNodeId);
    if (!hitBackendNodeId) return '位置无命中节点';
    if (hitBackendNodeId === ref.backendNodeId) return '';

    let nodeId = Number(located.nodeId);
    if (!nodeId) {
      const pushed = await this.send(tab, 'DOM.pushNodesByBackendIdsToFrontend', {
        backendNodeIds: [hitBackendNodeId],
      }).catch(() => ({}));
      nodeId = Number(Array.isArray(pushed?.nodeIds) ? pushed.nodeIds[0] : 0);
    }
    const visited = new Set<number>();
    let blocker = '';
    for (let depth = 0; nodeId && depth < 64 && !visited.has(nodeId); depth += 1) {
      visited.add(nodeId);
      const described = await this.send(tab, 'DOM.describeNode', {
        nodeId,
        depth: 0,
        pierce: true,
      }).catch(() => ({}));
      const node = asOptionalRecord(described?.node);
      if (Number(node.backendNodeId) === ref.backendNodeId) return '';
      if (!blocker) blocker = describeHitNode(node);
      nodeId = Number(node.parentId);
    }
    return blocker || '未知遮挡元素';
  }

  /**
   * Resolve a viewport CSS point through CDP's document-coordinate hit-test.
   *
   * `DOM.getBoxModel` and native `Input.dispatchMouseEvent` use coordinates in
   * the current viewport after `DOM.scrollIntoViewIfNeeded`, while
   * `DOM.getNodeForLocation` expects a point in the main document. On narrow
   * browser panels a horizontally-overflowing target (for example Baidu's
   * search button) is scrolled into view, so passing the viewport point
   * directly makes CDP report "No node found at given location". Keep native
   * input in viewport coordinates, but add the current page offset only for
   * the security hit-test.
   */
  private async nodeForViewportLocation(
    tab: BrowserTab,
    x: number,
    y: number,
  ): Promise<Record<string, unknown>> {
    const metrics = await this.send(tab, 'Page.getLayoutMetrics');
    const viewport = asOptionalRecord(
      metrics?.cssVisualViewport
      ?? metrics?.visualViewport
      ?? metrics?.cssLayoutViewport
      ?? metrics?.layoutViewport,
    );
    const pageX = Number(viewport.pageX ?? 0);
    const pageY = Number(viewport.pageY ?? 0);
    if (
      !Number.isFinite(x)
      || !Number.isFinite(y)
      || !Number.isFinite(pageX)
      || !Number.isFinite(pageY)
    ) {
      throw new BrowserHostError('页面坐标系不可用', { code: 'hit_test_failed' });
    }
    return this.send(tab, 'DOM.getNodeForLocation', {
      // CDP requires integer document coordinates. Element centers commonly
      // end in .5, and page offsets can also be fractional under zoom.
      x: Math.round(x + pageX),
      y: Math.round(y + pageY),
      includeUserAgentShadowDOM: true,
      ignorePointerEventsNone: false,
    });
  }

  private async dispatchInput(
    tab: BrowserTab,
    method: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    tab.automationDepth += 1;
    try {
      await this.send(tab, method, params);
    } finally {
      tab.automationDepth = Math.max(0, tab.automationDepth - 1);
    }
  }

  private async clickRef(
    tab: BrowserTab,
    ref: RefState,
    beforeFinalInput?: () => void,
  ): Promise<void> {
    await this.assertRefCurrent(tab, ref);
    const box = await this.elementBox(tab, ref);
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    const blocker = await this.pointBlockerForRef(tab, ref, x, y);
    if (blocker) {
      throw new BrowserHostError(
        `点击位置未命中 snapshot 目标（实际命中 ${blocker}，目标可能被弹层遮挡；请重新 snapshot 或先关闭遮挡）`,
        { code: 'hit_test_failed' },
      );
    }
    tab.mouseX = x;
    tab.mouseY = y;
    await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
      type: 'mouseMoved',
      x,
      y,
      button: 'none',
    });
    // Hover/focus handlers can rewrite formaction/href/type without changing
    // the page guard between RPCs. Recompute the exact snapshot fingerprint in
    // this same owner queue immediately before mousePressed.
    const hoverBlocker = await this.pointBlockerForRef(tab, ref, x, y);
    if (hoverBlocker) {
      throw new BrowserHostError(
        `鼠标移动后 snapshot 目标已变化（实际命中 ${hoverBlocker}）`,
        { code: 'hit_test_failed' },
      );
    }
    // Keep the full page-identity/AX/DOM fingerprint check as the final CDP
    // observation before the irreversible input event.
    await this.assertRefCurrent(tab, ref);
    beforeFinalInput?.();
    tab.visualEpoch = null;
    let pressed = true;
    const releaseOutsideTarget = async (): Promise<void> => {
      const bounds = tab.view.getBounds();
      const outsideX = Math.max(DEFAULT_VIEWPORT.width, Number(bounds.width) || 0) + 32;
      const outsideY = Math.max(DEFAULT_VIEWPORT.height, Number(bounds.height) || 0) + 32;
      try {
        await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
          type: 'mouseMoved',
          x: outsideX,
          y: outsideY,
          button: 'none',
        });
      } finally {
        await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
          type: 'mouseReleased',
          x: outsideX,
          y: outsideY,
          button: 'left',
          buttons: 0,
          clickCount: 1,
        });
      }
    };
    try {
      try {
        await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
          type: 'mousePressed',
          x,
          y,
          button: 'left',
          buttons: 1,
          clickCount: 1,
        });
      } catch (error) {
        throw new BrowserHostError(
          `点击按下结果未知：${error instanceof Error ? error.message : 'unknown'}`,
          { code: 'input_failed', uncertain: true },
        );
      }
      // mousedown handlers run during mousePressed and can turn an innocuous
      // button into a submit control or rewrite its destination. Revalidate
      // before release/default activation. If anything changed, release away
      // from the target and report the already-sent press as uncertain.
      try {
        await this.assertRefCurrent(tab, ref);
      } catch (error) {
        await releaseOutsideTarget().catch(() => undefined);
        pressed = false;
        throw new BrowserHostError(
          `点击按下后目标安全属性发生变化：${error instanceof Error ? error.message : 'unknown'}`,
          { code: 'stale_ref_security', uncertain: true },
        );
      }
      await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
        type: 'mouseReleased',
        x,
        y,
        button: 'left',
        buttons: 0,
        clickCount: 1,
      });
      pressed = false;
    } catch (error) {
      if (error instanceof BrowserHostError) throw error;
      throw new BrowserHostError(
        `点击已发送但释放鼠标失败：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'input_failed', uncertain: true },
      );
    } finally {
      if (pressed) {
        await releaseOutsideTarget().catch(() => undefined);
      }
    }
  }

  private async assertEditableRef(tab: BrowserTab, ref: RefState): Promise<void> {
    if (!EDITABLE_AX_ROLES.has(ref.role.toLocaleLowerCase())) {
      throw new BrowserHostError('浏览器元素不是可编辑输入框', { code: 'not_editable' });
    }
    const result = (await this.send(tab, 'Accessibility.getPartialAXTree', {
      backendNodeId: ref.backendNodeId,
      fetchRelatives: false,
    })) as { nodes?: AxNode[] };
    const current = result.nodes?.find(
      (node) => Number(node.backendDOMNodeId) === ref.backendNodeId,
    );
    const role = normalizedText(cdpValue(current?.role), 100).toLocaleLowerCase();
    const editable = current ? axProperty(current, 'editable') : undefined;
    const editableToken = typeof editable === 'string' ? editable.toLocaleLowerCase() : '';
    if (
      !current
      || !EDITABLE_AX_ROLES.has(role)
      || axProperty(current, 'disabled') === true
      || axProperty(current, 'readonly') === true
      || (editable !== true && editableToken !== 'plaintext' && editableToken !== 'richtext')
    ) {
      throw new BrowserHostError('浏览器元素当前不可编辑', { code: 'not_editable' });
    }
  }

  private async assertRefFocused(tab: BrowserTab, ref: RefState): Promise<void> {
    const result = (await this.send(tab, 'Accessibility.getPartialAXTree', {
      backendNodeId: ref.backendNodeId,
      fetchRelatives: false,
    })) as { nodes?: AxNode[] };
    const current = result.nodes?.find(
      (node) => Number(node.backendDOMNodeId) === ref.backendNodeId,
    );
    if (!current || axProperty(current, 'focused') !== true) {
      throw new BrowserHostError('页面把焦点移离了 snapshot 目标，输入已拒绝', {
        code: 'target_focus_lost',
      });
    }
  }

  private async fillRef(tab: BrowserTab, ref: RefState, text: string): Promise<DomSecurityState> {
    if (text.length > 1_000_000) {
      throw new BrowserHostError('输入文本过长', { code: 'invalid_input' });
    }
    // 准备阶段有界重试：联想层/异步 hydrate 会让焦点弹一下，一次性断言会直接判死。
    await this.retryUntilActionable(async () => {
      await this.assertRefCurrent(tab, ref);
      await this.assertEditableRef(tab, ref);
      await this.send(tab, 'DOM.focus', { backendNodeId: ref.backendNodeId });
      await this.assertRefFocused(tab, ref);
      await this.assertEditableRef(tab, ref);
    });
    // Focus handlers may rewrite the target. Recompute the complete snapshot
    // fingerprint after the editable check so it is the final observation.
    // Its DomSecurityState is returned so an atomic `--submit` can re-verify the
    // form destination after insertText (which is allowed to change only value).
    const verified = await this.assertRefCurrent(tab, ref);
    await this.assertRefFocused(tab, ref);
    tab.visualEpoch = null;
    await this.dispatchInput(tab, 'Input.dispatchKeyEvent', {
      type: 'rawKeyDown',
      key: 'a',
      code: 'KeyA',
      modifiers: process.platform === 'darwin' ? 4 : 2,
      windowsVirtualKeyCode: 65,
    });
    await this.dispatchInput(tab, 'Input.dispatchKeyEvent', {
      type: 'keyUp',
      key: 'a',
      code: 'KeyA',
      modifiers: process.platform === 'darwin' ? 4 : 2,
      windowsVirtualKeyCode: 65,
    });
    // Ctrl/Cmd+A handlers can redirect focus. Never let the following
    // destructive Backspace or insertText fall through to another field.
    await this.assertEditableRef(tab, ref);
    await this.assertRefFocused(tab, ref);
    await this.dispatchInput(tab, 'Input.dispatchKeyEvent', {
      type: 'rawKeyDown',
      key: 'Backspace',
      code: 'Backspace',
      windowsVirtualKeyCode: 8,
    });
    await this.dispatchInput(tab, 'Input.dispatchKeyEvent', {
      type: 'keyUp',
      key: 'Backspace',
      code: 'Backspace',
      windowsVirtualKeyCode: 8,
    });
    await this.assertEditableRef(tab, ref);
    await this.assertRefFocused(tab, ref);
    const continuation = await this.focusContinuationForRef(tab, ref);
    // Install provenance before insertText: a synchronous form navigation can
    // begin inside the input event. The navigation handler may then downgrade
    // this exact-node proof to the bounded same-origin continuation.
    tab.automationFocus = {
      backendNodeId: ref.backendNodeId,
      pageIdentity: ref.pageIdentity,
      continuation,
    };
    tab.automationFocusPending = null;
    try {
      await this.dispatchInput(tab, 'Input.insertText', { text });
    } catch (error) {
      tab.automationFocus = null;
      tab.automationFocusPending = null;
      throw error;
    }
    tab.lastFilled = {
      backendNodeId: ref.backendNodeId,
      pageIdentity: ref.pageIdentity,
      // The next snapshot may expose a value only if the exact value sent by
      // automation is still present. A rejected beforeinput or page rewrite
      // must never turn this one-shot verification into a private-value read.
      expectedValueHash: editableValueHash(text),
    };
    return verified;
  }

  /** 有界重试「元素还没就绪」，Playwright 式退避。
   *
   *  真实页面持续在动：搜索框一聚焦就弹联想层并把焦点弹走、组件异步 hydrate 后才可
   *  编辑。对这些做一次性精确断言，等于把**正常的页面行为**变成致命错误——用户看到的
   *  「页面把焦点移离了 snapshot 目标，输入已拒绝」就是这么来的。
   *
   *  只重试「准备+校验」阶段；破坏性操作与 insertText 永远只做一次，绝不因重试而重复
   *  输入或重复提交。安全类失败（stale_ref_security）不在重试集合里：那是元素真的变了，
   *  重试既无用也不该被磨平。
   */
  private async retryUntilActionable<T>(run: () => Promise<T>): Promise<T> {
    const deadline = Date.now() + ACTIONABILITY_TIMEOUT_MS;
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await run();
      } catch (error) {
        const code = error instanceof BrowserHostError ? error.code : '';
        if (!RETRYABLE_ACTIONABILITY_CODES.has(code) || Date.now() >= deadline) throw error;
        const wait = ACTIONABILITY_BACKOFF_MS[
          Math.min(attempt, ACTIONABILITY_BACKOFF_MS.length - 1)
        ];
        await new Promise((resolve) => setTimeout(resolve, wait));
      }
    }
  }

  /** 提交前复核：除元素自身 value 外的一切都不许变。
   *
   *  不能直接复用 assertRefCurrent——fillRef 刚改了 value，而 value 是 refFingerprint
   *  的组成字段，整份指纹必然不等。但表单目的地（元素的 form/formaction/formmethod/
   *  formtarget 与祖先 <form> 的 action/method/target）全在 dom.material 里，页面完全
   *  可以在 insertText 同步触发的 input handler 里改写它，把「在搜索框回车」变成提交到
   *  另一个目的地。所以逐项复核：页面身份、role/name/可操作性、以及表单目的地。
   */
  private async assertRefStillBoundForSubmit(
    tab: BrowserTab,
    ref: RefState,
    expected: DomSecurityState,
  ): Promise<void> {
    if (await this.currentPageIdentity(tab) !== ref.pageIdentity) {
      throw new BrowserHostError('页面身份在提交前已变化，已拒绝回车提交', {
        code: 'stale_ref_security',
      });
    }
    const axResult = (await this.send(tab, 'Accessibility.getPartialAXTree', {
      backendNodeId: ref.backendNodeId,
      fetchRelatives: false,
    })) as { nodes?: AxNode[] };
    const current = axResult.nodes?.find(
      (node) => Number(node.backendDOMNodeId) === ref.backendNodeId,
    );
    const role = normalizedText(cdpValue(current?.role), 100) || 'generic';
    const name = normalizedText(cdpValue(current?.name), 500);
    if (!current || role !== ref.role || name !== ref.name || !isActionableAxNode(current, role)) {
      throw new BrowserHostError('元素语义在提交前已变化，已拒绝回车提交', {
        code: 'stale_ref_security',
      });
    }
    const domIndex = await this.domDocumentIndex(tab);
    const pageUrl = tab.view.webContents.getURL() || 'about:blank';
    const dom = this.domSecurityState(domIndex, ref.backendNodeId, pageUrl);
    if (!dom || dom.material !== expected.material || dom.navigation !== expected.navigation) {
      throw new BrowserHostError('表单目的地在提交前已变化，已拒绝回车提交', {
        code: 'stale_ref_security',
      });
    }
  }

  private async scroll(tab: BrowserTab, direction: string | undefined, pixels: number): Promise<void> {
    if (!new Set(['up', 'down', 'left', 'right']).has(direction ?? '')) {
      throw new BrowserHostError('滚动方向无效', { code: 'invalid_input' });
    }
    if (!Number.isFinite(pixels)) throw new BrowserHostError('滚动距离无效', { code: 'invalid_input' });
    const amount = Math.max(1, Math.min(5000, Math.abs(pixels)));
    const deltaX = direction === 'left' ? -amount : direction === 'right' ? amount : 0;
    const deltaY = direction === 'up' ? -amount : direction === 'down' ? amount : 0;
    tab.visualEpoch = null;
    await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
      type: 'mouseWheel',
      x: tab.mouseX,
      y: tab.mouseY,
      deltaX,
      deltaY,
    });
  }

  private keyDescription(value: string | undefined): {
    key: string;
    code: string;
    windowsVirtualKeyCode: number;
  } {
    const key = value ?? '';
    const named: Record<string, { code: string; virtual: number }> = {
      ArrowDown: { code: 'ArrowDown', virtual: 40 },
      F1: { code: 'F1', virtual: 112 },
      F2: { code: 'F2', virtual: 113 },
      F3: { code: 'F3', virtual: 114 },
      F4: { code: 'F4', virtual: 115 },
      F5: { code: 'F5', virtual: 116 },
      F6: { code: 'F6', virtual: 117 },
      F7: { code: 'F7', virtual: 118 },
      F8: { code: 'F8', virtual: 119 },
      F9: { code: 'F9', virtual: 120 },
      F10: { code: 'F10', virtual: 121 },
      F11: { code: 'F11', virtual: 122 },
      F12: { code: 'F12', virtual: 123 },
      ArrowLeft: { code: 'ArrowLeft', virtual: 37 },
      ArrowRight: { code: 'ArrowRight', virtual: 39 },
      ArrowUp: { code: 'ArrowUp', virtual: 38 },
      Backspace: { code: 'Backspace', virtual: 8 },
      Delete: { code: 'Delete', virtual: 46 },
      End: { code: 'End', virtual: 35 },
      Enter: { code: 'Enter', virtual: 13 },
      Escape: { code: 'Escape', virtual: 27 },
      Home: { code: 'Home', virtual: 36 },
      PageDown: { code: 'PageDown', virtual: 34 },
      PageUp: { code: 'PageUp', virtual: 33 },
      Space: { code: 'Space', virtual: 32 },
      Tab: { code: 'Tab', virtual: 9 },
    };
    if (named[key]) return { key: key === 'Space' ? ' ' : key, code: named[key].code, windowsVirtualKeyCode: named[key].virtual };
    if (/^[A-Za-z0-9]$/.test(key)) {
      const upper = key.toLocaleUpperCase();
      return {
        key,
        code: /[A-Z]/.test(upper) ? `Key${upper}` : `Digit${upper}`,
        windowsVirtualKeyCode: upper.charCodeAt(0),
      };
    }
    throw new BrowserHostError('按键不在安全白名单中', { code: 'invalid_input' });
  }

  private async press(
    tab: BrowserTab,
    value: string | undefined,
    refValue: string | undefined,
  ): Promise<void> {
    const key = this.keyDescription(value);
    if (value === 'Enter' && !refValue) {
      throw new BrowserHostError('Enter 必须绑定 snapshot ref', { code: 'invalid_input' });
    }
    if (refValue) {
      const ref = this.ref(tab, refValue);
      await this.assertRefCurrent(tab, ref);
      await this.send(tab, 'DOM.focus', { backendNodeId: ref.backendNodeId });
      // focus 事件可能改写表单属性；在真正派发按键前再次核对完整指纹。
      await this.assertRefCurrent(tab, ref);
      await this.assertRefFocused(tab, ref);
      await this.highlightTarget(tab, ref.backendNodeId);
    }
    tab.visualEpoch = null;
    await this.dispatchInput(tab, 'Input.dispatchKeyEvent', { type: 'rawKeyDown', ...key });
    await this.dispatchInput(tab, 'Input.dispatchKeyEvent', { type: 'keyUp', ...key });
  }

  private async mouse(tab: BrowserTab, args: string[]): Promise<void> {
    const action = args[0] ?? '';
    if (action === 'move') {
      const x = Number(args[1]);
      const y = Number(args[2]);
      if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0) {
        throw new BrowserHostError('鼠标坐标无效', { code: 'invalid_input' });
      }
      tab.mouseX = x;
      tab.mouseY = y;
      tab.visualEpoch = null;
      await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
        type: 'mouseMoved',
        x,
        y,
        button: 'none',
      });
      return;
    }
    if (action === 'down' || action === 'up') {
      const button = args[1] ?? 'left';
      const buttons = { left: 1, right: 2, middle: 4 }[button];
      if (!buttons) throw new BrowserHostError('鼠标按钮无效', { code: 'invalid_input' });
      tab.visualEpoch = null;
      await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
        type: action === 'down' ? 'mousePressed' : 'mouseReleased',
        x: tab.mouseX,
        y: tab.mouseY,
        button,
        buttons: action === 'down' ? buttons : 0,
        clickCount: 1,
      });
      return;
    }
    throw new BrowserHostError('鼠标动作无效', { code: 'invalid_input' });
  }

  private async upload(tab: BrowserTab, ref: RefState, files: string[]): Promise<void> {
    if (!files.length || files.length > 32 || files.some((file) => !path.isAbsolute(file))) {
      throw new BrowserHostError('上传文件列表无效', { code: 'invalid_upload' });
    }
    await this.assertRefCurrent(tab, ref);
    const node = await this.describeNode(tab, ref);
    const attributes = Array.isArray(node.attributes) ? node.attributes.map(String) : [];
    let inputType = '';
    for (let index = 0; index + 1 < attributes.length; index += 2) {
      if (attributes[index]?.toLocaleLowerCase() === 'type') {
        inputType = (attributes[index + 1] ?? '').toLocaleLowerCase();
        break;
      }
    }
    if (String(node.nodeName ?? '').toLocaleUpperCase() !== 'INPUT' || inputType !== 'file') {
      throw new BrowserHostError('上传目标不是文件输入框', { code: 'invalid_upload_target' });
    }
    await this.assertRefCurrent(tab, ref);
    tab.visualEpoch = null;
    await this.send(tab, 'DOM.setFileInputFiles', {
      backendNodeId: ref.backendNodeId,
      files: files.map((file) => path.resolve(file)),
    });
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
      || data.length > Math.ceil(MAX_SCREENSHOT_BYTES * 4 / 3) + 16
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)
    ) {
      throw new BrowserHostError('浏览器截图数据无效或超过大小上限', {
        code: 'invalid_screenshot',
      });
    }
    const image = Buffer.from(data, 'base64');
    const pngSignature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const width = image.length >= 24 ? image.readUInt32BE(16) : 0;
    const height = image.length >= 24 ? image.readUInt32BE(20) : 0;
    if (
      image.length > MAX_SCREENSHOT_BYTES
      || !image.subarray(0, 8).equals(pngSignature)
      || width <= 0
      || height <= 0
      || width > 32_768
      || height > 32_768
    ) {
      throw new BrowserHostError('浏览器截图不是有效的有界 PNG', {
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
      const focused = (axResult.nodes ?? []).slice(0, MAX_AX_NODES).filter(focusedEditable);
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

  private async screenshot(
    tab: BrowserTab,
    args: string[],
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const output = canonicalPath(path.resolve(asString(args.at(-1), 'screenshot path', 4096)));
    const profile = profilePath(params.profile_dir);
    if (!ensureWithin(output, path.dirname(profile))) {
      throw new BrowserHostError('截图目标不属于账号浏览器目录', { code: 'invalid_artifact_path' });
    }
    const settled = args.includes('--settled');
    const beforeSettleIdentity = await this.currentPageIdentity(tab);
    const beforeSettleUrl = tab.view.webContents.getURL() || 'about:blank';
    let focusReleased = false;
    if (settled) {
      // Never export a transient DevTools target outline. Page UI is otherwise
      // preserved unless the exact editable node focused by our last fill is
      // still active (see releaseAutomationFocusForScreenshot).
      await this.send(tab, 'Overlay.hideHighlight').catch(() => undefined);
      focusReleased = await this.releaseAutomationFocusForScreenshot(tab);
      if (focusReleased) {
        // Give focusout-driven autocomplete panels one short compositor turn
        // to close. This is bounded and independent of page timers/rAF.
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
      settled,
      focus_released: focusReleased,
    };
  }

  private consoleCommand(tab: BrowserTab, args: string[]): Record<string, unknown> {
    if (tab.mode !== 'ai') {
      tab.console.splice(0);
      return { text: '[]' };
    }
    if (args.includes('--clear')) tab.console.splice(0);
    return { text: JSON.stringify(tab.console.slice(-MAX_RING_ENTRIES)) };
  }

  private networkCommand(tab: BrowserTab, args: string[]): Record<string, unknown> {
    if (args[0] !== 'requests') {
      throw new BrowserHostError('network 仅支持 requests', { code: 'unsupported_command' });
    }
    if (tab.mode !== 'ai') {
      tab.network.splice(0);
      return { text: '[]' };
    }
    if (args.includes('--clear')) tab.network.splice(0);
    return { text: JSON.stringify(tab.network.slice(-MAX_RING_ENTRIES)) };
  }

  private async dialogCommand(tab: BrowserTab, args: string[]): Promise<Record<string, unknown>> {
    const action = args[0] ?? '';
    if (action === 'status') {
      return tab.dialog
        ? {
            hasDialog: true,
            type: tab.dialog.type,
            message: tab.dialog.message,
            defaultValue: tab.dialog.defaultValue,
          }
        : { hasDialog: false };
    }
    if (action !== 'accept' && action !== 'dismiss') {
      throw new BrowserHostError('dialog 动作无效', { code: 'invalid_dialog' });
    }
    if (!tab.dialog) throw new BrowserHostError('页面当前没有对话框', { code: 'no_dialog' });
    tab.visualEpoch = null;
    await this.send(tab, 'Page.handleJavaScriptDialog', {
      accept: action === 'accept',
      ...(action === 'accept' && args[1] ? { promptText: args[1].slice(0, 10_000) } : {}),
    });
    tab.dialog = null;
    return {};
  }

  private async fixedEvaluation(tab: BrowserTab, expression: string): Promise<Record<string, unknown>> {
    const match = /elementFromPoint\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)/.exec(
      expression,
    );
    if (!match) {
      throw new BrowserHostError('Electron 浏览器禁止任意 JavaScript', {
        code: 'javascript_not_exposed',
      });
    }
    const x = Number(match[1]);
    const y = Number(match[2]);
    if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0) {
      return { value: 'null' };
    }
    let located: Record<string, unknown>;
    try {
      located = await this.nodeForViewportLocation(tab, x, y);
    } catch {
      return { value: 'null' };
    }
    const backendNodeId = Number(located.backendNodeId);
    if (!backendNodeId) return { value: 'null' };
    const temporary: RefState = {
      backendNodeId,
      role: '',
      name: '',
      security: '',
      navigation: '',
      pageIdentity: '',
    };
    const node = await this.describeNode(tab, temporary);
    let box: Rectangle | null = null;
    try {
      box = await this.elementBox(tab, temporary);
    } catch {
      box = null;
    }
    const attributes = Array.isArray(node.attributes) ? node.attributes.map(String) : [];
    const attribute = (name: string): string => {
      for (let index = 0; index + 1 < attributes.length; index += 2) {
        if (attributes[index]?.toLocaleLowerCase() === name) return attributes[index + 1] ?? '';
      }
      return '';
    };
    return {
      value: JSON.stringify({
        tag: normalizedText(node.nodeName, 50),
        role: normalizedText(attribute('role'), 100),
        name: normalizedText(attribute('aria-label') || (await this.nodeText(tab, temporary)), 160),
        box: box ? [box.x, box.y, box.width, box.height] : [],
      }),
    };
  }

  private async securitySurface(tab: BrowserTab): Promise<{
    digest: string;
    elementSecurity: Record<string, string>;
    elementNavigation: Record<string, string>;
  }> {
    const result = (await this.send(tab, 'Accessibility.getFullAXTree', { depth: 32 })) as {
      nodes?: AxNode[];
    };
    const nodes = Array.isArray(result.nodes) ? result.nodes.slice(0, MAX_AX_NODES) : [];
    const candidates = nodes
      .map((node) => ({
        node,
        role: normalizedText(cdpValue(node.role), 100) || 'generic',
        name: normalizedText(cdpValue(node.name), 500),
      }))
      .filter(({ node, role, name }) => Boolean(name) && isActionableAxNode(node, role));
    const counts = new Map<string, number>();
    for (const item of candidates) {
      const key = axSignature(item.role, item.name);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    const domIndex = await this.domDocumentIndex(tab);
    const pageUrl = tab.view.webContents.getURL() || 'about:blank';
    const elementSecurity: Record<string, string> = {};
    const elementNavigation: Record<string, string> = {};
    const seenBackendIds = new Set<number>();
    let counter = 0;
    for (const { node, role, name } of candidates) {
      const backendNodeId = Number(node.backendDOMNodeId);
      if (
        !backendNodeId
        || seenBackendIds.has(backendNodeId)
        || counter >= MAX_AX_REFS
      ) continue;
      seenBackendIds.add(backendNodeId);
      const key = axSignature(role, name);
      if (counts.get(key) !== 1) continue;
      const dom = this.domSecurityState(domIndex, backendNodeId, pageUrl);
      if (!dom) continue;
      const rawNavigation = normalizedText(axProperty(node, 'url'), 4096);
      const navigation = rawNavigation && isSafePageUrl(rawNavigation)
        ? rawNavigation
        : dom.navigation;
      elementSecurity[key] = refFingerprint(node, role, name, navigation, dom.material);
      if (navigation) elementNavigation[key] = navigation;
      counter += 1;
    }
    const digest = createHash('sha256')
      .update(
        Object.entries(elementSecurity)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, value]) => `${key}\0${value}`)
          .join('\n'),
        'utf8',
      )
      .digest('hex');
    return { digest, elementSecurity, elementNavigation };
  }

  private async pageGuard(key: string, params: Record<string, unknown>): Promise<string> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    await this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim());
    const tab = this.targetTab(owner, params.target_id);
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止读取页面守卫状态', {
        code: 'control_mode_blocked',
      });
    }
    await this.ensureDebugger(tab);
    const stateKey = asString(params.state_key, 'state_key', 100);
    const stateToken = asString(params.state_token, 'state_token', 100);
    if (!GUARD_KEY_RE.test(stateKey) || !TOKEN_RE.test(stateToken)) {
      throw new BrowserHostError('页面守卫标识无效', { code: 'invalid_guard' });
    }
    const frameTree = await this.send(tab, 'Page.getFrameTree');
    const frame = asOptionalRecord(asOptionalRecord(frameTree.frameTree).frame);
    const frameId = asString(frame.id, 'frame id', 256);
    const loaderId = typeof frame.loaderId === 'string' ? frame.loaderId.slice(0, 256) : '';
    if (
      !tab.guardContextId ||
      tab.guardFrameId !== frameId ||
      tab.guardLoaderId !== loaderId
    ) {
      const world = await this.send(tab, 'Page.createIsolatedWorld', {
        frameId,
        worldName: '__crew_browser_host_guard',
        grantUniveralAccess: false,
      });
      tab.guardContextId = Number(world.executionContextId);
      tab.guardFrameId = frameId;
      tab.guardLoaderId = loaderId;
    }
    const contextId = tab.guardContextId;
    if (!Number.isSafeInteger(contextId) || contextId <= 0) {
      throw new BrowserHostError('无法创建页面隔离守卫', { code: 'guard_unavailable' });
    }
    const reset = asBoolean(params.reset);
    const includeSecurity = asBoolean(params.include_security, true);
    const keyLiteral = JSON.stringify(stateKey);
    const tokenLiteral = JSON.stringify(stateToken);
    const expression = reset
      ? `(()=>{const k=${keyLiteral};const old=globalThis[k];if(old?.observer)old.observer.disconnect();const s={token:${tokenLiteral},counter:0,observer:null};s.observer=new MutationObserver(()=>{s.counter+=1});s.observer.observe(document,{subtree:true,childList:true,attributes:true,characterData:true});Object.defineProperty(globalThis,k,{value:s,configurable:true});return {token:s.token,counter:s.counter,href:location.href,timeOrigin:performance.timeOrigin,scrollX:scrollX,scrollY:scrollY,width:innerWidth,height:innerHeight,dpr:devicePixelRatio}})()`
      : `(()=>{const s=globalThis[${keyLiteral}]||{token:'',counter:-1};return {token:s.token,counter:s.counter,href:location.href,timeOrigin:performance.timeOrigin,scrollX:scrollX,scrollY:scrollY,width:innerWidth,height:innerHeight,dpr:devicePixelRatio}})()`;
    const result = await this.send(tab, 'Runtime.evaluate', {
      expression,
      contextId,
      returnByValue: true,
      awaitPromise: false,
      userGesture: false,
    });
    const marker = asOptionalRecord(result?.result?.value);
    // Stability polling asks only for the host/page transition marker. Avoid
    // repeatedly walking AX + flattened DOM while waiting for a navigation to
    // become quiet; the final capture guard still requests the full surface.
    const security = includeSecurity ? await this.securitySurface(tab) : null;
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
      ...(security
        ? {
            securityDigest: security.digest,
            elementSecurity: security.elementSecurity,
            elementNavigation: security.elementNavigation,
          }
        : {}),
    });
  }

  private async pageImages(key: string, params: Record<string, unknown>): Promise<Record<string, string>[]> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    await this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim());
    const tab = this.targetTab(owner, params.target_id);
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止读取页面图片', {
        code: 'control_mode_blocked',
      });
    }
    const frameTree = await this.send(tab, 'Page.getFrameTree');
    const frame = asOptionalRecord(asOptionalRecord(frameTree.frameTree).frame);
    const frameId = asString(frame.id, 'frame id', 256);
    const world = await this.send(tab, 'Page.createIsolatedWorld', {
      frameId,
      worldName: '__crew_browser_host_images',
      grantUniveralAccess: false,
    });
    const result = await this.send(tab, 'Runtime.evaluate', {
      contextId: Number(world.executionContextId),
      expression:
        "Array.from(document.images).slice(0,100).map(img=>({src:String(img.currentSrc||img.src||'').slice(0,4096),alt:String(img.alt||'').slice(0,500),width:String(img.naturalWidth||img.width||0),height:String(img.naturalHeight||img.height||0)}))",
      returnByValue: true,
      awaitPromise: false,
      userGesture: false,
    });
    const rows = result?.result?.value;
    if (!Array.isArray(rows)) return [];
    return rows.slice(0, 100).map((row: unknown) => {
      const item = asOptionalRecord(row);
      return {
        src: publicUrl(String(item.src ?? '')),
        alt: normalizedText(item.alt, 500),
        width: normalizedText(item.width, 20),
        height: normalizedText(item.height, 20),
      };
    });
  }

  private async coordinateClick(
    key: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    await this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim());
    const tab = this.targetTab(owner, params.target_id);
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止坐标点击', {
        code: 'control_mode_blocked',
      });
    }
    const expectedEpoch = typeof params.expected_epoch === 'string'
      ? params.expected_epoch.slice(0, 100)
      : '';
    const visualEpoch = tab.visualEpoch;
    if (!TOKEN_RE.test(expectedEpoch) || !visualEpoch || visualEpoch.token !== expectedEpoch) {
      throw new BrowserHostError('视觉截图 Host epoch 已失效，请重新截图', {
        code: 'invalid_visual_epoch',
      });
    }
    if (await this.currentPageIdentity(tab) !== visualEpoch.pageIdentity) {
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
    const metrics = await this.send(tab, 'Page.getLayoutMetrics');
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

    const located = await this.nodeForViewportLocation(tab, x, y)
      .catch((): Record<string, unknown> => ({}));
    const backendNodeId = Number(located.backendNodeId);
    if (!backendNodeId) {
      throw new BrowserHostError('DOM hit-test 无法确定坐标目标', { code: 'hit_test_failed' });
    }
    const ref: RefState = {
      backendNodeId,
      role: '',
      name: '',
      security: '',
      navigation: '',
      pageIdentity: '',
    };
    const node = await this.describeNode(tab, ref);
    const tag = String(node.nodeName ?? '').toLocaleUpperCase();
    const attributes = this.domAttributes(node);
    const inputType = (attributes.get('type') ?? '').toLocaleLowerCase();
    const axResult = (await this.send(tab, 'Accessibility.getPartialAXTree', {
      backendNodeId,
      fetchRelatives: false,
    })) as { nodes?: AxNode[] };
    const axNode = axResult.nodes?.find(
      (candidate) => Number(candidate.backendDOMNodeId) === backendNodeId,
    );
    const role = normalizedText(cdpValue(axNode?.role) ?? attributes.get('role'), 100);
    const name = normalizedText(
      cdpValue(axNode?.name) ?? attributes.get('aria-label') ?? attributes.get('title'),
      160,
    );
    const standardInteractive = new Set([
      'BUTTON',
      'INPUT',
      'LABEL',
      'OPTION',
      'SELECT',
      'SUMMARY',
      'TEXTAREA',
    ]).has(tag) || ((tag === 'A' || tag === 'AREA') && attributes.has('href'));
    const actionableAx = axNode ? isActionableAxNode(axNode, role) : false;
    if (
      new Set(['HTML', 'BODY']).has(tag)
      || attributes.has('disabled')
      || attributes.has('readonly')
      || axProperty(axNode ?? {}, 'disabled') === true
      || axProperty(axNode ?? {}, 'readonly') === true
      || (tag === 'INPUT' && inputType === 'hidden')
      || (!standardInteractive && tag !== 'CANVAS' && !actionableAx)
    ) {
      throw new BrowserHostError('坐标目标缺少可确认的交互语义', {
        code: 'not_interactable',
      });
    }
    let model: CdpBoxModel;
    try {
      model = (await this.send(tab, 'DOM.getBoxModel', { backendNodeId })) as CdpBoxModel;
    } catch {
      throw new BrowserHostError('坐标目标不可见或已失效', { code: 'not_interactable' });
    }
    const box = boxFromQuad(model.model?.border ?? model.model?.content);
    if (
      !box
      || box.width <= 0
      || box.height <= 0
      || x < box.x
      || y < box.y
      || x > box.x + box.width
      || y > box.y + box.height
    ) {
      throw new BrowserHostError('坐标目标的交互区域已变化', { code: 'hit_test_failed' });
    }

    const currentScreenshot = await this.captureScreenshotPng(tab);
    if (
      currentScreenshot.hash !== visualEpoch.screenshotHash
      || currentScreenshot.width !== visualEpoch.width
      || currentScreenshot.height !== visualEpoch.height
      || await this.currentPageIdentity(tab) !== visualEpoch.pageIdentity
    ) {
      tab.visualEpoch = null;
      throw new BrowserHostError('当前画面与已批准截图不一致，请重新截图', {
        code: 'invalid_visual_epoch',
      });
    }

    tab.mouseX = x;
    tab.mouseY = y;
    // A visual approval is strictly one-shot, including uncertain input errors.
    tab.visualEpoch = null;
    try {
      await this.dispatchInput(tab, 'Input.dispatchMouseEvent', {
        type: 'mouseMoved', x, y, button: 'none',
      });
    } catch (error) {
      throw new BrowserHostError(
        `坐标鼠标移动失败：${error instanceof Error ? error.message : 'unknown'}`,
        { code: 'input_failed' },
      );
    }
    const coordinateBlocker = await this.pointBlockerForRef(tab, ref, x, y);
    if (coordinateBlocker) {
      throw new BrowserHostError(
        `鼠标移动后坐标目标已变化（实际命中 ${coordinateBlocker}）`,
        { code: 'hit_test_failed' },
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
        });
      } catch (error) {
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
        });
        pressed = false;
      } catch (error) {
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
        }).catch(() => undefined);
      }
    }
    return { clicked: true, target: { tag: tag.slice(0, 50), role, name } };
  }

  private async setMode(key: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const owner = this.requireOwner(key);
    this.verifyProfileIfPresent(owner, params.profile_dir);
    const tab = this.targetTab(owner, params.target_id);
    const mode = normalizeMode(params.mode);
    for (const candidate of owner.tabs.values()) {
      if (candidate.sessionHash === tab.sessionHash) this.setTabMode(candidate, mode);
    }
    return { mode };
  }

  private setTabMode(tab: BrowserTab, mode: ControlMode): void {
    if (tab.mode !== mode) {
      tab.visualEpoch = null;
      // A pending AI fill must not authorize exposing a value entered later by
      // a human, and a human-era value must never survive return-to-AI.
      tab.lastFilled = null;
      tab.automationFocus = null;
      tab.automationFocusPending = null;
    }
    if (mode !== 'ai' || tab.mode !== 'ai') {
      tab.console.splice(0);
      tab.network.splice(0);
    }
    if (mode !== 'ai' && tab.dialog) {
      tab.dialog.message = '';
      tab.dialog.defaultValue = '';
    }
    tab.mode = mode;
    tab.takeoverRequestAt = 0;
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
    panel.window.contentView.addChildView(popup.view);
    this.detachPanel(panel);
    popup.view.setVisible(true);
    this.panel = { ...panel, tab: popup };
    popup.view.webContents.focus();
  }

  private popupDescendsFrom(owner: BrowserOwner, popup: BrowserTab, ancestor: BrowserTab): boolean {
    const visited = new Set<string>();
    let openerTargetId = popup.openerTargetId;
    for (let depth = 0; openerTargetId && depth < MAX_TABS_PER_SESSION; depth += 1) {
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
    await this.applyProxy(owner, asString(params.proxy_url, 'proxy_url', 4096).trim());
    const tab = this.targetTab(owner, params.target_id);
    if (tab.mode !== 'ai') {
      throw new BrowserHostError('人工接管或暂停期间禁止发起自动下载', {
        code: 'control_mode_blocked',
      });
    }
    const ref = this.ref(tab, asString(params.ref, 'ref', 100));
    const rawTarget = asString(params.target, 'download target', 4096);
    const rawQuarantine = asString(params.download_dir, 'download_dir', 4096);
    if (!path.isAbsolute(rawTarget) || !path.isAbsolute(rawQuarantine)) {
      throw new BrowserHostError('下载路径必须是绝对路径', { code: 'invalid_download_path' });
    }
    const target = canonicalPath(rawTarget);
    const quarantine = canonicalPath(rawQuarantine);
    const browserRoot = path.dirname(owner.profilePath);
    const approvedRoot = path.join(browserRoot, 'approved-downloads');
    const expectedQuarantine = path.join(browserRoot, 'download-quarantine');
    if (
      !ensureWithin(target, approvedRoot)
      || samePath(target, approvedRoot)
      || !samePath(quarantine, expectedQuarantine)
    ) {
      throw new BrowserHostError('下载目标不属于账号隔离目录', { code: 'invalid_download_path' });
    }
    if (owner.downloadGrant) {
      throw new BrowserHostError('账号已有进行中的下载', { code: 'download_busy' });
    }
    const maxBytes = asPositiveInteger(params.max_bytes, 'max_bytes', 2_147_483_647);
    const timeoutMs = downloadTimeoutMs(params);
    // Validate cheap, caller-controlled path/budget fields before observing the
    // page. The ref fingerprint and deterministic download destination are then
    // recomputed inside this Host command and clickRef repeats the same check
    // immediately before the mouse press.
    const currentRef = await this.assertRefCurrent(tab, ref);
    const expectedUrl = comparableHttpUrl(currentRef.downloadNavigation);
    if (!expectedUrl) {
      throw new BrowserHostError('下载 ref 无法绑定确定的 HTTP(S) 请求目标', {
        code: 'download_target_unbound',
      });
    }
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
      }, timeoutMs);
      timer.unref();
      owner.downloadGrant = {
        tabId: tab.tabId,
        target,
        maxBytes,
        claimed: false,
        item: null,
        expectedUrl,
        actionActive: false,
        actionDeadline: 0,
        eventBaseline: owner.downloadEventSequence,
        resolve,
        reject,
        timer,
      };
    });
    try {
      await this.clickRef(tab, ref, () => {
        const grant = owner.downloadGrant;
        if (!grant || grant.target !== target) {
          throw new BrowserHostError('下载授权在点击前已失效', {
            code: 'download_grant_expired',
          });
        }
        grant.eventBaseline = owner.downloadEventSequence;
        grant.actionActive = true;
        grant.actionDeadline = Date.now() + Math.min(timeoutMs, DOWNLOAD_ACTION_WINDOW_MS);
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

  private handleWillDownload(
    owner: BrowserOwner,
    event: ElectronEvent,
    item: DownloadItem,
    contents: WebContents,
  ): void {
    owner.downloadEventSequence += 1;
    const eventSequence = owner.downloadEventSequence;
    const grant = owner.downloadGrant;
    const found = this.tabsByWebContentsId.get(contents.id);
    let itemUrls: string[] = [];
    try {
      itemUrls = [item.getURL(), ...item.getURLChain()]
        .map((value) => comparableHttpUrl(String(value ?? '')))
        .filter(Boolean);
    } catch {
      itemUrls = [];
    }
    const actionExpired = Boolean(grant?.actionActive && Date.now() > grant.actionDeadline);
    const matchesAction = Boolean(
      grant
      && grant.actionActive
      && !actionExpired
      && eventSequence > grant.eventBaseline
      && itemUrls.includes(grant.expectedUrl),
    );
    if (
      !grant
      || grant.claimed
      || !found
      || found.owner !== owner
      || found.tab.tabId !== grant.tabId
      || !matchesAction
    ) {
      event.preventDefault();
      item.cancel();
      if (grant && actionExpired) {
        void this.cancelDownloadGrant(
          owner,
          new BrowserHostError('下载事件未在已绑定点击窗口内开始', {
            code: 'download_action_expired',
          }),
        ).catch(() => undefined);
      }
      return;
    }
    grant.claimed = true;
    grant.actionActive = false;
    grant.item = item;
    const totalBytes = item.getTotalBytes();
    if (totalBytes > 0 && totalBytes > grant.maxBytes) {
      event.preventDefault();
      void this.cancelDownloadGrant(
        owner,
        new BrowserHostError('下载文件超过大小限制', {
          code: 'download_too_large',
          uncertain: true,
        }),
      );
      return;
    }
    item.setSavePath(grant.target);
    item.on('updated', () => {
      if (item.getReceivedBytes() <= grant.maxBytes) return;
      void this.cancelDownloadGrant(
        owner,
        new BrowserHostError('下载文件超过大小限制', {
          code: 'download_too_large',
          uncertain: true,
        }),
      );
    });
    item.once('done', (_doneEvent, state) => {
      if (owner.downloadGrant !== grant) return;
      clearTimeout(grant.timer);
      owner.downloadGrant = null;
      if (state !== 'completed' || item.getReceivedBytes() > grant.maxBytes) {
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
      await owner.session.closeAllConnections().catch(() => undefined);
    } finally {
      // session.fromPath() returns the same Session object after an idle/close
      // restart. Removing the owner-bound listener prevents a stale listener
      // from cancelling the next owner's one-shot download grant.
      this.detachSession(owner);
    }
    if (cleanupError) throw cleanupError;
  }

  private closeTab(owner: BrowserOwner, tab: BrowserTab): void {
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
    owner.tabs.delete(tab.tabId);
    this.tabsByTarget.delete(tab.targetId);
    this.tabsByWebContentsId.delete(tab.webContentsId);
    if (owner.activeTabId === tab.tabId) {
      owner.activeTabId = owner.tabs.keys().next().value ?? '';
    }
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

  private detachPanel(panel: { tab: BrowserTab; window: BrowserWindow }): void {
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
