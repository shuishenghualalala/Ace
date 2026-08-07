/**
 * Crew 动作面：snapshot/locate ref → exact strict Playwright Locator → dispatch。
 *
 * 普通可变更动作遵守 Playwright MCP 同一套 target-locator 语义：
 *
 *   ref 定位 → 唯一匹配 → Playwright 官方 actionability → dispatch
 *
 * owner/tab 拓扑与控制模式负责路由和用户接管；动态页面不会因为辅助功能名称或
 * DOM 指纹的普通变化被额外判成 stale_ref_security。
 */

import { locatorFromRef } from './playwright-compat';

import type {
  CDPSession,
  FileChooser,
  Locator,
  Page,
  Request,
} from './playwright-compat';
import type { RefRecord } from './playwright-snapshot';

export type ActionPhase = 'pre_dispatch' | 'dispatching' | 'partial';

export interface ActionErrorDetails {
  phase?: ActionPhase;
  /** true 表示 Playwright mutation 已被调用，无法证明页面是否收到全部事件。 */
  uncertain?: boolean;
  /** true 表示动作或复合动作可能/确定只完成了一部分。 */
  partial?: boolean;
  /** 复合动作中已经收到 Playwright 成功答复的步骤数；不含结果不确定的当前步骤。 */
  completedCount?: number;
}

/** 与宿主 BrowserHostError 对齐，并保留真实执行阶段。 */
export class ActionError extends Error {
  readonly code: string;
  readonly phase: ActionPhase;
  readonly uncertain: boolean;
  readonly partial: boolean;
  readonly completedCount: number;

  constructor(message: string, code: string, details: ActionErrorDetails = {}) {
    super(message);
    this.name = 'ActionError';
    this.code = code;
    this.phase = details.phase ?? 'pre_dispatch';
    this.uncertain = details.uncertain ?? false;
    this.partial = details.partial ?? false;
    this.completedCount = Math.max(0, Math.trunc(details.completedCount ?? 0));
  }
}

export interface ActionContext {
  page: Page;
  refs: Map<string, RefRecord>;
  hash: (value: string) => string;
  timeoutMs: number;
  /** Absolute local deadline shared by every phase of one Host command. */
  deadlineAt?: number;
  /** Expected-dialog transactions handle their own modal sequence. */
  raceDialogs?: boolean;
  /** Keep the underlying Playwright action owned until a surfaced modal closes. */
  onModalActionPending?: (pending: Promise<void>) => void;
}

export type ClickButton = 'left' | 'right' | 'middle';
export type ClickModifier = 'Alt' | 'Control' | 'ControlOrMeta' | 'Meta' | 'Shift';
export interface ClickPosition {
  x: number;
  y: number;
}

export interface ClickOptions {
  button?: ClickButton;
  clickCount?: number;
  modifiers?: ClickModifier[];
  delayMs?: number;
  position?: ClickPosition;
}

export interface FillOptions {
  submit: boolean;
  slowly?: boolean;
}

export interface WaitOptions {
  timeSeconds?: number;
  text?: string;
  textGone?: string;
}

export interface MouseClickOptions {
  button?: ClickButton;
  clickCount?: number;
  delayMs?: number;
}

export interface PointerGesturePoint {
  x: number;
  y: number;
  elapsedMs: number;
  pressure?: number;
  tangentialPressure?: number;
  tiltX?: number;
  tiltY?: number;
  twist?: number;
  width?: number;
  height?: number;
}

export type PointerDeviceType = 'mouse' | 'pen' | 'touch';

export interface PointerGestureStart extends ClickPosition {
  pressure?: number;
  tangentialPressure?: number;
  tiltX?: number;
  tiltY?: number;
  twist?: number;
  width?: number;
  height?: number;
}

export interface PointerGestureOptions {
  /**
   * Absent in legacy v11/replay.v3 artifacts and therefore defaults to mouse.
   * New recordings always persist the browser PointerEvent.pointerType.
   */
  pointerType?: PointerDeviceType;
  button: ClickButton;
  modifiers: Exclude<ClickModifier, 'ControlOrMeta'>[];
  start: PointerGestureStart;
  /** Selector border-box-relative points, chronological and including the endpoint. */
  points: PointerGesturePoint[];
}

export interface DropPayload {
  files?: string[];
  /**
   * An explicitly supplied empty object is meaningful and must not be folded
   * into "no data": Playwright accepts `drop({ data: {} })`.
   */
  data?: Record<string, string>;
}

interface ResolvedRef {
  locator: Locator;
  record: RefRecord;
}

type FillFormTarget =
  | { ref: string; selector?: never }
  | { selector: string; ref?: never };

export type FillFormField = FillFormTarget & (
  | {
    type: 'textbox' | 'slider';
    value: string;
  }
  | {
    type: 'combobox';
    value: string;
    selectBy: 'label' | 'value';
  }
  | {
    type: 'checkbox' | 'radio';
    value: boolean;
  }
);

const DEFAULT_DISPATCH_TIMEOUT_MS = 15_000;
/** Matches Playwright MCP's default post-action observation window. */
const COMPLETION_SETTLE_MS = 500;
const COMPLETION_REQUEST_TIMEOUT_MS = 5_000;
const COMPLETION_NAVIGATION_TIMEOUT_MS = 10_000;
const CLICK_BUTTONS = new Set<ClickButton>(['left', 'right', 'middle']);
const CLICK_MODIFIERS = new Set<ClickModifier>([
  'Alt',
  'Control',
  'ControlOrMeta',
  'Meta',
  'Shift',
]);

function dispatchTimeout(ctx: ActionContext): number {
  if (Number.isFinite(ctx.deadlineAt)) {
    const remaining = Math.floor(Number(ctx.deadlineAt) - Date.now());
    if (remaining <= 0) {
      throw new ActionError(
        '浏览器命令已超过截止时间，未继续派发后续动作',
        'command_timeout',
      );
    }
    return remaining;
  }
  const requested = Number(ctx.timeoutMs);
  return Number.isFinite(requested) && requested > 0
    ? requested
    : DEFAULT_DISPATCH_TIMEOUT_MS;
}

function completionDeadlineExpired(ctx: ActionContext): boolean {
  return Number.isFinite(ctx.deadlineAt) && Date.now() >= Number(ctx.deadlineAt);
}

function completionDeadlineError(): ActionError {
  return new ActionError(
    '动作已执行，但页面结果未能在命令截止时间内完成收束',
    'command_timeout',
    {
      phase: 'dispatching',
      uncertain: true,
      partial: true,
      completedCount: 1,
    },
  );
}

function completionTimeout(ctx: ActionContext): number {
  try {
    return dispatchTimeout(ctx);
  } catch (error) {
    if (error instanceof ActionError && error.code === 'command_timeout') {
      throw completionDeadlineError();
    }
    throw error;
  }
}

/**
 * Observe requests from the moment an action starts, then settle the page using
 * the same causal model as upstream Playwright MCP.
 */
async function withCompletion<R>(
  ctx: ActionContext,
  action: () => Promise<R>,
): Promise<R> {
  const page = ctx.page;
  const pageLike = page as Page & {
    on?: Page['on'];
    off?: Page['off'];
    waitForTimeout?: Page['waitForTimeout'];
  };
  const requests: Request[] = [];
  const requestListener = (request: Request): void => {
    requests.push(request);
  };
  const observesRequests = (
    typeof pageLike.on === 'function'
    && typeof pageLike.off === 'function'
  );
  const completionDelay = async (
    milliseconds: number,
  ): Promise<'elapsed' | 'page-closed'> => {
    try {
      await page.waitForTimeout(milliseconds);
      return 'elapsed';
    } catch (error) {
      // window.close(), page.close() and OAuth popup self-close are successful
      // action outcomes. Playwright's page-scoped timer rejects once the
      // target is gone; treating that observation helper as the action failure
      // would incorrectly retry a mutation that already completed.
      if (page.isClosed()) return 'page-closed';
      throw error;
    }
  };
  if (observesRequests) page.on('request', requestListener);
  const complete = async (): Promise<R> => {
    const result = await Promise.resolve().then(action);
    if (typeof pageLike.waitForTimeout === 'function') {
      const settle = Math.min(COMPLETION_SETTLE_MS, completionTimeout(ctx));
      if (settle > 0 && await completionDelay(settle) === 'page-closed') {
        return result;
      }
      if (completionDeadlineExpired(ctx)) {
        throw completionDeadlineError();
      }
    }

    if (requests.some((request) => request.isNavigationRequest())) {
      try {
        await page.mainFrame().waitForLoadState('load', {
          timeout: Math.min(
            COMPLETION_NAVIGATION_TIMEOUT_MS,
            completionTimeout(ctx),
          ),
        });
      } catch {
        if (completionDeadlineExpired(ctx)) throw completionDeadlineError();
      }
      return result;
    }

    const pendingRequests = requests.map(async (request) => {
      try {
        const response = await request.response();
        if (
          response
          && ['document', 'stylesheet', 'script', 'xhr', 'fetch'].includes(
            request.resourceType(),
          )
        ) {
          await response.finished();
        }
      } catch {
        // A request may be aborted by a route change or page close. Completion
        // is observational and must not turn a successfully dispatched action
        // into an automatic retry.
      }
    });
    if (pendingRequests.length) {
      const requestOutcome = await Promise.race([
        Promise.all(pendingRequests).then(() => 'complete' as const),
        completionDelay(Math.min(
          COMPLETION_REQUEST_TIMEOUT_MS,
          completionTimeout(ctx),
        )).then((outcome) => (
          outcome === 'page-closed' ? 'page-closed' as const : 'timeout' as const
        )),
      ]);
      if (requestOutcome === 'page-closed') return result;
      if (requestOutcome === 'timeout' && completionDeadlineExpired(ctx)) {
        throw completionDeadlineError();
      }
      const settle = Math.min(COMPLETION_SETTLE_MS, completionTimeout(ctx));
      if (settle > 0 && await completionDelay(settle) === 'page-closed') {
        return result;
      }
      if (completionDeadlineExpired(ctx)) {
        throw completionDeadlineError();
      }
    }
    return result;
  };

  const completionPromise = complete();
  let cleanupTransferred = false;
  let dialogListener: (() => void) | null = null;
  const cleanup = (): void => {
    if (dialogListener && typeof pageLike.off === 'function') {
      page.off('dialog', dialogListener);
      dialogListener = null;
    }
    if (observesRequests) page.off('request', requestListener);
  };
  try {
    if (
      ctx.raceDialogs !== false
      && typeof pageLike.on === 'function'
      && typeof pageLike.off === 'function'
    ) {
      const dialogSignal = new Promise<{ kind: 'dialog' }>((resolve) => {
        dialogListener = () => resolve({ kind: 'dialog' });
        page.on('dialog', dialogListener);
      });
      const actionSignal = completionPromise.then(
        (value) => ({ kind: 'action' as const, value }),
        (error: unknown) => ({ kind: 'error' as const, error }),
      );
      const outcome = await Promise.race([actionSignal, dialogSignal]);
      if (outcome.kind === 'dialog') {
        cleanupTransferred = true;
        const pending = completionPromise
          .then(() => undefined)
          .finally(cleanup);
        if (ctx.onModalActionPending) ctx.onModalActionPending(pending);
        else void pending.catch(() => undefined);
        throw new ActionError(
          '动作已触发 JavaScript 对话框；请先处理对话框',
          'dialog_pending',
          { phase: 'dispatching' },
        );
      }
      if (outcome.kind === 'error') throw outcome.error;
      return outcome.value;
    } else {
      return await completionPromise;
    }
  } finally {
    if (!cleanupTransferred) cleanup();
  }
}

/**
 * Apply Playwright MCP's post-action completion boundary to a Host-owned
 * operation such as page/locator evaluation.
 *
 * Keep this narrow: ordinary hover/fill/select/check/key/mouse movement calls
 * deliberately do not use it, matching the upstream tool handlers.
 */
export async function withActionCompletion<R>(
  ctx: ActionContext,
  action: () => Promise<R>,
): Promise<R> {
  return await withCompletion(ctx, action);
}

function locatorForRecord(page: Page, record: RefRecord): Locator {
  return record.playwrightRef
    ? locatorFromRef(page, record.playwrightRef)
    : page.locator(record.selector);
}

function firstLine(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.split('\n')[0] || '未知错误';
}

function fullErrorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function boundedKey(key: string): string {
  if (
    typeof key !== 'string'
    || key.length < 1
    || [...key].some((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code <= 31 || code === 127;
    })
  ) {
    throw new ActionError('按键名称必须是非空且不含控制字符的字符串', 'invalid_input');
  }
  return key;
}

interface ValidatedClickOptions {
  button: ClickButton;
  clickCount: number;
  modifiers: ClickModifier[];
  delayMs: number;
  position?: ClickPosition;
}

function validatedClickOptions(options: ClickOptions = {}): ValidatedClickOptions {
  const button = options.button ?? 'left';
  const clickCount = options.clickCount ?? 1;
  const delayMs = options.delayMs ?? 0;
  const modifiers = options.modifiers ?? [];
  if (!CLICK_BUTTONS.has(button)) {
    throw new ActionError('click button 必须是 left/right/middle', 'invalid_input');
  }
  if (!Number.isSafeInteger(clickCount) || clickCount < 1) {
    throw new ActionError('click_count 必须是正整数', 'invalid_input');
  }
  if (!Number.isSafeInteger(delayMs) || delayMs < 0) {
    throw new ActionError('delay_ms 必须是非负整数', 'invalid_input');
  }
  if (
    !Array.isArray(modifiers)
    || modifiers.length > CLICK_MODIFIERS.size
    || new Set(modifiers).size !== modifiers.length
    || modifiers.some((modifier) => !CLICK_MODIFIERS.has(modifier))
  ) {
    throw new ActionError('modifiers 包含无效或重复的修饰键', 'invalid_input');
  }
  let position: ClickPosition | undefined;
  if (options.position !== undefined) {
    const raw = options.position as unknown;
    if (
      !raw
      || typeof raw !== 'object'
      || Array.isArray(raw)
      || Object.keys(raw as Record<string, unknown>).length !== 2
    ) {
      throw new ActionError('position 必须包含有限的 x/y 坐标', 'invalid_input');
    }
    const point = raw as Record<string, unknown>;
    if (
      typeof point.x !== 'number'
      || typeof point.y !== 'number'
      || !Number.isFinite(point.x)
      || !Number.isFinite(point.y)
      || point.x < 0
      || point.y < 0
    ) {
      throw new ActionError('position 必须包含有限的 x/y 坐标', 'invalid_input');
    }
    position = { x: point.x, y: point.y };
  }
  return {
    button,
    clickCount,
    delayMs,
    modifiers: [...modifiers],
    ...(position ? { position } : {}),
  };
}

function validatedWaitOptions(options: WaitOptions): Required<WaitOptions> {
  const timeSeconds = options.timeSeconds ?? 0;
  const text = options.text ?? '';
  const textGone = options.textGone ?? '';
  if (
    typeof timeSeconds !== 'number'
    || !Number.isFinite(timeSeconds)
    || timeSeconds < 0
  ) {
    throw new ActionError('time_seconds 必须是非负有限数字', 'invalid_input');
  }
  if (
    typeof text !== 'string'
    || typeof textGone !== 'string'
  ) {
    throw new ActionError('等待文本必须是字符串', 'invalid_input');
  }
  if (timeSeconds === 0 && !text && !textGone) {
    throw new ActionError(
      'wait 至少需要 time_seconds、text 或 text_gone 之一',
      'invalid_input',
    );
  }
  return { timeSeconds, text, textGone };
}

/**
 * 元素**存在但当前点不动**的证据：动画未静止、被浮层挡住、在视口外、被禁用。
 *
 * 这些只出现在 Playwright 的 Call log 里（错误第一行永远只有
 * `locator.click: Timeout NNNNNms exceeded.`），所以必须看全文。
 */
const NOT_ACTIONABLE_EVIDENCE =
  /element is not stable|intercepts pointer events|element is not visible|element is outside of the viewport|element is not enabled|element is not editable|element is disabled/i;

/** mutation 尚未调用时，可安全地分类成 stale/not-executed。 */
function translatePreDispatch(error: unknown): never {
  if (error instanceof ActionError) throw error;
  const message = firstLine(error);
  const callLog = fullErrorText(error);
  // 「找不到元素」和「元素在那儿但点不动」需要完全相反的下一步：前者重新 snapshot
  // 取新 ref 有用，后者再 snapshot 一百次也没用（轮播/动画元素永远不静止），
  // 只会每次白等满一个超时。过去两者都被塞进 stale_ref，模型就在 30s × N 的
  // 死循环里打转。这里按证据分开，并给出各自可执行的下一步。
  if (NOT_ACTIONABLE_EVIDENCE.test(callLog)) {
    throw new ActionError(
      `元素存在但当前不可点击（动画未停、被遮挡或在视口外）：${message}`,
      'element_not_actionable',
    );
  }
  // strict mode violation 同时用于「解析到 0 个」和「解析到 N 个」。
  // 前者是元素没了（重新 snapshot 有用），后者是身份不唯一（要挑更具体的 ref）。
  // 只有后者才是 ambiguous，否则会把「找不到」误导成「太多了」。
  if (/strict mode violation[^\n]*resolved to (?!0\b)\d+ element/i.test(callLog)) {
    throw new ActionError(`该 ref 在当前页面匹配到多个元素：${message}`, 'ambiguous_ref');
  }
  if (
    /Timeout .* exceeded|waiting for locator|element is not attached|not visible|not enabled|not editable|strict mode/i
      .test(message)
  ) {
    throw new ActionError(`元素不可操作或已变化：${message}`, 'stale_ref');
  }
  throw new ActionError(message, 'input_failed');
}

function translateWaitFailure(error: unknown): never {
  if (error instanceof ActionError) throw error;
  const message = firstLine(error);
  throw new ActionError(
    message,
    /Timeout .* exceeded|waiting for/i.test(message) ? 'wait_timeout' : 'wait_failed',
  );
}

/**
 * mutation API 已经被调用后，Timeout/协议错误不能再翻译成“未执行”。
 *
 * Locator 可能已发出 mouseDown、input、change、keydown 或 navigation，只是等待后续
 * 条件时报错。统一标记 uncertain+partial，禁止上层把它当 stale_ref 自动重试。
 */
function translateAfterDispatch(error: unknown): never {
  // Modal races are a deliberate, classified early return. The underlying
  // Playwright action is still owned by BrowserHost and will be joined after
  // the dialog is handled; do not erase that state into a generic uncertainty.
  if (error instanceof ActionError) throw error;
  const callLog = fullErrorText(error);
  const hasDispatchEvidence = /\bperforming [^\n]* action\b/i.test(callLog);
  const hasOnlyActionabilityEvidence = (
    /waiting for (?:locator|element)|strict mode violation|element is (?:not |outside )|intercepts pointer events|did not find some options|option being selected is not enabled/i
      .test(callLog)
  );
  if (hasOnlyActionabilityEvidence && !hasDispatchEvidence) {
    // Playwright logs `performing <action> action` immediately before native
    // mouse/touch dispatch. A timeout that never reached that marker is a
    // proven actionability/strict-resolution failure, safe for a fresh ref.
    translatePreDispatch(error);
  }
  throw new ActionError(
    `动作可能已部分执行，最终页面状态未知：${firstLine(error)}`,
    'input_uncertain',
    { phase: 'dispatching', uncertain: true, partial: true },
  );
}

async function resolve(ctx: ActionContext, nativeRef: string): Promise<ResolvedRef> {
  const record = ctx.refs.get(nativeRef);
  if (!record) {
    throw new ActionError('元素 ref 不属于当前快照，请重新观察', 'stale_ref');
  }
  // Execute the original exact Locator. For snapshot refs this is
  // `aria-ref=eN`, whose identity is stronger than normalize()'s generated
  // semantic selector. Upstream Playwright MCP likewise normalizes only for
  // generated code and executes the original locator.
  const locator = locatorForRecord(ctx.page, record);
  // Do not preflight with locator.count(). Every official Locator mutation is
  // already strict and auto-waits for the target. A separate instantaneous
  // count turns transient React/Vue rerenders (0 → 1 or 2 → 1) into false
  // stale_ref failures and adds a TOCTOU window before Playwright's own
  // actionability loop. Read operations below are strict Locator operations as
  // well. Persisted selectors are still uniqueness-checked once when `locate`
  // creates their @sN handle.
  return { locator, record };
}

async function resolveMutation(
  ctx: ActionContext,
  nativeRef: string,
): Promise<ResolvedRef> {
  return await resolve(ctx, nativeRef);
}

function validateFormFieldShape(field: FillFormField, index: number): void {
  const candidate = field as unknown as Record<string, unknown>;
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) {
    throw new ActionError(`批量表单第 ${index + 1} 项无效`, 'invalid_fill_form');
  }
  if (
    !['textbox', 'combobox', 'checkbox', 'radio', 'slider'].includes(field.type)
  ) {
    throw new ActionError(`批量表单第 ${index + 1} 项无效`, 'invalid_fill_form');
  }
  const hasRef = typeof candidate.ref === 'string' && candidate.ref.length > 0;
  const hasSelector = (
    typeof candidate.selector === 'string'
    && candidate.selector.length > 0
  );
  if (hasRef === hasSelector) {
    throw new ActionError(`批量表单第 ${index + 1} 项目标无效`, 'invalid_fill_form');
  }
  const targetKey = hasRef ? 'ref' : 'selector';
  if (field.type === 'textbox' || field.type === 'slider') {
    if (
      Object.keys(candidate).some((key) => !['type', targetKey, 'value'].includes(key))
      || typeof field.value !== 'string'
      || (field.type === 'slider' && !field.value)
    ) {
      throw new ActionError(`批量表单第 ${index + 1} 项无效`, 'invalid_fill_form');
    }
    return;
  }
  if (field.type === 'combobox') {
    if (
      Object.keys(candidate).some(
        (key) => !['type', targetKey, 'value', 'selectBy'].includes(key),
      )
      || typeof field.value !== 'string'
      || !['label', 'value'].includes(field.selectBy)
    ) {
      throw new ActionError(`批量表单第 ${index + 1} 项无效`, 'invalid_fill_form');
    }
    return;
  }
  if (
    Object.keys(candidate).some((key) => !['type', targetKey, 'value'].includes(key))
    || typeof field.value !== 'boolean'
  ) {
    throw new ActionError(`批量表单第 ${index + 1} 项无效`, 'invalid_fill_form');
  }
}

async function locatorForFormField(
  ctx: ActionContext,
  field: FillFormField,
): Promise<Locator> {
  if ('selector' in field && typeof field.selector === 'string') {
    // Recorder selectors are Playwright's own normalized selector language.
    // Construct the Locator immediately before this field's action, matching
    // upstream fill_form and allowing earlier fields to reveal/re-render it.
    return ctx.page.locator(field.selector);
  }
  return (await resolve(ctx, field.ref)).locator;
}

function rethrowBatchFailure(
  error: unknown,
  completedCount: number,
  total: number,
): never {
  const original = error instanceof ActionError
    ? error
    : new ActionError(firstLine(error), 'input_failed');
  const partial = completedCount > 0 || original.partial;
  const phase: ActionPhase = partial && !original.uncertain ? 'partial' : original.phase;
  throw new ActionError(
    `批量表单已确认完成 ${completedCount}/${total} 项；后续项失败，未自动提交`,
    original.code,
    {
      phase,
      uncertain: original.uncertain,
      partial,
      completedCount,
    },
  );
}

export async function click(
  ctx: ActionContext,
  nativeRef: string,
  options: ClickOptions = {},
): Promise<void> {
  const checked = validatedClickOptions(options);
  const { locator } = await resolveMutation(ctx, nativeRef);
  try {
    await withCompletion(ctx, async () => {
      await locator.click({
        timeout: dispatchTimeout(ctx),
        button: checked.button,
        clickCount: checked.clickCount,
        modifiers: checked.modifiers,
        delay: checked.delayMs,
        ...(checked.position ? { position: checked.position } : {}),
      });
    });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function verifyDownloadTarget(
  ctx: ActionContext,
  nativeRef: string,
): Promise<string> {
  const resolved = await resolve(ctx, nativeRef);
  return resolved.record.downloadNavigation ?? '';
}

export async function clickArmed(
  ctx: ActionContext,
  nativeRef: string,
  arm: () => void,
): Promise<void> {
  const { locator } = await resolveMutation(ctx, nativeRef);
  try {
    arm();
  } catch (error) {
    translatePreDispatch(error);
  }
  try {
    await locator.click({ timeout: dispatchTimeout(ctx) });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function hover(
  ctx: ActionContext,
  nativeRef: string,
  position?: ClickPosition,
): Promise<void> {
  if (position !== undefined) {
    validatedClickOptions({ position });
  }
  const { locator } = await resolveMutation(ctx, nativeRef);
  try {
    await locator.hover({
      timeout: dispatchTimeout(ctx),
      ...(position ? { position } : {}),
    });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function fill(
  ctx: ActionContext,
  nativeRef: string,
  value: string,
  options: FillOptions,
): Promise<void> {
  if (typeof value !== 'string') {
    throw new ActionError('type text 必须是字符串', 'invalid_input');
  }
  if (typeof options.submit !== 'boolean' || (
    options.slowly !== undefined && typeof options.slowly !== 'boolean'
  )) {
    throw new ActionError('type submit/slowly 必须是 boolean', 'invalid_input');
  }
  const resolved = await resolveMutation(ctx, nativeRef);

  const action = async (): Promise<void> => {
    if (options.slowly) {
      await resolved.locator.pressSequentially(value, {
        timeout: dispatchTimeout(ctx),
      });
    } else {
      await resolved.locator.fill(value, { timeout: dispatchTimeout(ctx) });
    }
    if (options.submit) {
      await resolved.locator.press('Enter', {
        timeout: dispatchTimeout(ctx),
      });
    }
  };
  try {
    if (options.submit || options.slowly) await withCompletion(ctx, action);
    else await action();
  } catch (error) {
    translateAfterDispatch(error);
  }
}

/**
 * Resolve both endpoints before any pointer event, then let Playwright perform
 * its own actionability checks and native HTML5/pointer drag sequence.
 */
export async function drag(
  ctx: ActionContext,
  startRef: string,
  endRef: string,
  options: {
    sourcePosition?: { x: number; y: number };
    targetPosition?: { x: number; y: number };
  } = {},
): Promise<void> {
  // Keep endpoint materialisation deterministic; dragTo performs strict
  // resolution and actionability for both Locators in one official action.
  const start = await resolveMutation(ctx, startRef);
  const end = await resolveMutation(ctx, endRef);
  try {
    await withCompletion(ctx, async () => {
      await start.locator.dragTo(end.locator, {
        timeout: dispatchTimeout(ctx),
        ...(options.sourcePosition
          ? { sourcePosition: { ...options.sourcePosition } }
          : {}),
        ...(options.targetPosition
          ? { targetPosition: { ...options.targetPosition } }
          : {}),
      });
    });
  } catch (error) {
    // dragTo can fail after mousedown or intermediate mousemove.
    translateAfterDispatch(error);
  }
}

/**
 * Fill a typed form without submitting it.
 *
 * This is intentionally non-atomic: web pages do not offer transactions.
 * Each ref is resolved immediately before its field action, matching upstream
 * Playwright MCP form.ts and supporting virtualized/dependent forms. Actual
 * actionability stays inside each official Playwright action;
 * speculative trial/wait passes would scroll virtualized forms and introduce a
 * second TOCTOU window. Failures report only confirmed completion.
 */
export async function fillForm(
  ctx: ActionContext,
  fields: FillFormField[],
): Promise<{ completedCount: number }> {
  if (!Array.isArray(fields) || fields.length < 1) {
    throw new ActionError('批量表单 fields 至少包含一项', 'invalid_fill_form');
  }
  fields.forEach(validateFormFieldShape);

  let completedCount = 0;
  for (const field of fields) {
    try {
      const locator = await locatorForFormField(ctx, field);
      if (field.type === 'textbox' || field.type === 'slider') {
        try {
          await locator.fill(field.value, { timeout: dispatchTimeout(ctx) });
        } catch (error) {
          translateAfterDispatch(error);
        }
      } else if (field.type === 'combobox') {
        try {
          const option = field.selectBy === 'label'
            ? { label: field.value }
            : { value: field.value };
          await locator.selectOption(option, {
            timeout: dispatchTimeout(ctx),
          });
        } catch (error) {
          translateAfterDispatch(error);
        }
      } else if (field.type === 'checkbox' || field.type === 'radio') {
        try {
          await locator.setChecked(field.value, {
            timeout: dispatchTimeout(ctx),
          });
        } catch (error) {
          translateAfterDispatch(error);
        }
      } else {
        throw new ActionError('批量表单字段类型无效', 'invalid_fill_form');
      }
      completedCount += 1;
    } catch (error) {
      rethrowBatchFailure(error, completedCount, fields.length);
    }
  }
  return { completedCount };
}

export async function press(
  ctx: ActionContext,
  key: string,
  nativeRef: string | undefined,
): Promise<void> {
  const checkedKey = boundedKey(key);
  if (!nativeRef) {
    try {
      dispatchTimeout(ctx);
      const action = async (): Promise<void> => {
        await ctx.page.keyboard.press(checkedKey);
      };
      if (checkedKey === 'Enter') await withCompletion(ctx, action);
      else await action();
    } catch (error) {
      translateAfterDispatch(error);
    }
    return;
  }

  const mutationResolved = await resolveMutation(ctx, nativeRef);
  try {
    const action = async (): Promise<void> => {
      await mutationResolved.locator.press(checkedKey, {
        timeout: dispatchTimeout(ctx),
      });
    };
    if (checkedKey === 'Enter') await withCompletion(ctx, action);
    else await action();
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function keyDown(ctx: ActionContext, key: string): Promise<void> {
  const checkedKey = boundedKey(key);
  try {
    dispatchTimeout(ctx);
    await ctx.page.keyboard.down(checkedKey);
  } catch (error) {
    // A transport failure can leave the key held down in the renderer.
    translateAfterDispatch(error);
  }
}

export async function keyUp(ctx: ActionContext, key: string): Promise<void> {
  const checkedKey = boundedKey(key);
  try {
    dispatchTimeout(ctx);
    await ctx.page.keyboard.up(checkedKey);
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function goBack(ctx: ActionContext): Promise<void> {
  let response: Awaited<ReturnType<Page['goBack']>>;
  try {
    response = await ctx.page.goBack({ waitUntil: 'commit', timeout: dispatchTimeout(ctx) });
  } catch (error) {
    translateAfterDispatch(error);
  }
  if (response === null) {
    throw new ActionError('当前页面无法后退', 'no_history');
  }
}

export async function goForward(ctx: ActionContext): Promise<void> {
  let response: Awaited<ReturnType<Page['goForward']>>;
  try {
    response = await ctx.page.goForward({ waitUntil: 'commit', timeout: dispatchTimeout(ctx) });
  } catch (error) {
    translateAfterDispatch(error);
  }
  if (response === null) {
    throw new ActionError('当前页面无法前进', 'no_history');
  }
}

export async function reload(ctx: ActionContext): Promise<void> {
  try {
    await ctx.page.reload({ timeout: dispatchTimeout(ctx) });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function waitFor(ctx: ActionContext, options: WaitOptions): Promise<void> {
  const checked = validatedWaitOptions(options);
  try {
    if (checked.timeSeconds > 0) {
      const requestedMs = checked.timeSeconds * 1_000;
      const remainingMs = dispatchTimeout(ctx);
      const truncatedByDeadline = requestedMs > remainingMs;
      await ctx.page.waitForTimeout(Math.min(requestedMs, remainingMs));
      if (truncatedByDeadline) {
        throw new ActionError(
          '等待时长超过浏览器命令截止时间',
          'command_timeout',
        );
      }
    }
    if (checked.textGone) {
      await ctx.page.getByText(checked.textGone).first().waitFor({
        state: 'hidden',
        timeout: dispatchTimeout(ctx),
      });
    }
    if (checked.text) {
      await ctx.page.getByText(checked.text).first().waitFor({
        state: 'visible',
        timeout: dispatchTimeout(ctx),
      });
    }
  } catch (error) {
    translateWaitFailure(error);
  }
}

export async function selectOption(
  ctx: ActionContext,
  nativeRef: string,
  values: string[],
): Promise<string[]> {
  const resolved = await resolveMutation(ctx, nativeRef);
  try {
    return await resolved.locator.selectOption(values, {
      timeout: dispatchTimeout(ctx),
    });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function setChecked(
  ctx: ActionContext,
  nativeRef: string,
  checked: boolean,
): Promise<void> {
  const { locator } = await resolveMutation(ctx, nativeRef);
  try {
    await locator.setChecked(checked, {
      timeout: dispatchTimeout(ctx),
    });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

/**
 * 注册一个意外遮挡的自动处理器。
 *
 * 内网系统最常见的回放杀手不是站点改版，是**随机出现的公告弹窗、满意度调查、
 * 版本更新提示**：录制那次没弹，回放这次弹了，于是每一个后续点击都被一个
 * 半透明遮罩吃掉，报出来的却是"元素不可点击"。
 *
 * Playwright 的 `addLocatorHandler` 正为此设计：注册之后，它在**每次**
 * actionability 检查与自动等待断言之前检查这个 locator，可见就先跑处理器，
 * 然后确认遮挡已消失才继续原动作。这比在计划里插一个"点掉弹窗"的步骤强得多
 * ——后者只在那个固定位置有效，而弹窗什么时候来是不确定的。
 *
 * ## 三个刻意的选择
 *
 * 1. **用 selector 而不是 ref。** 处理器要跨越整场回放存活，而 ref 表每次快照
 *    整张替换。ref 在这里必然失效。
 * 2. **`.first()`。** 遮挡层的关闭按钮在页面上可能有多个同名兄弟（多个弹窗
 *    排队）。strict 模式下多匹配会抛，而抛在处理器里会让**触发它的那个动作**
 *    失败——一个本该提高稳定性的机制反而成了新的失败源。
 * 3. **处理器内部只点击，不做别的。** 处理器的执行时间计入触发它的那个动作的
 *    超时预算；在里面等待、导航、再点第二个东西，会把一次普通点击拖成超时。
 */
export async function registerOverlayHandler(
  ctx: ActionContext,
  selector: string,
): Promise<void> {
  const target = String(selector || '');
  if (!target) {
    throw new ActionError('遮挡处理器缺少 selector', 'invalid_overlay');
  }
  const locator = ctx.page.locator(target).first();
  try {
    await ctx.page.addLocatorHandler(locator, async (overlay) => {
      // 处理器里的失败不能冒泡成触发动作的失败：遮挡可能在我们点它之前
      // 就自己消失了（动画结束、倒计时关闭），那不是错误。
      await overlay.click({ timeout: OVERLAY_CLICK_TIMEOUT_MS }).catch(() => undefined);
    });
  } catch (error) {
    throw new ActionError(
      `注册遮挡处理器失败：${error instanceof Error ? error.message.split('\n')[0] : String(error)}`,
      'invalid_overlay',
    );
  }
}

/**
 * 处理器内点击的超时预算。
 *
 * 刻意很短：它计入触发它的那个动作的总预算，而它要做的事只是点一下一个
 * 已经确认可见的元素。给长了，一次普通点击会因为处理器慢而超时。
 */
const OVERLAY_CLICK_TIMEOUT_MS = 2_000;

/**
 * 状态断言。
 *
 * ## 为什么不用 `expect()`
 *
 * Playwright 的 `expect` 断言库在 `@playwright/test` 里，那是 devDependency，
 * 主进程运行时拿不到，也不该为了几个断言把整个测试框架打进产品。这里用
 * `playwright-core` 的公开 Locator API 复刻 `expect` 的**语义**：
 * 严格单一匹配 + 自动重试到截止时间。
 *
 * ## 为什么断言必须能等
 *
 * `expect(locator).toBeVisible()` 会重试；`locator.isVisible()` 是一次性快照。
 * 用后者做断言，等于要求每个断言前面手工塞一个 wait——那就等于没有断言，
 * 因为忘了塞的那一次会随机失败。visible/hidden 直接走 `waitFor({state})`
 * （Playwright 自带重试），其余状态走有界轮询。
 */
export type AssertState =
  | 'visible' | 'hidden' | 'enabled' | 'disabled'
  | 'checked' | 'unchecked' | 'editable';

const ASSERT_STATES = new Set<AssertState>([
  'visible', 'hidden', 'enabled', 'disabled', 'checked', 'unchecked', 'editable',
]);

/** 轮询间隔。与 Playwright expect 的量级一致，够快又不打满 CDP 通道。 */
const ASSERT_POLL_MS = 100;

export async function assertState(
  ctx: ActionContext,
  nativeRef: string,
  state: string,
): Promise<void> {
  if (!ASSERT_STATES.has(state as AssertState)) {
    throw new ActionError(`不支持的断言状态：${state}`, 'invalid_assert');
  }
  const { locator } = await resolve(ctx, nativeRef);
  // visible / hidden 由 Playwright 原生重试，语义与 expect 完全一致。
  if (state === 'visible' || state === 'hidden') {
    try {
      await locator.waitFor({ state, timeout: dispatchTimeout(ctx) });
    } catch (error) {
      throw assertionFailure(state, error);
    }
    return;
  }

  // 其余状态没有 waitFor 对应项，自己复刻 expect 的重试。
  //
  // 每一轮都重新读一次：断言的对象是**页面当前状态**，缓存第一次的结果就
  // 退化成一次性快照。`isEnabled` 等在元素不存在时会抛，按"还没到"处理继续等，
  // 到截止时间才判失败——这正是 expect 的行为。
  const check = async (): Promise<boolean> => {
    switch (state) {
      case 'enabled': return await locator.isEnabled({ timeout: ASSERT_POLL_MS });
      case 'disabled': return await locator.isDisabled({ timeout: ASSERT_POLL_MS });
      case 'checked': return await locator.isChecked({ timeout: ASSERT_POLL_MS });
      case 'unchecked': return !(await locator.isChecked({ timeout: ASSERT_POLL_MS }));
      default: return await locator.isEditable({ timeout: ASSERT_POLL_MS });
    }
  };

  // **自己算绝对截止时间，不要每轮问 dispatchTimeout。**
  //
  // `dispatchTimeout` 在没有 `ctx.deadlineAt` 时返回的是**常量** `ctx.timeoutMs`
  // ——拿它当"剩余预算"来判断退出，循环条件永远不成立，主进程会被一个断言
  // 卡死。这里先把预算折算成一个单调递减的绝对时刻，之后只跟它比。
  const budgetMs = dispatchTimeout(ctx);
  const deadline = Date.now() + budgetMs;
  let lastError: unknown;
  for (;;) {
    try {
      if (await check()) return;
      lastError = undefined;
    } catch (error) {
      // 元素还不存在时 isEnabled 等会抛。按"还没到"处理继续等，
      // 到截止时间才判失败——这正是 expect 的行为。
      lastError = error;
    }
    if (Date.now() + ASSERT_POLL_MS >= deadline) {
      throw assertionFailure(state, lastError);
    }
    await new Promise((resolve) => setTimeout(resolve, ASSERT_POLL_MS));
  }
}

/**
 * 断言失败必须是**可区分**的失败。
 *
 * 混进 stale_ref / command_timeout 里，模型就会去重试导航或重新观察，
 * 而真正的结论是"这一页不是预期的那一页，别往下走了"。
 */
function assertionFailure(state: string, cause: unknown): ActionError {
  const detail = cause instanceof Error && cause.message
    ? `：${cause.message.split('\n')[0]}`
    : '';
  return new ActionError(
    `断言不成立：目标元素未达到 ${state} 状态${detail}`,
    'assertion_failed',
    { phase: 'pre_dispatch' },
  );
}

const SCROLL_DIRECTIONS = new Set(['up', 'down', 'left', 'right']);

export async function scroll(ctx: ActionContext, direction: string, pixels: number): Promise<void> {
  if (!SCROLL_DIRECTIONS.has(direction)) {
    throw new ActionError('滚动方向无效', 'invalid_input');
  }
  if (!Number.isFinite(pixels)) throw new ActionError('滚动距离无效', 'invalid_input');
  const amount = Math.abs(pixels);
  const deltaX = direction === 'left' ? -amount : direction === 'right' ? amount : 0;
  const deltaY = direction === 'up' ? -amount : direction === 'down' ? amount : 0;
  try {
    dispatchTimeout(ctx);
    await ctx.page.mouse.wheel(deltaX, deltaY);
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function scrollDelta(
  ctx: ActionContext,
  deltaX: number,
  deltaY: number,
): Promise<void> {
  if (
    !Number.isFinite(deltaX)
    || !Number.isFinite(deltaY)
    || (deltaX === 0 && deltaY === 0)
  ) {
    throw new ActionError('双轴滚动距离无效', 'invalid_input');
  }
  try {
    dispatchTimeout(ctx);
    // Preserve a trackpad-style diagonal gesture as one browser input event.
    await ctx.page.mouse.wheel(deltaX, deltaY);
  } catch (error) {
    translateAfterDispatch(error);
  }
}

function finiteMouseNumber(value: number, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new ActionError(`${label} 必须是有限数字`, 'invalid_input');
  }
  return value;
}

function mouseButton(button: ClickButton | undefined): ClickButton {
  const checked = button ?? 'left';
  if (!CLICK_BUTTONS.has(checked)) {
    throw new ActionError('mouse button 必须是 left/right/middle', 'invalid_input');
  }
  return checked;
}

export async function mouseMove(
  ctx: ActionContext,
  x: number,
  y: number,
): Promise<void> {
  const checkedX = finiteMouseNumber(x, 'mouse x');
  const checkedY = finiteMouseNumber(y, 'mouse y');
  try {
    dispatchTimeout(ctx);
    await ctx.page.mouse.move(checkedX, checkedY);
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function mouseDown(
  ctx: ActionContext,
  button?: ClickButton,
): Promise<void> {
  const checkedButton = mouseButton(button);
  try {
    dispatchTimeout(ctx);
    await ctx.page.mouse.down({ button: checkedButton });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function mouseUp(
  ctx: ActionContext,
  button?: ClickButton,
): Promise<void> {
  const checkedButton = mouseButton(button);
  try {
    dispatchTimeout(ctx);
    await ctx.page.mouse.up({ button: checkedButton });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function mouseWheel(
  ctx: ActionContext,
  deltaX: number,
  deltaY: number,
): Promise<void> {
  const checkedX = finiteMouseNumber(deltaX, 'mouse deltaX');
  const checkedY = finiteMouseNumber(deltaY, 'mouse deltaY');
  try {
    dispatchTimeout(ctx);
    // The upstream schema deliberately accepts the no-op (0, 0) pair.
    await ctx.page.mouse.wheel(checkedX, checkedY);
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function mouseClick(
  ctx: ActionContext,
  x: number,
  y: number,
  options: MouseClickOptions = {},
): Promise<void> {
  const checkedX = finiteMouseNumber(x, 'mouse x');
  const checkedY = finiteMouseNumber(y, 'mouse y');
  const button = mouseButton(options.button);
  const clickCount = options.clickCount ?? 1;
  const delayMs = options.delayMs ?? 0;
  if (!Number.isSafeInteger(clickCount) || clickCount < 1) {
    throw new ActionError('mouse clickCount 必须是正整数', 'invalid_input');
  }
  if (typeof delayMs !== 'number' || !Number.isFinite(delayMs) || delayMs < 0) {
    throw new ActionError('mouse delay 必须是非负有限数字', 'invalid_input');
  }
  try {
    dispatchTimeout(ctx);
    await withCompletion(ctx, async () => {
      await ctx.page.mouse.click(checkedX, checkedY, {
        button,
        clickCount,
        delay: delayMs,
      });
    });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function mouseDrag(
  ctx: ActionContext,
  startX: number,
  startY: number,
  endX: number,
  endY: number,
): Promise<void> {
  const checkedStartX = finiteMouseNumber(startX, 'mouse startX');
  const checkedStartY = finiteMouseNumber(startY, 'mouse startY');
  const checkedEndX = finiteMouseNumber(endX, 'mouse endX');
  const checkedEndY = finiteMouseNumber(endY, 'mouse endY');
  let buttonDown = false;
  try {
    dispatchTimeout(ctx);
    await withCompletion(ctx, async () => {
      await ctx.page.mouse.move(checkedStartX, checkedStartY);
      await ctx.page.mouse.down();
      buttonDown = true;
      try {
        await ctx.page.mouse.move(checkedEndX, checkedEndY);
        await ctx.page.mouse.up();
        buttonDown = false;
      } finally {
        // A failed intermediate move must not poison every later browser
        // action by leaving the shared Playwright mouse permanently pressed.
        if (buttonDown) {
          await ctx.page.mouse.up().catch(() => undefined);
          buttonDown = false;
        }
      }
    });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

interface NormalizedPointerTelemetry {
  pressure?: number;
  tangentialPressure?: number;
  tiltX?: number;
  tiltY?: number;
  twist?: number;
  width?: number;
  height?: number;
}

interface CdpPointerTelemetry {
  force?: number;
  tangentialPressure?: number;
  tiltX?: number;
  tiltY?: number;
  twist?: number;
}

function pointerTelemetry(
  value: PointerGestureStart | PointerGesturePoint,
  label: string,
): NormalizedPointerTelemetry {
  const ranges = {
    pressure: [0, 1],
    tangentialPressure: [-1, 1],
    tiltX: [-90, 90],
    tiltY: [-90, 90],
    twist: [0, 359],
    width: [0, Number.POSITIVE_INFINITY],
    height: [0, Number.POSITIVE_INFINITY],
  } as const;
  const normalized: NormalizedPointerTelemetry = {};
  for (const name of Object.keys(ranges) as Array<keyof typeof ranges>) {
    const raw = value[name];
    if (raw === undefined) continue;
    const number = finiteMouseNumber(raw, `${label}.${name}`);
    const [minimum, maximum] = ranges[name];
    if (number < minimum || number > maximum) {
      throw new ActionError(`${label}.${name} 超出浏览器范围`, 'invalid_input');
    }
    normalized[name] = number;
  }
  return normalized;
}

function modifierMask(
  modifiers: Array<Exclude<ClickModifier, 'ControlOrMeta'>>,
): number {
  let mask = 0;
  if (modifiers.includes('Alt')) mask |= 1;
  if (modifiers.includes('Control')) mask |= 2;
  if (modifiers.includes('Meta')) mask |= 4;
  if (modifiers.includes('Shift')) mask |= 8;
  return mask;
}

function cdpPointerTelemetry(
  telemetry: NormalizedPointerTelemetry,
  defaultPressure: number,
): CdpPointerTelemetry {
  return {
    force: telemetry.pressure ?? defaultPressure,
    ...(telemetry.tangentialPressure === undefined
      ? {}
      : { tangentialPressure: telemetry.tangentialPressure }),
    ...(telemetry.tiltX === undefined ? {} : { tiltX: telemetry.tiltX }),
    ...(telemetry.tiltY === undefined ? {} : { tiltY: telemetry.tiltY }),
    ...(telemetry.twist === undefined ? {} : { twist: telemetry.twist }),
  };
}

function cdpButtonMask(button: ClickButton): number {
  if (button === 'left') return 1;
  if (button === 'right') return 2;
  return 4;
}

/**
 * Replay a recorded canvas/map/custom-control pointer stream.
 *
 * Legacy/mouse artifacts stay on Playwright's public `page.mouse` surface.
 * Chromium exposes pen/touch fidelity through the public
 * `BrowserContext.newCDPSession()` API: pen is dispatched as pointerType=pen,
 * while touch uses one stable primary touch id. Coordinates remain relative to
 * the locator's current border box so responsive layout changes do not
 * invalidate the recording.
 *
 * Cleanup deliberately bypasses translating wrappers. Even when an
 * intermediate CDP command has an uncertain result, a pressed button/contact,
 * every held modifier, and the public CDPSession are released best-effort
 * before the original error escapes.
 */
export async function pointerGesture(
  ctx: ActionContext,
  nativeRef: string,
  options: PointerGestureOptions,
): Promise<void> {
  const pointerType = options.pointerType ?? 'mouse';
  if (
    pointerType !== 'mouse'
    && pointerType !== 'pen'
    && pointerType !== 'touch'
  ) {
    throw new ActionError('pointer gesture pointerType 无效', 'invalid_input');
  }
  const button = mouseButton(options.button);
  if (pointerType === 'touch' && button !== 'left') {
    throw new ActionError('touch pointer gesture 只支持主触点', 'invalid_input');
  }
  const modifiers = options.modifiers;
  if (
    !Array.isArray(modifiers)
    || new Set(modifiers).size !== modifiers.length
    || modifiers.some((modifier) => (
      modifier !== 'Alt'
      && modifier !== 'Control'
      && modifier !== 'Meta'
      && modifier !== 'Shift'
    ))
  ) {
    throw new ActionError('pointer gesture modifiers 无效', 'invalid_input');
  }
  const start: PointerGestureStart = {
    x: finiteMouseNumber(options.start.x, 'pointer gesture start.x'),
    y: finiteMouseNumber(options.start.y, 'pointer gesture start.y'),
    ...pointerTelemetry(options.start, 'pointer gesture start'),
  };
  if (!Array.isArray(options.points) || options.points.length === 0) {
    throw new ActionError('pointer gesture points 不能为空', 'invalid_input');
  }
  let previousElapsed = 0;
  const points = options.points.map((point, index) => {
    const elapsedMs = finiteMouseNumber(
      point.elapsedMs,
      `pointer gesture points[${index}].elapsedMs`,
    );
    const normalized: PointerGesturePoint = {
      x: finiteMouseNumber(point.x, `pointer gesture points[${index}].x`),
      y: finiteMouseNumber(point.y, `pointer gesture points[${index}].y`),
      elapsedMs,
      ...pointerTelemetry(point, `pointer gesture points[${index}]`),
    };
    if (elapsedMs < previousElapsed || elapsedMs < 0) {
      throw new ActionError(
        'pointer gesture elapsedMs 必须单调非降',
        'invalid_input',
      );
    }
    previousElapsed = elapsedMs;
    return normalized;
  });
  const pressedModifiers: Array<Exclude<ClickModifier, 'ControlOrMeta'>> = [];
  let buttonMayBeDown = false;
  let touchMayBeActive = false;
  let cdp: CDPSession | null = null;
  let lastX = 0;
  let lastY = 0;
  let actionOwnsCleanup = false;
  const cleanupPointerState = async (): Promise<void> => {
    if (buttonMayBeDown) {
      if (pointerType === 'mouse') {
        await ctx.page.mouse.up({ button }).catch(() => undefined);
      } else if (cdp) {
        await cdp.send('Input.dispatchMouseEvent', {
          type: 'mouseReleased',
          x: lastX,
          y: lastY,
          button,
          buttons: 0,
          clickCount: 1,
          pointerType: 'pen',
          force: 0,
        }).catch(() => undefined);
      }
      buttonMayBeDown = false;
    }
    if (touchMayBeActive && cdp) {
      await cdp.send('Input.dispatchTouchEvent', {
        type: 'touchCancel',
        touchPoints: [],
      }).catch(() => undefined);
      touchMayBeActive = false;
    }
    while (pressedModifiers.length) {
      const modifier = pressedModifiers.pop();
      if (modifier) {
        await ctx.page.keyboard.up(modifier).catch(() => undefined);
      }
    }
    if (cdp) {
      await cdp.detach().catch(() => undefined);
      cdp = null;
    }
  };
  try {
    const resolved = await resolveMutation(ctx, nativeRef);
    // Raw mouse APIs do not auto-scroll like Locator.click/dragTo. Resolve the
    // selector into the viewport before translating its border-box coordinates.
    await resolved.locator.scrollIntoViewIfNeeded({
      timeout: dispatchTimeout(ctx),
    });
    const box = await resolved.locator.boundingBox();
    if (!box) {
      throw new ActionError('pointer gesture 目标不可见', 'stale_ref');
    }
    lastX = box.x + start.x;
    lastY = box.y + start.y;
    if (pointerType !== 'mouse') {
      cdp = await ctx.page.context().newCDPSession(ctx.page);
    }
    dispatchTimeout(ctx);
    await withCompletion(ctx, async () => {
      actionOwnsCleanup = true;
      try {
        for (const modifier of modifiers) {
          // Key dispatch may reach Chromium and still reject locally. Register
          // cleanup before awaiting so uncertain delivery cannot leave it held.
          pressedModifiers.push(modifier);
          await ctx.page.keyboard.down(modifier);
        }
        const modifiersBitfield = modifierMask(modifiers);
        let lastTouchPressure = start.pressure ?? 1;
        if (pointerType === 'mouse') {
          await ctx.page.mouse.move(lastX, lastY);
          buttonMayBeDown = true;
          await ctx.page.mouse.down({ button });
        } else if (pointerType === 'pen') {
          const session = cdp;
          if (!session) {
            throw new ActionError('pen CDP session 不可用', 'browser_unavailable');
          }
          await session.send('Input.dispatchMouseEvent', {
            type: 'mouseMoved',
            x: lastX,
            y: lastY,
            modifiers: modifiersBitfield,
            button: 'none',
            buttons: 0,
            pointerType: 'pen',
            ...cdpPointerTelemetry(start, 0),
          });
          // Register cleanup before awaiting: a rejected command may still
          // have reached Chromium and pressed the stylus button.
          buttonMayBeDown = true;
          await session.send('Input.dispatchMouseEvent', {
            type: 'mousePressed',
            x: lastX,
            y: lastY,
            modifiers: modifiersBitfield,
            button,
            buttons: cdpButtonMask(button),
            clickCount: 1,
            pointerType: 'pen',
            ...cdpPointerTelemetry(start, 0.5),
          });
        } else {
          const session = cdp;
          if (!session) {
            throw new ActionError('touch CDP session 不可用', 'browser_unavailable');
          }
          const startTelemetry = pointerTelemetry(start, 'pointer gesture start');
          touchMayBeActive = true;
          await session.send('Input.dispatchTouchEvent', {
            type: 'touchStart',
            modifiers: modifiersBitfield,
            touchPoints: [{
              x: lastX,
              y: lastY,
              id: 1,
              force: startTelemetry.pressure ?? 1,
              ...(startTelemetry.width === undefined
                ? {}
                : { radiusX: startTelemetry.width / 2 }),
              ...(startTelemetry.height === undefined
                ? {}
                : { radiusY: startTelemetry.height / 2 }),
              ...(startTelemetry.tangentialPressure === undefined
                ? {}
                : { tangentialPressure: startTelemetry.tangentialPressure }),
              ...(startTelemetry.tiltX === undefined
                ? {}
                : { tiltX: startTelemetry.tiltX }),
              ...(startTelemetry.tiltY === undefined
                ? {}
                : { tiltY: startTelemetry.tiltY }),
              ...(startTelemetry.twist === undefined
                ? {}
                : { twist: startTelemetry.twist }),
            }],
          });
        }
        let elapsed = 0;
        for (let pointIndex = 0; pointIndex < points.length; pointIndex += 1) {
          const point = points[pointIndex];
          const delay = point.elapsedMs - elapsed;
          if (delay > 0) await ctx.page.waitForTimeout(delay);
          dispatchTimeout(ctx);
          lastX = box.x + point.x;
          lastY = box.y + point.y;
          if (pointerType === 'mouse') {
            await ctx.page.mouse.move(lastX, lastY);
          } else if (pointerType === 'pen') {
            const session = cdp;
            if (!session) {
              throw new ActionError('pen CDP session 已关闭', 'browser_unavailable');
            }
            await session.send('Input.dispatchMouseEvent', {
              type: 'mouseMoved',
              x: lastX,
              y: lastY,
              modifiers: modifiersBitfield,
              button: 'none',
              buttons: cdpButtonMask(button),
              pointerType: 'pen',
              ...cdpPointerTelemetry(point, 0.5),
            });
          } else {
            const session = cdp;
            if (!session) {
              throw new ActionError('touch CDP session 已关闭', 'browser_unavailable');
            }
            const telemetry = pointerTelemetry(
              point,
              'pointer gesture touch point',
            );
            const endpointReleaseSample = (
              pointIndex === points.length - 1
              && telemetry.pressure === 0
            );
            const activePressure = endpointReleaseSample
              ? lastTouchPressure
              : (telemetry.pressure ?? lastTouchPressure);
            if (activePressure > 0) lastTouchPressure = activePressure;
            await session.send('Input.dispatchTouchEvent', {
              type: 'touchMove',
              modifiers: modifiersBitfield,
              touchPoints: [{
                x: lastX,
                y: lastY,
                id: 1,
                // Every recorder stream ends with its pointerup sample. Move
                // the still-active contact to that endpoint using the last
                // active pressure; touchEnd below emits pressure=0.
                force: activePressure,
                ...(telemetry.width === undefined
                  ? {}
                  : { radiusX: telemetry.width / 2 }),
                ...(telemetry.height === undefined
                  ? {}
                  : { radiusY: telemetry.height / 2 }),
                ...(telemetry.tangentialPressure === undefined
                  ? {}
                  : { tangentialPressure: telemetry.tangentialPressure }),
                ...(telemetry.tiltX === undefined
                  ? {}
                  : { tiltX: telemetry.tiltX }),
                ...(telemetry.tiltY === undefined
                  ? {}
                  : { tiltY: telemetry.tiltY }),
                ...(telemetry.twist === undefined
                  ? {}
                  : { twist: telemetry.twist }),
              }],
            });
          }
          elapsed = point.elapsedMs;
        }
        if (pointerType === 'mouse') {
          await ctx.page.mouse.up({ button });
          buttonMayBeDown = false;
        } else if (pointerType === 'pen') {
          const session = cdp;
          if (!session) {
            throw new ActionError('pen CDP session 已关闭', 'browser_unavailable');
          }
          const endpoint = points[points.length - 1];
          await session.send('Input.dispatchMouseEvent', {
            type: 'mouseReleased',
            x: lastX,
            y: lastY,
            modifiers: modifiersBitfield,
            button,
            buttons: 0,
            clickCount: 1,
            pointerType: 'pen',
            ...cdpPointerTelemetry(endpoint, 0),
            force: 0,
          });
          buttonMayBeDown = false;
        } else {
          const session = cdp;
          if (!session) {
            throw new ActionError('touch CDP session 已关闭', 'browser_unavailable');
          }
          await session.send('Input.dispatchTouchEvent', {
            type: 'touchEnd',
            modifiers: modifiersBitfield,
            touchPoints: [],
          });
          touchMayBeActive = false;
        }
        // Release modifiers before withCompletion begins its post-action waits.
        // A gesture may navigate; holding Ctrl/Shift throughout load completion
        // would contaminate unrelated browser/page input during that wait.
        for (let index = pressedModifiers.length - 1; index >= 0; index -= 1) {
          const modifier = pressedModifiers[index];
          await ctx.page.keyboard.up(modifier);
          pressedModifiers.splice(index, 1);
        }
      } finally {
        // `withCompletion` can surface a dialog while retaining this callback.
        // Keep the CDP session/button/contact owned by that retained operation;
        // detaching from the outer frame would race its still-pending command.
        try {
          await cleanupPointerState();
        } finally {
          actionOwnsCleanup = false;
        }
      }
    });
  } catch (error) {
    translateAfterDispatch(error);
  } finally {
    if (!actionOwnsCleanup) await cleanupPointerState();
  }
}

export async function resize(
  ctx: ActionContext,
  width: number,
  height: number,
): Promise<void> {
  const checkedWidth = finiteMouseNumber(width, 'resize width');
  const checkedHeight = finiteMouseNumber(height, 'resize height');
  try {
    dispatchTimeout(ctx);
    // Preserve the official z.number surface and let Playwright/CDP reject
    // browser-invalid dimensions rather than inventing a product-side cap.
    await ctx.page.setViewportSize({ width: checkedWidth, height: checkedHeight });
  } catch (error) {
    translateAfterDispatch(error);
  }
}

export async function drop(
  ctx: ActionContext,
  nativeRef: string,
  payload: DropPayload,
): Promise<void> {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new ActionError('drop payload 无效', 'invalid_input');
  }
  const files = payload.files;
  const data = payload.data;
  if (
    files !== undefined
    && (!Array.isArray(files) || files.some((file) => typeof file !== 'string'))
  ) {
    throw new ActionError('drop files 必须是字符串数组', 'invalid_upload');
  }
  if (
    data !== undefined
    && (
      !data
      || typeof data !== 'object'
      || Array.isArray(data)
      || Object.entries(data).some(
        ([mime, value]) => typeof mime !== 'string' || typeof value !== 'string',
      )
    )
  ) {
    throw new ActionError('drop data 必须是 MIME type 到字符串的 object', 'invalid_input');
  }
  if (!(files?.length) && data === undefined) {
    throw new ActionError('drop 至少需要 files 或显式 data', 'invalid_input');
  }
  const { locator } = await resolveMutation(ctx, nativeRef);
  const playwrightPayload: {
    files?: string | string[];
    data?: Record<string, string>;
  } = {};
  if (files?.length) playwrightPayload.files = files.length === 1 ? files[0] : files;
  if (data !== undefined) playwrightPayload.data = data;
  const emptyDataTransfer = (
    !files?.length
    && data !== undefined
    && Object.keys(data).length === 0
  );
  try {
    await withCompletion(ctx, async () => {
      if (emptyDataTransfer) {
        // Playwright's public drop payload validator rejects `{ data: {} }`
        // even though the MCP schema/handler accepts an explicitly supplied
        // empty object. Preserve that useful external-drag primitive without
        // inventing hidden sentinel data: dispatch the browser's real
        // DragEvent sequence with a genuinely empty DataTransfer.
        await locator.evaluate(
          (element) => {
            const transfer = new DataTransfer();
            for (const type of ['dragenter', 'dragover', 'drop']) {
              element.dispatchEvent(new DragEvent(type, {
                bubbles: true,
                cancelable: true,
                composed: true,
                dataTransfer: transfer,
              }));
            }
          },
          undefined,
          { timeout: dispatchTimeout(ctx) },
        );
      } else {
        await locator.drop(playwrightPayload, {
          timeout: dispatchTimeout(ctx),
        });
      }
    });
  } catch (error) {
    const message = firstLine(error);
    if (/\b(?:ENOENT|EACCES|EPERM|EISDIR)\b|not (?:a regular )?file/i.test(message)) {
      throw new ActionError(`拖放文件无效：${message}`, 'invalid_upload');
    }
    translateAfterDispatch(error);
  }
}

export async function upload(
  ctx: ActionContext,
  nativeRef: string,
  files: string[],
): Promise<void> {
  // An empty list is Playwright's official "clear this file input" primitive and is required
  // to replay recorder v5 uploadMode=clear.
  if (!Array.isArray(files) || files.some((file) => typeof file !== 'string')) {
    throw new ActionError('上传文件列表无效', 'invalid_upload');
  }
  const resolved = await resolveMutation(ctx, nativeRef);
  try {
    await withCompletion(ctx, async () => {
      await resolved.locator.setInputFiles(files, { timeout: dispatchTimeout(ctx) });
    });
  } catch (error) {
    const message = firstLine(error);
    // Playwright validates the DOM target before setting files or dispatching
    // input/change. Preserve that deterministic pre-dispatch classification
    // without a separate Runtime.evaluate round trip.
    if (
      /HTMLInputElement/i.test(message)
      || /input.*type[='\" ]*file/i.test(message)
      || /not (?:an? )?(?:input|file input)/i.test(message)
    ) {
      throw new ActionError('上传目标不是文件输入框', 'invalid_upload_target');
    }
    // Playwright resolves local payloads before sending the browser mutation.
    // Filesystem failures therefore prove that no input/change event was
    // dispatched and are safe to report as deterministic invalid input.
    if (/\b(?:ENOENT|EACCES|EPERM|EISDIR)\b|not (?:a regular )?file/i.test(message)) {
      throw new ActionError(`上传文件无效：${message}`, 'invalid_upload');
    }
    translateAfterDispatch(error);
  }
}

/**
 * Complete a browser-native FileChooser captured before/while clicking a styled upload button.
 *
 * `files === undefined` means cancel, matching upstream browser_file_upload. Cancellation does
 * not call setFiles; an intercepted chooser has no persistent native dialog to dismiss. An
 * explicit empty array remains distinct and clears the backing input.
 */
export async function uploadFileChooser(
  ctx: ActionContext,
  chooser: FileChooser,
  files: string[] | undefined,
): Promise<void> {
  if (files === undefined) return;
  if (!Array.isArray(files) || files.some((file) => typeof file !== 'string')) {
    throw new ActionError('上传文件列表无效', 'invalid_upload');
  }
  try {
    await withCompletion(ctx, async () => {
      await chooser.setFiles(files, { timeout: dispatchTimeout(ctx) });
    });
  } catch (error) {
    const message = firstLine(error);
    if (/\b(?:ENOENT|EACCES|EPERM|EISDIR)\b|not (?:a regular )?file/i.test(message)) {
      throw new ActionError(`上传文件无效：${message}`, 'invalid_upload');
    }
    translateAfterDispatch(error);
  }
}

/**
 * 把稳定 selector 解析成与 snapshot ref 同构的记录。
 *
 * 0/多匹配拒绝；语义/指纹材料尽力采集，但它们不是普通动作的执行前置条件。
 */
export async function locateBySelector(
  ctx: ActionContext,
  nativeRef: string,
  selector: string,
  _hashOf: (value: string) => string,
): Promise<RefRecord> {
  const locator = ctx.page.locator(selector);
  let count: number;
  try {
    count = await locator.count();
  } catch (error) {
    throw new ActionError(`选择器无效：${firstLine(error)}`, 'invalid_selector');
  }
  if (count === 0) {
    throw new ActionError(
      '当前页面上找不到该元素，请重新观察后判断流程是否走岔',
      'selector_no_match',
    );
  }
  if (count > 1) {
    throw new ActionError(
      `该身份在当前页面上匹配到 ${count} 个元素，无法确定目标，请重新观察`,
      'selector_ambiguous',
    );
  }

  // locate is an execution primitive, not a diagnostics/code-generation probe.
  // Execute the persisted selector as-is; normalize/evaluate/AX reads stay off
  // this hot path. The exact @sN handle is sufficient functional identity, so
  // do not manufacture fingerprints or an auxiliary security key.
  const role = 'generic';
  const name = '';
  const record: RefRecord = {
    selector,
    playwrightRef: '',
    role,
    name,
    securityKey: nativeRef,
    security: '',
    navigation: '',
    downloadNavigation: '',
    action: '',
    actionKind: 'activate',
    semanticRole: role,
    semanticName: name,
    documentBaseURI: '',
    documentURL: '',
    tag: '',
    inputType: '',
    contentEditable: false,
    fieldTier: 'plain',
  };
  ctx.refs.set(nativeRef, record);
  return record;
}

/** 只读动作不执行 mutation；仍要求 ref 属于当前 ref 表。 */
export async function textOf(ctx: ActionContext, nativeRef: string): Promise<string> {
  const { locator } = await resolve(ctx, nativeRef);
  return (await locator.textContent({ timeout: dispatchTimeout(ctx) })) ?? '';
}

export async function attributeOf(
  ctx: ActionContext,
  nativeRef: string,
  name: string,
): Promise<string> {
  const { locator } = await resolve(ctx, nativeRef);
  return (await locator.getAttribute(name, { timeout: dispatchTimeout(ctx) })) ?? '';
}
