/**
 * 浏览器录制器：注入页面的事件捕获脚本，以及它与宿主之间的契约。
 *
 * ## 为什么使用 document world
 *
 * Electron 43 / Chromium 150 的真实 OOPIF 上，自定义 isolated world（包括
 * runImmediately 与 createIsolatedWorld）会让主 renderer 的 Runtime 通道永久挂死。
 * 因此录制器以 document-start 脚本运行在 document world。状态保留在模块闭包里，
 * Host 为每段录制分配独立 binding/stash/control 名，并校验 session、schema
 * 与 Event.isTrusted；原生输入关联只做审计统计，不作为 IME/粘贴的丢弃门槛。
 *
 * ## 分级是元数据，不是功能开关
 *
 * schema v10 为了可靠复现，会完整记录所有可编辑字段（包括密码与 OTP）的最终值、
 * 目标、选择器和 URL。`FieldTier` 仍保留给 UI/编译器描述字段语义，但不得改变
 * 录制、回放或生成 skill 的能力。
 */

/** 旧版隔离世界名，仅供升级兼容；当前 Electron 路径不再创建该 world。 */
export const RECORDER_WORLD = 'crew-recorder';

/** 默认绑定名前缀；生产 Host 会附加每段录制的随机后缀。 */
export const RECORDER_BINDING = '__crewRecorderEmit';

/** 页面侧暂存最近若干个事件目标的默认全局名前缀。 */
export const RECORDER_TARGET_STASH = '__crewRecorderTargets';

/** 页面侧生命周期控制器。生产 Host 可为每次录制传入随机名称。 */
export const RECORDER_CONTROL = '__crewRecorderControl';

/**
 * 兼容旧调用方的导出。schema v10 不再用固定产品上限截断录制证据。
 * JavaScript/Chromium 自身的内存与安全整数边界仍然自然生效。
 */
export const RECORDER_STASH_SIZE = Number.POSITIVE_INFINITY;
export const RECORDER_VALUE_MAX = Number.POSITIVE_INFINITY;
export const RECORDER_DIRTY_INPUT_MAX = Number.POSITIVE_INFINITY;

/**
 * 注入侧事件 schema。
 *
 * 版本只在增加/改变可持久化字段语义时递增。宿主与 Python bridge 都按这个版本
 * 做显式白名单，未知版本直接拒绝，避免新旧桌面/网关混跑时把未知页面字段落盘。
 */
export const RECORDER_EVENT_SCHEMA_VERSION = 10;

export const RECORDER_SELECTOR_MAX = Number.POSITIVE_INFINITY;
export const RECORDER_SELECT_VALUE_MAX = Number.POSITIVE_INFINITY;
export const RECORDER_UPLOAD_FILE_MAX = Number.POSITIVE_INFINITY;
export const RECORDER_UPLOAD_PATH_MAX = Number.POSITIVE_INFINITY;
export const RECORDER_UPLOAD_PATHS_TOTAL_MAX = Number.POSITIVE_INFINITY;
export const RECORDER_UPLOAD_ACCEPT_MAX = Number.POSITIVE_INFINITY;
export const RECORDER_UPLOAD_REPORTED_COUNT_MAX = Number.POSITIVE_INFINITY;

/** 同步目标证据与来源说明的独立版本。 */
export const RECORDER_PROVENANCE_SCHEMA_VERSION = 1;

/**
 * 凭据分级。
 *
 * - `identifier`：工号 / 手机号 / 邮箱 / 用户名。不是秘密，记录值并在编译期
 *   参数化成技能入参（值本身不会硬编码进 SKILL.md，见设计文档 §5）。
 * - `secret`：密码。
 * - `handoff`：短信验证码 / 图形验证码。
 * - `plain`：普通输入，正常记录。
 *
 * schema v10 中四档都保留完整值并可直接回放；tier 只表达语义。
 */
export type FieldTier = 'plain' | 'identifier' | 'secret' | 'handoff';

/** 判定分级所需的元素属性。全部取自页面，一律当不可信输入。 */
export interface FieldProbe {
  type: string;
  autocomplete: string;
  name: string;
  id: string;
  placeholder: string;
  ariaLabel: string;
  /** Associated <label> and aria-labelledby text. */
  labelText: string;
}

/**
 * 输入框分级判定。
 *
 * **这个函数会被 `recorderScript()` 用 `toString()` 原样嵌进注入脚本**——注入的
 * 代码就是这里被测的代码，不存在"实现与测试各写一份然后慢慢漂移"的问题。
 * 因此它必须完全自包含：不引用任何模块作用域的标识符，不用 TS 专有语法。
 *
 * 判定只使用明确、高置信度的密码/OTP 证据。普通长文本、缺失标签或页面属性解析
 * 异常不能把正常表单一律标错；但无论分级结果如何，schema v10 都完整记录并回放。
 */
export function classifyFieldTier(probe: FieldProbe): FieldTier {
  const textOf = (value: unknown) => String(value || '');
  const type = textOf(probe.type).toLowerCase();
  if (type === 'password') return 'secret';
  // autocomplete 是空格分隔的 token 列表（HTML 规范允许
  // "section-login shipping current-password" 这种写法），按整串精确比较必然漏判：
  // 实测 "section-login one-time-code" 与 "cc-number" 都会被当成普通输入。
  // 注意：本函数体会被 toString() 嵌进模板字符串，注释里不能出现反引号。
  const autoTokens = textOf(probe.autocomplete).toLowerCase().split(/\s+/).filter(Boolean);
  if (autoTokens.indexOf('current-password') >= 0 || autoTokens.indexOf('new-password') >= 0) return 'secret';
  if (autoTokens.indexOf('one-time-code') >= 0) return 'handoff';
  // 支付类 token 的值就是凭据本身
  const CC_SECRET = ['cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year'];
  if (CC_SECRET.some((token) => autoTokens.indexOf(token) >= 0)) return 'secret';
  // 先把 camelCase 与各种分隔符拆成词再小写。不做这一步，"loginPwd" 小写成
  // "loginpwd" 之后 "pwd" 前面是字母、词边界不成立，密码框就会被漏判成普通输入
  // ——这是安全方向的漏判，比误判严重得多。
  const hay = ' ' + [
    textOf(probe.name),
    textOf(probe.id),
    textOf(probe.placeholder),
    textOf(probe.ariaLabel),
    textOf(probe.labelText),
  ]
    .join(' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[^A-Za-z0-9一-鿿]+/g, ' ')
    .toLowerCase() + ' ';
  // 复合词内部允许一个可选空格：拆词之后 "smsCode" / "sms-code" / "smscode"
  // 分别变成 "sms code" / "sms code" / "smscode"，三种写法要一网打尽。
  // 词两侧的空格是硬边界，"passwordless"、"accountant"、"hotelName" 不会误命中。
  if (/ (otp|captcha|verify ?code|vcode|sms ?code|auth ?code) |验证码|校验码|动态码/.test(hay)) {
    return 'handoff';
  }
  // 仅保留密码类高置信度词。token、账号、卡号、PIN 等词在业务表单里歧义很大，
  // 不能仅凭名字就让普通填写失效；显式 autocomplete 支付 token 已在上面处理。
  const SECRET_WORDS = [
    'password', 'passwd', 'pwd', 'passcode',
  ];
  const SECRET_CJK = /密码|口令/;
  if (new RegExp(' (' + SECRET_WORDS.join('|') + ') ').test(hay) || SECRET_CJK.test(hay)) {
    return 'secret';
  }
  const ID_TOKENS = ['username', 'email', 'tel', 'tel-national', 'cc-name'];
  if (ID_TOKENS.some((token) => autoTokens.indexOf(token) >= 0)) return 'identifier';
  if (
    / (account|user ?name|user ?id|login ?name|staff ?(no|code)|emp ?no|mobile|phone|email) |工号|账号|帐号|手机号|邮箱|用户名/
      .test(hay)
  ) {
    return 'identifier';
  }
  return 'plain';
}

export type RecorderEventType =
  | 'click'
  | 'dblclick'
  | 'drag'
  | 'drop'
  | 'dialog'
  | 'hover'
  | 'input'
  | 'upload'
  | 'submit'
  | 'key'
  | 'pointerGesture'
  | 'scroll'
  | 'wheel'
  | 'navigate';

export type RecorderClickButton = 'left' | 'middle' | 'right';
export type RecorderModifier = 'Alt' | 'Control' | 'Meta' | 'Shift';
export type RecorderPointerType = 'mouse' | 'pen' | 'touch';
export type RecorderUploadMode = 'paths' | 'handoff' | 'clear';
export type RecorderDialogAction = 'accept' | 'dismiss';
export type RecorderDialogType = 'alert' | 'confirm' | 'prompt' | 'beforeunload';
export interface RecorderPoint {
  x: number;
  y: number;
}

export interface RecorderPointerSample extends RecorderPoint {
  pressure?: number;
  tangentialPressure?: number;
  tiltX?: number;
  tiltY?: number;
  twist?: number;
  width?: number;
  height?: number;
}

export interface RecorderGesturePoint extends RecorderPointerSample {
  elapsedMs: number;
}

export type RecorderTargetEvidence = 'synchronous' | 'redacted' | 'none';

/**
 * 这份 provenance 只描述「数据怎么取得」，不声称事件一定来自真人。
 *
 * `browserTrusted` 对应 Chromium 的 Event.isTrusted；它挡得住 dispatchEvent，
 * 挡不住页面调用 focus()/blur()/requestSubmit() 触发的浏览器事件。真人来源必须
 * 由 BrowserHost 再与 Electron 原生输入做时间关联，不能拿这个位替代。
 */
export interface RecorderProvenance {
  schemaVersion: typeof RECORDER_PROVENANCE_SCHEMA_VERSION;
  source:
    | 'document-world'
    | 'isolated-world'
    | 'legacy-isolated-world'
    | 'host-navigation'
    | 'browser-host';
  capturePhase: 'event-callback' | 'host';
  browserTrusted: boolean;
  targetEvidence: RecorderTargetEvidence;
  /**
   * 注入侧恒为 unverified；BrowserHost 与 Electron 原生输入关联成功后改成
   * correlated，宿主自己生成的 navigate 用 host。bridge 的 v2 契约不接受
   * unverified 事件落盘。
   */
  nativeInput?: 'unverified' | 'correlated' | 'host';
}

export interface RecorderTarget {
  tag: string;
  text: string;
  ariaLabel: string;
  href: string;
  /** 同 tag 同文本的元素中，本元素在文档序里排第几（从 1 起）。 */
  ordinal: number;
  /**
   * 以下字段都在事件回调里同步读取并完整保留。
   *
   * 导航点击返回后旧 document 可能已经销毁，宿主此时无法再向 Playwright 询问
   * locator。它可以用这些 token 生成一个待严格校验的候选 selector；回放时仍须
   * 0/多匹配拒绝，不能把页面给的 token 当成已验证的唯一身份。
   */
  id: string;
  name: string;
  role: string;
  inputType: string;
  /** Browser-normalized editability proof; distinguishes generic contenteditable targets. */
  contentEditable: boolean;
  testId: string;
  testIdAttribute: '' | 'data-testid' | 'data-test' | 'data-qa';
  /**
   * 元素在**本框架内**的唯一 CSS 路径，事件回调里同步算出。
   *
   * 它只是个「够用的临时身份」——宿主拿它构造 Locator，再由 Playwright 的
   * `normalize()` 升级成 role/testid 优先的稳定选择器写进技能。不要指望它本身稳定。
   */
  cssPath: string;
  /**
   * 从顶层文档到本框架的 iframe 链。schema v7 在同源祖先上由各父文档的
   * Playwright InjectedScript 同步生成；OOPIF 无法读取 frameElement，Host 会
   * 依据 CDP frame/session 拓扑重建。主文档为空数组。
   */
  framePath: string[];
}

export interface RecorderEvent {
  /** 旧宿主手工构造的 navigate 事件暂时允许缺省；解析页面事件时一定存在。 */
  schemaVersion?: 1 | typeof RECORDER_EVENT_SCHEMA_VERSION;
  provenance?: RecorderProvenance;
  /**
   * Host-only arrival time, captured before selector/file resolution enters the
   * shared async ledger. It is never accepted from the page payload.
   */
  capturedAt?: number;
  /** Host-assigned browser-task identity; page payloads must keep this zero. */
  causalId?: number;
  /**
   * Page-local input-burst token. It exists only between the injected recorder
   * and BrowserHost; Host consumes it and persists only causalId.
   */
  causalToken?: number;
  seq: number;
  type: RecorderEventType;
  /** 事件时刻页面 URL。宿主会再校验一次，不直接信任。 */
  url: string;
  /** 目标元素的粗描述，给人看的。 */
  hint: string;
  /**
   * 事件发生的同一时刻、同步取下来的元素身份。
   *
   * 为什么不能只靠 `backendNodeId`：宿主是在收到绑定回调之后**异步**去页面
   * 取暂存元素的，而点击的默认动作（导航）就在捕获阶段回调返回之后立刻发生。
   * 点一个链接跳页时，等宿主去取，文档往往已经换了——实测点「详情」链接必然
   * 拿不到 backendNodeId。而跳页的点击恰恰是工作流里最关键的那些。
   *
   * 所以真正可靠的对齐信息在这里：这些原始信号在事件回调里同步读出，不受导航
   * 影响。编译期用它们与动作前的快照行做匹配。注意这里**不试图复刻无障碍名
   * 计算**（那是 workflow-use 花了 400 行启发式去做、而我们有 AX 树本就不必做
   * 的事），只是把页面给的原始信号原样带出来。
   */
  target: RecorderTarget | null;
  /** drag 终点的同步身份；其它事件为空。 */
  dragTarget?: RecorderTarget | null;
  /** Internal dragTo source/target points in Playwright's padding-box coordinates. */
  dragSourcePosition: RecorderPoint | null;
  dragTargetPosition: RecorderPoint | null;
  /**
   * The exact selector produced synchronously by the installed
   * playwright-core InjectedScript in the event's local document.
   *
   * These two fields are transport-only. BrowserHost prepends the authoritative
   * frame-owner chain and persists the result as `selector` /
   * `targetSelector`; the raw local fragments are never written to the trace.
   */
  recordedSelector?: string;
  recordedDragSelector?: string;
  selectorSource?: 'playwright' | 'unavailable';
  /** input 事件的分级；其余类型为 'plain'。 */
  tier: FieldTier;
  /** 事件时刻的完整最终值；tier 不改变其录制语义。 */
  value: string;
  /**
   * schema v6: exact DOM-order values from HTMLSelectElement.selectedOptions.
   *
   * Playwright's official recorder represents select actions as an array even
   * for a single selected option. Crew keeps `value` for the existing
   * select-one/text contract and uses `values` only for select-multiple, so old
   * v1/v3/v4/v5 traces retain their exact serialized shapes.
   */
  values: string[];
  /** 旧 schema 的兼容字段；schema v10 完整录制时恒为 false。 */
  valueTruncated?: boolean;
  /** 仅供 Host 在 pause/stop 排水阶段识别控制器同步 flush，不进入持久轨迹。 */
  lifecycleFlush?: boolean;
  /** key 事件的键名。 */
  key: string;
  /** Playwright-compatible pointer evidence; schema v4 persists it for exact replay. */
  clickButton?: RecorderClickButton;
  modifiers?: RecorderModifier[];
  /**
   * Continuous-gesture device identity. Missing means mouse for old v11 rows;
   * new pointerGesture recordings always set the browser PointerEvent value.
   */
  pointerType?: RecorderPointerType;
  /** 浏览器 click.detail；Host 用它折叠 dblclick，并写入持久轨迹。 */
  clickCount?: number;
  /**
   * Element-relative point for CANVAS clicks, matching Playwright recorder.
   * Ordinary DOM clicks deliberately keep this null.
   */
  position?: RecorderPoint | null;
  /** Continuous pointer gesture, selector border-box-relative and chronological. */
  gestureStart?: RecorderPointerSample | null;
  gesturePoints?: RecorderGesturePoint[];
  /**
   * Host-captured JavaScript dialog outcome. Page-origin events keep the fixed
   * empty shape; BrowserHost fills these fields from the authoritative CDP
   * opening/closed pair after the human has made the decision.
   */
  dialogAction: RecorderDialogAction | '';
  dialogType: RecorderDialogType | '';
  dialogText: string;
  /** scroll 事件的滚动量。 */
  scrollX: number;
  scrollY: number;
  /**
   * 文件上传在 schema v5 中是一等动作。
   *
   * document-world 只会产生 handoff / clear 且 paths 恒空；页面拿不到可信的
   * 本机路径。BrowserHost 随后通过该事件所属 executionContext/session 中的
   * File RemoteObject + DOM.getFileInfo 把完整可读的一组升级为 paths。
   * 非 upload 事件五个字段必须保持固定空形状。
   */
  uploadMode: RecorderUploadMode | '';
  paths: string[];
  fileCount: number;
  multiple: boolean;
  accept: string;
  /**
   * External DataTransfer string entries, keyed by their exact synchronous
   * format/MIME type. Only `drop` may populate this object. An empty object is
   * meaningful: it replays a trusted external drop with an empty DataTransfer.
   */
  dropData: Record<string, string>;
}

/**
 * 旧调用点仍会经过这个函数。schema v10 的功能优先契约要求它是严格恒等映射：
 * tier 只提供元数据，绝不能再清空值、目标、selector、page 或 URL。
 */
/**
 * 录制证据保留策略。**当前策略是完整保留，不做任何删改。**
 *
 * 名字必须如实：这个函数曾经叫 `enforceRecorderPrivacyPolicy`，而函数体是
 * `return record` —— 任何人读到调用点都会以为这里在按分级抹值。实际上从
 * schema v10 起 `tier` 只是描述性元数据，密码原值会完整落盘。
 *
 * 这是**刻意的**，因为回放需要真实值：登录类工作流没有值就跑不动。代价由
 * 三处承担，都在别的地方：
 *
 * 1. `handoff`（一次性验证码）在编译期强制变成 takeover —— 存下来必然失效；
 * 2. 安装前的审批摘要明确列出「内含 N 个凭据字段的录制原值」；
 * 3. 轨迹与 artifact 都是 owner 私有、0600。
 *
 * 保留这个钩子而不是删掉，是因为它是**唯一**能给部署方插入自定义脱敏的位置：
 * 想要更严的策略，只改这一个函数就够，不必在录制链路上到处找出口。
 */
export function retainRecorderEvidence<T extends Record<string, unknown>>(
  record: T,
  _tier: FieldTier,
): T {
  return record;
}

/**
 * 选择器片段只做类型归一化。Playwright selector 本身允许内部引擎、Unicode、
 * 转义符与跨 frame 分段；过滤这些合法语法会让真实网站无法录制。
 */
export function sanitizeSelectorFragment(raw: unknown): string {
  return typeof raw === 'string' ? raw : '';
}

/**
 * 目标证据只做类型归一化。真实 DOM id/name/test-id 可以包含空格、Unicode、
 * 引号和转义字符，录制层必须保留原文。
 */
export function sanitizeRecorderEvidenceToken(raw: unknown, _maximum = Number.POSITIVE_INFINITY): string {
  return typeof raw === 'string' ? raw : '';
}

/**
 * 注入脚本源码。
 *
 * 保持自包含、无依赖、不抛异常：它跑在用户正在操作的真实页面上，任何未捕获
 * 异常都可能干扰用户自己的操作。所有回调整体 try/catch。
 */
export interface RecorderScriptOptions {
  /** Host 在 document-start 永久安装时传 false，开始录制后再 activate。 */
  initiallyActive?: boolean;
  /**
   * v11 records semantic hover and raw wheel gestures in addition to the v10
   * post-layout scroll contract.  Keep v10 byte/behaviour compatibility unless
   * the recording ledger explicitly opted into v11.
   */
  recordingSchemaVersion?: 10 | 11;
  /** main-world 注入必须使用每次录制随机、不可预测的名称。 */
  bindingName?: string;
  targetStashName?: string;
  controlName?: string;
  /**
   * Exact generated InjectedScript source from the installed playwright-core.
   *
   * BrowserHost always supplies this in production. Keeping it optional makes
   * the small VM unit harness independent of Chromium; an absent/broken engine
   * reports `selectorSource: unavailable` and the Host marks the recording
   * incomplete instead of silently persisting Crew's old heuristic.
   */
  officialSelectorSource?: string;
}

export function recorderScript(options: RecorderScriptOptions = {}): string {
  const bindingName = options.bindingName || RECORDER_BINDING;
  const targetStashName = options.targetStashName || RECORDER_TARGET_STASH;
  const controlName = options.controlName || RECORDER_CONTROL;
  const initiallyActive = options.initiallyActive !== false;
  const recordingV11 = options.recordingSchemaVersion === 11;
  const officialSelectorBootstrap = options.officialSelectorSource
    ? `
  let officialSelectorFor = null;
  try {
    const officialModule = { exports: {} };
    ((module) => {
${options.officialSelectorSource}
    })(officialModule);
    let InjectedScript = officialModule.exports && officialModule.exports.InjectedScript;
    // Playwright's generated bundle currently exports a zero-argument thunk.
    // Accept a future direct-class export as well, but nothing else.
    if (
      typeof InjectedScript === 'function'
      && !(
        InjectedScript.prototype
        && typeof InjectedScript.prototype.generateSelectorSimple === 'function'
      )
    ) {
      InjectedScript = InjectedScript();
    }
    if (
      typeof InjectedScript !== 'function'
      || !InjectedScript.prototype
      || typeof InjectedScript.prototype.generateSelectorSimple !== 'function'
    ) throw new Error('invalid Playwright InjectedScript export');
    const injectedScript = new InjectedScript(window, {
      isUnderTest: false,
      sdkLanguage: 'javascript',
      frameSeq: 0,
      testIdAttributeName: 'data-testid',
      stableRafCount: 1,
      browserName: 'chromium',
      shouldPrependErrorPrefix: false,
      isUtilityWorld: false,
      customEngines: [],
    });
    officialSelectorFor = (element) => {
      try {
        const selector = String(injectedScript.generateSelectorSimple(element) || '');
        return selector && selector !== 'error:notconnected' ? selector : '';
      } catch (_) {
        return '';
      }
    };
  } catch (_) {
    officialSelectorFor = null;
  }`
    : '  const officialSelectorFor = null;';
  // 分级函数原样嵌入：注入的就是上面被单测覆盖的那份实现。
  return `(() => {
  const classifyFieldTier = ${classifyFieldTier.toString()};
  const BINDING_NAME = ${JSON.stringify(bindingName)};
  const TARGET_STASH_NAME = ${JSON.stringify(targetStashName)};
  const CONTROL_NAME = ${JSON.stringify(controlName)};
  const RECORDING_V11 = ${recordingV11 ? 'true' : 'false'};
  const RECORDING_SCHEMA_VERSION = ${recordingV11 ? '11' : '10'};
  const existingControl = globalThis[CONTROL_NAME];
  if (
    existingControl
    && existingControl.schemaVersion === ${RECORDER_EVENT_SCHEMA_VERSION}
    && existingControl.recordingSchemaVersion === RECORDING_SCHEMA_VERSION
  ) {
    if (${initiallyActive ? 'true' : 'false'}) {
      if (typeof existingControl.activate === 'function') existingControl.activate();
    } else if (typeof existingControl.deactivate === 'function') {
      existingControl.deactivate();
    }
    return;
  }
  try {
    if (existingControl && typeof existingControl.deactivate === 'function') {
      existingControl.deactivate();
    }
  } catch (_) { /* incompatible prior recorder must not stay active */ }
${officialSelectorBootstrap}
  const stash = new Map();
  try {
    Object.defineProperty(globalThis, TARGET_STASH_NAME, {
      value: stash, configurable: true, enumerable: false, writable: false,
    });
  } catch (_) {
    globalThis[TARGET_STASH_NAME] = stash;
  }
  let seq = 0;
  let active = ${initiallyActive ? 'true' : 'false'};

  const emit = (event) => {
    try {
      const binding = globalThis[BINDING_NAME];
      if (typeof binding === 'function') binding(JSON.stringify(event));
    } catch (_) { /* 宿主未就绪 */ }
  };

  // 事件目标按 seq 暂存，宿主随后取回来解析 backendNodeId。不能按固定数量淘汰：
  // 页面主线程或 CDP 短暂繁忙时，任意一个被淘汰的目标都会让那一步失去精确定位。
  const stashTarget = (id, value) => {
    stash.set(id, value);
  };

  // 属性必须保留原始大小写。tierOf 里的 classifyFieldTier 会先拆 camelCase 再小写；
  // 若这里提前把 loginPwd 变成 loginpwd，词边界就永远找不回来了，真实注入链会
  // 把密码框误判成 plain（只测 classifyFieldTier 纯函数发现不了这类接线错误）。
  const attributeEvidence = (element, name, _maximum) => {
    try {
      const value = String(element.getAttribute(name) || '');
      return { value, complete: true };
    } catch (_) {
      return { value: '', complete: false };
    }
  };
  const attr = (element, name) => {
    return attributeEvidence(element, name, 4096).value;
  };

  // Preserve the complete descendant text. This remains target evidence rather than an
  // accessibility-name implementation; Playwright normalize() is the selector scorer.
  const textEvidence = (element) => {
    try {
      const owner = element && element.ownerDocument ? element.ownerDocument : document;
      const showText = owner.defaultView && owner.defaultView.NodeFilter
        ? owner.defaultView.NodeFilter.SHOW_TEXT
        : (typeof NodeFilter !== 'undefined' ? NodeFilter.SHOW_TEXT : 4);
      const walker = owner.createTreeWalker(element, showText);
      const parts = [];
      while (true) {
        const node = walker.nextNode();
        if (!node) break;
        parts.push(String(node.nodeValue || ''));
      }
      return {
        text: parts.join('').replace(/\\s+/g, ' ').trim(),
        complete: true,
      };
    } catch (_) {
      return { text: '', complete: false };
    }
  };
  const targetText = (element) => {
    return textEvidence(element).text;
  };
  // Form values follow Playwright Recorder semantics, not selector-evidence
  // semantics. In particular, contenteditable uses the browser-normalized
  // innerText verbatim: newlines and meaningful spaces must survive replay.
  const editableValue = (element) => {
    try {
      const raw = String(element.innerText || '');
      return { text: raw, complete: true };
    } catch (_) {
      return { text: '', complete: false };
    }
  };

  // DOM token 原样保留；真实站点的 id/name/test-id 可以包含 Unicode 与转义符。
  const evidenceToken = (value, _maximum) => {
    try {
      return String(value || '');
    } catch (_) { return ''; }
  };

  // 只看 value 属性存不存在是不够的：HTMLButtonElement 也有 value，于是点完
  // 按钮一失焦就会多发一条假的「输入」事件（实测到过）。必须按标签与类型判定。
  // 注意：本函数体在模板字符串里，注释中不能出现反引号。
  const NON_TEXT_INPUT_TYPES = new Set(['button', 'submit', 'reset', 'image', 'file', 'hidden']);
  const editingHostOf = (element) => {
    try {
      if (!element || !element.isContentEditable) return null;
      let current = element;
      while (current) {
        let parent = current.parentElement;
        if (!parent) {
          const root = current.getRootNode && current.getRootNode();
          parent = root && root.host && root.host.nodeType === 1 ? root.host : null;
        }
        if (!parent || !parent.isContentEditable) return current;
        current = parent;
      }
      return null;
    } catch (_) {
      return null;
    }
  };
  const inputTargetOf = (element) => editingHostOf(element) || element;
  const isTextEntry = (element) => {
    try {
      // contenteditable 富文本框（工单系统的处理意见常用它）也是输入源。
      if (editingHostOf(element)) return true;
      const tag = String(element.tagName || '').toUpperCase();
      if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
      if (tag !== 'INPUT') return false;
      const inputType = String(element.type || 'text').toLowerCase();
      // checkbox / radio 不是文本输入，但它们的勾选同样是工作流动作
      //（筛选条件、单选项），要记。值走 commitInput 里的状态分支。
      if (inputType === 'checkbox' || inputType === 'radio') return true;
      return !NON_TEXT_INPUT_TYPES.has(inputType);
    } catch (_) { return false; }
  };

  const tierOf = (element) => {
    let directTier = 'plain';
    try {
      const labels = [];
      const probe = {};
      const probeAttributes = ['autocomplete', 'name', 'id', 'placeholder', 'aria-label'];
      for (let index = 0; index < probeAttributes.length; index += 1) {
        const name = probeAttributes[index];
        probe[name] = attributeEvidence(element, name, 4096).value;
      }
      // Check direct native evidence before walking fallible label relationships. A missing
      // label must never hide explicit type=password or autocomplete=one-time-code.
      directTier = classifyFieldTier({
        type: String(element.type || attr(element, 'type') || ''),
        autocomplete: probe.autocomplete,
        name: probe.name,
        id: probe.id,
        placeholder: probe.placeholder,
        ariaLabel: probe['aria-label'],
        labelText: '',
      });
      if (directTier === 'secret' || directTier === 'handoff') return directTier;

      const labelledByEvidence = attributeEvidence(element, 'aria-labelledby', 4096);
      const labelledBy = labelledByEvidence.value.split(/\\s+/).filter(Boolean);
      const root = element.getRootNode && element.getRootNode();
      for (let index = 0; index < labelledBy.length; index += 1) {
        const id = labelledBy[index];
        const labelled = (
          root && typeof root.getElementById === 'function' ? root.getElementById(id) : null
        ) || (typeof document.getElementById === 'function' ? document.getElementById(id) : null);
        if (!labelled) continue;
        labels.push(textEvidence(labelled).text);
      }
      const associated = element.labels;
      if (associated) {
        const maximum = Number(associated.length) || 0;
        for (let index = 0; index < maximum; index += 1) {
          labels.push(textEvidence(associated[index]).text);
        }
      }
      return classifyFieldTier({
        type: String(element.type || attr(element, 'type') || ''),
        autocomplete: probe.autocomplete,
        name: probe.name,
        id: probe.id,
        placeholder: probe.placeholder,
        ariaLabel: probe['aria-label'],
        labelText: labels.join(' '),
      });
    } catch (_) {
      // Broken custom-control metadata is common. Direct password/OTP evidence was already
      // checked; parser failure alone must not discard ordinary input.
      return directTier;
    }
  };

  // hint 只用于人读，绝不能读取输入控件的 value。
  //
  // 早先这里写的是 innerText || value：密码框没有 innerText、也常常没有 aria-label，
  // 于是明文密码直接进了 hint —— 而后续各层只清 value 不清 hint，密码就一路落盘。
  // 输入控件一律只用它的标签性描述（aria-label / placeholder / name），永不取值。
  const hintOf = (element) => {
    try {
      const tag = String(element.tagName || '').toLowerCase();
      const label = isTextEntry(element)
        ? (attr(element, 'aria-label') || attr(element, 'placeholder') || attr(element, 'name'))
        : (attr(element, 'aria-label')
          || String(element.innerText || '').trim());
      return label ? tag + ' ' + label : tag;
    } catch (_) { return ''; }
  };

  // 在事件回调里同步取下元素身份。导航会在回调返回后立刻发生，任何异步取值
  // 都赶不上——这是唯一可靠的时机。
  // 元素在**本框架内**的唯一 CSS 路径。
  //
  // 它不需要好看也不需要稳定——只要能在录制发生的这一刻唯一命中该元素即可。宿主
  // 随后把它交给 Playwright 的 normalize()，由 codegen 同一套评分升级成 role/testid
  // 优先的稳定选择器。**不要在这里自己发明选择器质量启发式**：那正是 workflow-use
  // 花了几百行去做、而 Playwright 已经做得更好的事。
  const cssPathOf = (element) => {
    try {
      const parts = [];
      let node = element;
      while (node && node.nodeType === 1) {
        const tag = String(node.tagName || '').toLowerCase();
        if (!tag) break;
        // id 唯一就可以收尾了，路径越短越不容易被无关的 DOM 变化打断。
        const id = evidenceToken(node.getAttribute && node.getAttribute('id'), 200);
        if (id && node.ownerDocument.querySelectorAll('#' + CSS.escape(id)).length === 1) {
          parts.unshift('#' + CSS.escape(id));
          break;
        }
        const parent = node.parentElement;
        if (!parent) {
          parts.unshift(tag);
          // Playwright's CSS engine pierces open shadow roots. Continue at the host and
          // join with a descendant combinator so identical controls in sibling roots do
          // not collapse to a global "button"/"input" selector.
          const root = node.getRootNode && node.getRootNode();
          const host = root && root.host;
          if (host && host.nodeType === 1) {
            node = host;
            continue;
          }
          break;
        }
        let index = 1;
        for (let i = 0; i < parent.children.length; i += 1) {
          const child = parent.children[i];
          if (child === node) break;
          if (String(child.tagName || '').toLowerCase() === tag) index += 1;
        }
        parts.unshift(tag + ':nth-of-type(' + index + ')');
        node = parent;
      }
      return parts.join(' ');
    } catch (_) { return ''; }
  };

  // 从顶层文档到本框架的 iframe 链。每一级都必须由**父文档自己的**
  // Playwright InjectedScript 生成，因为 selector scorer 的查询根就是该文档。
  // 跨源 frameElement 不可见时返回空链，Host 会从 binding context 的 CDP
  // frame/session 拓扑重建，绝不拿 CSS nth-of-type 冒充官方 selector。
  const framePathOf = () => {
    try {
      const chain = [];
      let win = window;
      while (win !== win.parent) {
        const frameElement = win.frameElement;
        if (!frameElement) return [];
        const parentControl = win.parent[CONTROL_NAME];
        const selector = parentControl && typeof parentControl.selectorFor === 'function'
          ? parentControl.selectorFor(frameElement)
          : '';
        if (!selector) return [];
        chain.unshift(selector);
        win = win.parent;
      }
      return chain;
    } catch (_) { return []; }
  };

  const targetOf = (element) => {
    try {
      const tag = String(element.tagName || '').toLowerCase();
      const text = targetText(element);
      let ordinal = 0;
      // 只在同 tag 的元素里数同文本的序号。全页 querySelectorAll('*') 在长列表上
      // 太贵，而按 tag 收窄之后足够区分「第几行的详情链接」。
      const peers = document.getElementsByTagName(tag);
      for (let index = 0; index < peers.length; index += 1) {
        const peer = peers[index];
        const peerText = targetText(peer);
        if (peerText !== text) continue;
        ordinal += 1;
        if (peer === element) break;
      }
      const testIdAttributes = ['data-testid', 'data-test', 'data-qa'];
      let testId = '';
      let testIdAttribute = '';
      for (let index = 0; index < testIdAttributes.length; index += 1) {
        const attribute = testIdAttributes[index];
        const candidate = evidenceToken(attr(element, attribute), 200);
        if (!candidate) continue;
        testId = candidate;
        testIdAttribute = attribute;
        break;
      }
      return {
        tag,
        text,
        ariaLabel: attr(element, 'aria-label'),
        href: attr(element, 'href'),
        ordinal,
        id: evidenceToken(attr(element, 'id'), 200),
        name: evidenceToken(attr(element, 'name'), 200),
        role: evidenceToken(attr(element, 'role').toLowerCase(), 80),
        inputType: evidenceToken(String(element.type || attr(element, 'type')).toLowerCase(), 40),
        contentEditable: editingHostOf(element) === element,
        testId,
        testIdAttribute,
        cssPath: cssPathOf(element),
        framePath: framePathOf(),
      };
    } catch (_) { return null; }
  };

  // Assigned by the scroll recorder below. Declaring it before send lets any
  // later user action synchronously commit a pending debounced scroll first.
  let flushPendingScrolls = () => 0;
  let flushPendingHover = () => 0;
  let flushPendingWheel = () => 0;
  let flushPendingPointerGesture = () => 0;
  let cancelPointerGesture = () => 0;
  // Likewise, keydown is registered before the scroll implementation below.
  // Its callback runs only after this IIFE has finished, so the implementation
  // can be assigned later while still sampling before the browser default action.
  let primeScrollBaselines = () => 0;
  const send = (type, element, extra, flushScroll = true, externalFiles = null) => {
    try {
      if (flushScroll) {
        if (type !== 'hover') flushPendingHover(element);
        if (type !== 'wheel') flushPendingWheel();
        flushPendingScrolls();
      }
      seq += 1;
      const elementTier = element && isTextEntry(element) ? tierOf(element) : 'plain';
      const requestedTier = extra && (
        extra.tier === 'plain' || extra.tier === 'identifier'
          || extra.tier === 'secret' || extra.tier === 'handoff'
      ) ? extra.tier : elementTier;
      if (element && (type === 'upload' || type === 'drop')) {
        // Only File wrappers require a short-lived renderer handle. Selectors
        // and target evidence for every ordinary action were already frozen in
        // this callback; stashing each keystroke/click would add an unnecessary
        // CDP round trip and retain live DOM nodes until the Host queue catches up.
        let files = null;
        const count = Number(extra && extra.fileCount);
        if (Number.isSafeInteger(count) && count >= 0) {
          try {
            const sourceFiles = type === 'drop' ? externalFiles : element.files;
            files = Array.from(sourceFiles || []).slice(0, count);
            if (files.length !== count) files = null;
          } catch (_) {
            files = null;
          }
        }
        stashTarget(seq, { files });
      }
      const capturedTarget = element ? targetOf(element) : null;
      const localSelector = element && officialSelectorFor
        ? officialSelectorFor(element)
        : '';
      const localDragSelector = (
        type === 'drag'
        && extra
        && typeof extra.recordedDragSelector === 'string'
      ) ? extra.recordedDragSelector : '';
      const selectorComplete = Boolean(
        localSelector && (type !== 'drag' || localDragSelector),
      );
      const payload = Object.assign({
        schemaVersion: ${RECORDER_EVENT_SCHEMA_VERSION},
        provenance: {
          schemaVersion: ${RECORDER_PROVENANCE_SCHEMA_VERSION},
          source: 'document-world',
          capturePhase: 'event-callback',
          browserTrusted: true,
          targetEvidence: capturedTarget ? 'synchronous' : 'none',
          nativeInput: 'unverified',
        },
        seq, causalId: 0, causalToken: 0, type, url: location.href,
        hint: element ? hintOf(element) : '',
        target: capturedTarget,
        dragTarget: null,
        dragSourcePosition: null,
        dragTargetPosition: null,
        recordedSelector: selectorComplete ? localSelector : '',
        recordedDragSelector: selectorComplete ? localDragSelector : '',
        selectorSource: selectorComplete ? 'playwright' : 'unavailable',
        tier: requestedTier, value: '', values: [],
        valueTruncated: false, lifecycleFlush: false,
        key: '', clickButton: '', clickCount: 0, position: null, modifiers: [],
        pointerType: '',
        gestureStart: null, gesturePoints: [],
        dialogAction: '', dialogType: '', dialogText: '',
        scrollX: 0, scrollY: 0,
        uploadMode: '', paths: [], fileCount: 0, multiple: false, accept: '',
        dropData: {},
      }, extra || {});
      // tier 只表达字段语义；定位与动作证据始终保持完整。
      payload.tier = requestedTier;
      payload.recordedSelector = selectorComplete ? localSelector : '';
      payload.recordedDragSelector = selectorComplete ? localDragSelector : '';
      payload.selectorSource = selectorComplete ? 'playwright' : 'unavailable';
      emit(payload);
      // The zero-delay callback runs after the complete trusted DOM event task
      // (including microtasks). A synchronous alert/confirm/prompt blocks it,
      // giving BrowserHost an exact task-scoped causal window. A page timer
      // runs only after this marker and is therefore recorded standalone.
      const causalSeq = seq;
      setTimeout(() => {
        try {
          emit({
            schemaVersion: ${RECORDER_EVENT_SCHEMA_VERSION},
            type: 'causal-end',
            seq: causalSeq,
          });
        } catch (_) { /* causal bookkeeping never affects the page */ }
      }, 0);
    } catch (_) { /* 录制绝不能影响用户自己的操作 */ }
  };

  // 用户点的是按钮，DOM 给的却常常是按钮里的 span/svg/文本节点。
  // 直接记 event.target 会让轨迹里出现「点了一个 span」，编译期既对不上快照行，
  // 也判断不出它其实是个提交按钮。往上找最近的可交互祖先。
  const INTERACTIVE = 'a,button,input,select,textarea,label,summary,'
    + '[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=checkbox],'
    + '[role=radio],[role=switch],[role=slider],[contenteditable=""],'
    + '[contenteditable=true],[onclick]';
  const actionable = (element) => {
    try {
      if (!element || typeof element.closest !== 'function') return element;
      const direct = element.closest(INTERACTIVE);
      if (direct) return direct;
      // Shadow DOM：closest 不会穿过 shadow 边界，宿主元素上找不到可交互祖先。
      // 沿 shadow host 逐层往上找——不这么做，Web Component 里的按钮点击会
      // 记成一个无名的宿主元素，编译期对不上任何快照行。
      let node = element;
      while (node) {
        const root = node.getRootNode && node.getRootNode();
        const host = root && root.host;
        if (!host) break;
        const found = host.closest && host.closest(INTERACTIVE);
        if (found) return found;
        node = host;
      }
      return element;
    } catch (_) { return element; }
  };
  // Composed events are retargeted to a shadow host at the document boundary.
  // The first composed-path entry is the real originating control, matching
  // Playwright recorder's event-target semantics for open Shadow DOM.
  const eventTargetOf = (event) => {
    try {
      const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
      return path && path.length ? path[0] : event.target;
    } catch (_) {
      return event.target;
    }
  };

  const nativeEditableControl = (element) => {
    try {
      if (!element) return null;
      let control = element;
      if (String(element.tagName || '').toLowerCase() === 'label') {
        control = element.control
          || (typeof element.querySelector === 'function'
            ? element.querySelector('input,select,textarea,[contenteditable=""],[contenteditable=true]')
            : null);
      }
      if (!control) return null;
      const tag = String(control.tagName || '').toLowerCase();
      if (tag === 'select' || tag === 'textarea' || control.isContentEditable) return control;
      if (tag !== 'input') return null;
      const type = String(control.type || attr(control, 'type') || 'text').toLowerCase();
      return ['button', 'submit', 'reset', 'image', 'hidden'].indexOf(type) >= 0
        ? null
        : control;
    } catch (_) { return null; }
  };

  let suppressClickUntil = 0;
  const positionForEvent = (event) => {
    try {
      const target = eventTargetOf(event);
      if (!target || String(target.nodeName || '').toUpperCase() !== 'CANVAS') return null;
      const x = Number(event.offsetX);
      const y = Number(event.offsetY);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
      return { x, y };
    } catch (_) {
      return null;
    }
  };
  const pointerTargetOf = (event) => {
    const target = eventTargetOf(event);
    // A canvas point is relative to the canvas padding box. Selecting an
    // actionable ancestor here would silently move the coordinate system.
    return target && String(target.nodeName || '').toUpperCase() === 'CANVAS'
      ? target
      : actionable(target);
  };
  const pointerEvidence = (event, forcedButton) => {
    const button = forcedButton || (
      Number(event.button) === 1 ? 'middle'
        : (Number(event.button) === 2 ? 'right' : 'left')
    );
    const modifiers = [];
    if (event.altKey) modifiers.push('Alt');
    if (event.ctrlKey) modifiers.push('Control');
    if (event.metaKey) modifiers.push('Meta');
    if (event.shiftKey) modifiers.push('Shift');
    return {
      clickButton: button,
      clickCount: Math.max(1, Math.trunc(Number(event.detail) || 1)),
      position: positionForEvent(event),
      modifiers,
    };
  };
  const dragPositionForEvent = (element, event) => {
    try {
      if (!element || typeof element.getBoundingClientRect !== 'function') return null;
      const clientX = Number(event && event.clientX);
      const clientY = Number(event && event.clientY);
      if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return null;
      const rect = element.getBoundingClientRect();
      if (!rect) return null;
      let borderLeft = 0;
      let borderTop = 0;
      try {
        const view = element.ownerDocument && element.ownerDocument.defaultView;
        const style = view && typeof view.getComputedStyle === 'function'
          ? view.getComputedStyle(element)
          : (
              typeof globalThis.getComputedStyle === 'function'
                ? globalThis.getComputedStyle(element)
                : null
            );
        const parsedLeft = Number.parseFloat(style && style.borderLeftWidth);
        const parsedTop = Number.parseFloat(style && style.borderTopWidth);
        if (Number.isFinite(parsedLeft)) borderLeft = parsedLeft;
        if (Number.isFinite(parsedTop)) borderTop = parsedTop;
      } catch (_) { /* zero-border fallback keeps the event usable */ }
      const x = clientX - Number(rect.left) - borderLeft;
      const y = clientY - Number(rect.top) - borderTop;
      if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0) return null;
      return { x, y };
    } catch (_) {
      return null;
    }
  };

  // Continuous pointer gestures are first-class only in v11.  They cover the
  // surfaces for which click/dragTo/input are insufficient: canvas signatures,
  // maps, drawing boards and custom (non-native) sliders.  Coordinates stay
  // relative to the exact selector border box captured at pointerdown; replay
  // translates them through the element's current Playwright bounding box.
  //
  // Sampling deliberately has no arbitrary point-count ceiling.  Instead it
  // removes only geometric/time duplicates, preserving corners, pauses and the
  // final endpoint no matter how long the gesture lasts.
  let activePointerGesture = null;
  let pendingPointerGesture = null;
  const pointerGestureButton = (event) => (
    Number(event.button) === 1 ? 'middle'
      : (Number(event.button) === 2 ? 'right' : 'left')
  );
  const pointerGestureDeviceOf = (event) => {
    try {
      const pointerType = String(event && event.pointerType || '').toLowerCase();
      return pointerType === 'pen' || pointerType === 'touch'
        ? pointerType
        : 'mouse';
    } catch (_) {
      return 'mouse';
    }
  };
  const pointerGestureTelemetryFields = [
    ['pressure', 0, 1],
    ['tangentialPressure', -1, 1],
    ['tiltX', -90, 90],
    ['tiltY', -90, 90],
    ['twist', 0, 359],
    ['width', 0, Number.POSITIVE_INFINITY],
    ['height', 0, Number.POSITIVE_INFINITY],
  ];
  const pointerGestureTelemetry = (event) => {
    const telemetry = {};
    for (let index = 0; index < pointerGestureTelemetryFields.length; index += 1) {
      const [name, minimum, maximum] = pointerGestureTelemetryFields[index];
      let raw = Number.NaN;
      try {
        raw = Number(event && event[name]);
      } catch (_) {
        continue;
      }
      if (
        Number.isFinite(raw)
        && raw >= minimum
        && raw <= maximum
      ) telemetry[name] = raw;
    }
    return telemetry;
  };
  const pointerGestureTargetOf = (event) => {
    const rawTarget = eventTargetOf(event);
    if (!rawTarget) return null;
    const element = rawTarget.nodeType === 1
      ? rawTarget
      : (rawTarget.parentElement || null);
    if (!element) return null;
    // Preserve the canvas coordinate space exactly.  For ordinary custom
    // widgets, prefer the semantic/actionable root over a decorative child.
    return String(element.nodeName || '').toUpperCase() === 'CANVAS'
      ? element
      : actionable(element);
  };
  const pointerGestureTimestamp = (event) => {
    const timestamp = Number(event && event.timeStamp);
    if (Number.isFinite(timestamp) && timestamp >= 0) return timestamp;
    try {
      const now = Number(globalThis.performance && globalThis.performance.now());
      if (Number.isFinite(now) && now >= 0) return now;
    } catch (_) { /* Date fallback below */ }
    return Date.now();
  };
  const pointerGesturePoint = (gesture, event) => {
    const clientX = Number(event && event.clientX);
    const clientY = Number(event && event.clientY);
    if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return null;
    const timestamp = pointerGestureTimestamp(event);
    const elapsedMs = Math.max(
      gesture.lastElapsedMs,
      Number.isFinite(timestamp - gesture.startedAt)
        ? Math.max(0, timestamp - gesture.startedAt)
        : gesture.lastElapsedMs,
    );
    return {
      x: clientX - gesture.rectLeft,
      y: clientY - gesture.rectTop,
      elapsedMs,
      ...pointerGestureTelemetry(event),
    };
  };
  const pointerGestureTelemetryEqual = (a, b) => (
    a.pressure === b.pressure
    && a.tangentialPressure === b.tangentialPressure
    && a.tiltX === b.tiltX
    && a.tiltY === b.tiltY
    && a.twist === b.twist
    && a.width === b.width
    && a.height === b.height
  );
  const pointDistance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const middlePointDistance = (a, b, c) => {
    const dx = c.x - a.x;
    const dy = c.y - a.y;
    const lengthSquared = dx * dx + dy * dy;
    if (lengthSquared <= 0.000001) return pointDistance(a, b);
    const projection = Math.max(0, Math.min(
      1,
      ((b.x - a.x) * dx + (b.y - a.y) * dy) / lengthSquared,
    ));
    return Math.hypot(
      b.x - (a.x + projection * dx),
      b.y - (a.y + projection * dy),
    );
  };
  const appendPointerGesturePoint = (gesture, event, forceEndpoint = false) => {
    const point = pointerGesturePoint(gesture, event);
    if (!point) return false;
    const rawDistance = pointDistance(gesture.lastRawPoint, point);
    gesture.pathLength += rawDistance;
    gesture.lastRawPoint = point;
    gesture.lastElapsedMs = point.elapsedMs;

    const points = gesture.points;
    const last = points.length ? points[points.length - 1] : null;
    if (
      last
      && pointDistance(last, point) < 0.25
      && pointerGestureTelemetryEqual(last, point)
    ) {
      // Same place at a later time is a pause, not another geometric sample.
      // Retain its latest timestamp so replay preserves the dwell duration.
      points[points.length - 1] = point;
      return rawDistance > 0;
    }
    if (
      !forceEndpoint
      && last
      && point.elapsedMs - last.elapsedMs < 8
      && pointDistance(last, point) < 0.75
    ) {
      points[points.length - 1] = point;
      return true;
    }
    if (points.length >= 2) {
      const beforeLast = points[points.length - 2];
      if (
        last.elapsedMs - beforeLast.elapsedMs <= 16
        && point.elapsedMs - last.elapsedMs <= 16
        && middlePointDistance(beforeLast, last, point) <= 0.35
        && pointerGestureTelemetryEqual(beforeLast, last)
        && pointerGestureTelemetryEqual(last, point)
      ) {
        points[points.length - 1] = point;
        return true;
      }
    }
    points.push(point);
    return true;
  };
  const pointerGestureQualifies = (gesture) => (
    gesture.completed === true
    && gesture.pathLength >= 3
    && gesture.points.length > 0
  );
  cancelPointerGesture = () => {
    let canceled = 0;
    if (activePointerGesture) {
      activePointerGesture = null;
      canceled = 1;
    }
    if (pendingPointerGesture) {
      clearTimeout(pendingPointerGesture.timer);
      pendingPointerGesture = null;
      canceled = 1;
    }
    return canceled;
  };
  flushPendingPointerGesture = (lifecycleFlush = false) => {
    if (!pendingPointerGesture) return 0;
    const pending = pendingPointerGesture;
    pendingPointerGesture = null;
    clearTimeout(pending.timer);
    if (!pointerGestureQualifies(pending)) return 0;
    send('pointerGesture', pending.element, {
      clickButton: pending.button,
      clickCount: 0,
      position: null,
      modifiers: pending.modifiers,
      pointerType: pending.pointerType,
      gestureStart: pending.start,
      gesturePoints: pending.points,
      lifecycleFlush,
    }, false);
    // Usually Chromium dispatches click before the zero-delay commit timer, but
    // do not rely on that scheduling detail. A late synthesized click still
    // belongs to this completed physical gesture and must not become a duplicate.
    suppressClickUntil = Date.now() + 1_000;
    return 1;
  };
  const consumeCompletedPointerGesture = () => {
    if (!pendingPointerGesture) return false;
    const committed = flushPendingPointerGesture();
    if (committed) suppressClickUntil = Date.now() + 1_000;
    return committed > 0;
  };
  document.addEventListener('pointerdown', (event) => {
    if (
      !RECORDING_V11
      || !active
      || !event.isTrusted
      || event.isPrimary === false
      || ![0, 1, 2].includes(Number(event.button))
    ) return;
    // A completed prior gesture owns its earlier ledger position. An incomplete
    // stream (lost pointerup/navigation) is not guessed into a replay action.
    flushPendingPointerGesture();
    activePointerGesture = null;
    const element = pointerGestureTargetOf(event);
    if (!element || typeof element.getBoundingClientRect !== 'function') return;
    let rect;
    try {
      rect = element.getBoundingClientRect();
    } catch (_) {
      return;
    }
    const rectLeft = Number(rect && rect.left);
    const rectTop = Number(rect && rect.top);
    const clientX = Number(event.clientX);
    const clientY = Number(event.clientY);
    if (
      !Number.isFinite(rectLeft)
      || !Number.isFinite(rectTop)
      || !Number.isFinite(clientX)
      || !Number.isFinite(clientY)
    ) return;
    flushDirtyInputs();
    const startedAt = pointerGestureTimestamp(event);
    const start = {
      x: clientX - rectLeft,
      y: clientY - rectTop,
      ...pointerGestureTelemetry(event),
    };
    const modifiers = pointerEvidence(event).modifiers;
    activePointerGesture = {
      pointerId: Number(event.pointerId),
      element,
      rectLeft,
      rectTop,
      startedAt,
      lastElapsedMs: 0,
      start,
      lastRawPoint: { ...start, elapsedMs: 0 },
      pathLength: 0,
      points: [],
      button: pointerGestureButton(event),
      modifiers,
      pointerType: pointerGestureDeviceOf(event),
      completed: false,
      timer: 0,
    };
  }, true);
  document.addEventListener('pointermove', (event) => {
    const gesture = activePointerGesture;
    if (
      !gesture
      || !active
      || !event.isTrusted
      || Number(event.pointerId) !== gesture.pointerId
    ) return;
    let samples = [event];
    try {
      const coalesced = typeof event.getCoalescedEvents === 'function'
        ? event.getCoalescedEvents()
        : null;
      if (Array.isArray(coalesced) && coalesced.length) samples = coalesced;
    } catch (_) { /* the dispatched pointer sample remains authoritative */ }
    for (let index = 0; index < samples.length; index += 1) {
      appendPointerGesturePoint(gesture, samples[index]);
    }
  }, true);
  const completePointerGesture = (event) => {
    const gesture = activePointerGesture;
    if (
      !gesture
      || !active
      || !event.isTrusted
      || Number(event.pointerId) !== gesture.pointerId
    ) return;
    appendPointerGesturePoint(gesture, event, true);
    activePointerGesture = null;
    gesture.completed = true;
    gesture.timer = setTimeout(() => {
      if (active && pendingPointerGesture === gesture) {
        flushPendingPointerGesture();
      }
    }, 0);
    pendingPointerGesture = gesture;
  };
  document.addEventListener('pointerup', completePointerGesture, true);
  document.addEventListener('pointercancel', (event) => {
    if (
      activePointerGesture
      && event.isTrusted
      && Number(event.pointerId) === activePointerGesture.pointerId
    ) cancelPointerGesture();
  }, true);

  // Playwright's useful hover semantic is "the pointer rested on an element
  // long enough to expose state", not every mousemove sample.  A short dwell
  // commits directly; moving to a different actionable element commits the
  // previous candidate only after a smaller transition threshold.  Thus a
  // hover-menu parent survives the move into its submenu without adding a
  // redundant hover before every ordinary click.
  let pendingHover = null;
  flushPendingHover = (nextElement = null, lifecycleFlush = false) => {
    if (!pendingHover) return 0;
    const pending = pendingHover;
    pendingHover = null;
    clearTimeout(pending.timer);
    const elapsed = Date.now() - pending.startedAt;
    if (
      !lifecycleFlush
      && (
        nextElement === pending.element
        || elapsed < 120
      )
    ) return 0;
    send('hover', pending.element, { position: pending.position }, false);
    return 1;
  };
  document.addEventListener('pointerover', (event) => {
    if (!RECORDING_V11 || !active || !event.isTrusted) return;
    const element = pointerTargetOf(event);
    if (!element || pendingHover?.element === element) return;
    flushPendingHover(element);
    const pending = {
      element,
      position: positionForEvent(event),
      startedAt: Date.now(),
      timer: 0,
    };
    pending.timer = setTimeout(() => {
      if (active && pendingHover === pending) {
        flushPendingHover(null, true);
      }
    }, 250);
    pendingHover = pending;
  }, true);
  document.addEventListener('pointerout', (event) => {
    if (!RECORDING_V11 || !active || !event.isTrusted || !pendingHover) return;
    if (pointerTargetOf(event) !== pendingHover.element) return;
    const next = actionable(eventTargetOf({ target: event.relatedTarget }));
    if (next === pendingHover.element) return;
    flushPendingHover(next);
  }, true);

  document.addEventListener('click', (event) => {
    if (!active || !event.isTrusted) return;
    // Enter/Space keyboard activation and accessibility activation produce a trusted click
    // with zero detail. Playwright records the originating keydown and ignores this derived
    // click; keeping both would replay the same activation twice.
    if (event.detail === 0) return;
    if (consumeCompletedPointerGesture()) return;
    const element = pointerTargetOf(event);
    // Text controls still need their click. Real applications commonly reveal
    // autocomplete, search suggestions, virtual keyboards, or validation UI on
    // focus/click before any input event exists. The compiler removes this click
    // only when an immediately following input on the same page and selector
    // supersedes it. State controls and native pickers remain represented by
    // their stronger input/upload action, matching Playwright's recorder.
    const editable = nativeEditableControl(element);
    if (editable) {
      const tag = String(editable.tagName || '').toLowerCase();
      const inputType = String(editable.type || attr(editable, 'type') || '').toLowerCase();
      const nativePickerTypes = [
        'color', 'date', 'datetime-local', 'file', 'month', 'range', 'time', 'week',
      ];
      if (
        tag === 'select'
        || inputType === 'checkbox'
        || inputType === 'radio'
        || nativePickerTypes.indexOf(inputType) >= 0
      ) return;
    }
    if (Date.now() <= suppressClickUntil) return;
    send('click', element, pointerEvidence(event, 'left'));
  }, true);

  // Chromium fires auxclick for non-primary buttons. Persist middle-click because
  // it commonly opens a new tab; right-click is represented by contextmenu below
  // to avoid emitting the same physical action twice.
  document.addEventListener('auxclick', (event) => {
    if (!active || !event.isTrusted || Number(event.button) !== 1) return;
    if (consumeCompletedPointerGesture()) return;
    if (Date.now() <= suppressClickUntil) return;
    send('click', pointerTargetOf(event), pointerEvidence(event, 'middle'));
  }, true);

  document.addEventListener('contextmenu', (event) => {
    if (!active || !event.isTrusted) return;
    if (consumeCompletedPointerGesture()) return;
    if (Date.now() <= suppressClickUntil) return;
    send('click', pointerTargetOf(event), pointerEvidence(event, 'right'));
  }, true);

  let dragSource = null;
  document.addEventListener('dragstart', (event) => {
    if (!active || !event.isTrusted) return;
    cancelPointerGesture();
    const element = actionable(eventTargetOf(event));
    dragSource = {
      element,
      position: dragPositionForEvent(element, event),
    };
    suppressClickUntil = Date.now() + 1_000;
  }, true);
  document.addEventListener('drop', (event) => {
    if (!active || !event.isTrusted) return;
    const destination = actionable(eventTargetOf(event));
    if (dragSource) {
      send('drag', dragSource.element, {
        dragTarget: targetOf(destination),
        dragSourcePosition: dragSource.position,
        dragTargetPosition: dragPositionForEvent(destination, event),
        recordedDragSelector: officialSelectorFor ? officialSelectorFor(destination) : '',
        ...pointerEvidence(event, 'left'),
        clickCount: 1,
        position: null,
      });
      suppressClickUntil = Date.now() + 1_000;
      dragSource = null;
      return;
    }
    // External OS/browser drags do not dispatch dragstart in this document.
    // v11 records every synchronously readable string flavor plus the exact
    // File wrappers. No arbitrary item, byte, type-name or value-size cap is
    // applied; Chromium and JSON serialization are the natural boundaries.
    if (!RECORDING_V11) return;
    let files = [];
    const data = Object.create(null);
    try {
      const transfer = event.dataTransfer;
      if (transfer) {
        try {
          files = Array.from(transfer.files || []);
        } catch (_) {
          files = [];
        }
        const formats = [];
        try {
          for (const format of Array.from(transfer.types || [])) {
            if (typeof format === 'string' && !formats.includes(format)) formats.push(format);
          }
        } catch (_) { /* items below may still expose the formats */ }
        try {
          for (const item of Array.from(transfer.items || [])) {
            if (
              item
              && item.kind === 'string'
              && typeof item.type === 'string'
              && !formats.includes(item.type)
            ) formats.push(item.type);
          }
        } catch (_) { /* types above may already be complete */ }
        if (typeof transfer.getData === 'function') {
          for (const format of formats) {
            if (format.toLowerCase() === 'files') continue;
            try {
              const value = transfer.getData(format);
              if (typeof value === 'string') data[format] = value;
            } catch (_) { /* inaccessible formats are not synchronously readable */ }
          }
        }
      }
    } catch (_) { /* a trusted empty external drop is still representable */ }
    send('drop', destination, {
      fileCount: files.length,
      dropData: data,
    }, true, files);
    suppressClickUntil = Date.now() + 1_000;
  }, true);
  document.addEventListener('dragend', () => {
    dragSource = null;
    suppressClickUntil = Date.now() + 1_000;
  }, true);

  // 只在 trusted input/change 时更新最终值；blur 只负责提交已观察到的输入突发。
  // 这样中文输入、粘贴、替换、删除与浏览器自动补全都走同一条通用路径，同时页面
  // 单纯赋值再 focus()/blur() 不会凭空制造录制步骤。
  const dirtyInputs = new WeakMap();
  const dirtyElements = new Set();
  const lastCommitted = new WeakMap();
  const inputCausalTokens = new WeakMap();
  let nextInputCausalToken = 0;
  let nextInputCausalLease = 0;

  const beginInputCausal = (element) => {
    let token = inputCausalTokens.get(element);
    if (!Number.isSafeInteger(token) || token <= 0) {
      nextInputCausalToken += 1;
      if (!Number.isSafeInteger(nextInputCausalToken) || nextInputCausalToken <= 0) {
        nextInputCausalToken = 1;
      }
      token = nextInputCausalToken;
      inputCausalTokens.set(element, token);
    }
    nextInputCausalLease += 1;
    if (!Number.isSafeInteger(nextInputCausalLease) || nextInputCausalLease <= 0) {
      nextInputCausalLease = 1;
    }
    const lease = nextInputCausalLease;
    try {
      emit({
        schemaVersion: ${RECORDER_EVENT_SCHEMA_VERSION},
        type: 'causal-begin',
        seq: lease,
        token,
      });
    } catch (_) { /* causal bookkeeping never affects the page */ }
    setTimeout(() => {
      try {
        emit({
          schemaVersion: ${RECORDER_EVENT_SCHEMA_VERSION},
          type: 'causal-end',
          seq: lease,
          token,
        });
      } catch (_) { /* causal bookkeeping never affects the page */ }
    }, 0);
    return token;
  };

  document.addEventListener('beforeinput', (event) => {
    if (!active) return;
    const element = inputTargetOf(eventTargetOf(event));
    if (!event.isTrusted || !element || !isTextEntry(element)) return;
    beginInputCausal(element);
  }, true);

  const inputFingerprint = (dirty) => (
    dirty.tier + '\\u0000' + dirty.value
    + '\\u0000' + JSON.stringify(dirty.values || [])
    + '\\u0000' + (dirty.valueTruncated ? '1' : '0')
  );

  const commitElement = (element, lifecycleFlush = false) => {
    if (!element || !isTextEntry(element)) return false;
    const dirty = dirtyInputs.get(element);
    if (!dirty) return false;
    // Always release iterable/weak dirty state, including the dedup path.
    dirtyInputs.delete(element);
    dirtyElements.delete(element);
    inputCausalTokens.delete(element);
    const fingerprint = inputFingerprint(dirty);
    if (lastCommitted.get(element) === fingerprint) return false;
    lastCommitted.set(element, fingerprint);
    send('input', element, {
      tier: dirty.tier,
      value: dirty.value,
      values: dirty.values || [],
      valueTruncated: dirty.valueTruncated,
      lifecycleFlush,
      causalToken: dirty.causalToken,
    });
    return true;
  };

  const trackDirty = (element, dirty) => {
    dirtyInputs.set(element, dirty);
    dirtyElements.add(element);
  };

  const captureTrustedInput = (event) => {
    if (!active || !event.isTrusted) return;
    const element = inputTargetOf(eventTargetOf(event));
    if (!element || !isTextEntry(element)) return;
    cancelPointerGesture();
    const causalToken = beginInputCausal(element);
    // A debounced scroll that happened before this edit must keep its earlier
    // ledger position even though the input value itself is committed later.
    flushPendingScrolls();
    const tier = tierOf(element);
    const inputType = String(element.type || '').toLowerCase();
    let value = '';
    let values = [];
    let valueTruncated = false;
    if (inputType === 'checkbox' || inputType === 'radio') {
      value = element.checked ? 'checked' : 'unchecked';
    } else if (element.isContentEditable) {
      const evidence = editableValue(element);
      value = evidence.text;
      valueTruncated = !evidence.complete;
    } else if (
      String(element.tagName || '').toUpperCase() === 'SELECT'
      && element.multiple === true
    ) {
      const selected = Array.from(element.selectedOptions || []);
      values = selected.map((option) => {
        const raw = String(option && option.value !== undefined ? option.value : '');
        return raw;
      });
      // Keep the legacy scalar surface deterministic while values remains
      // authoritative for select-multiple in schema v6.
      value = values.length ? values[0] : '';
    } else {
      const rawValue = String(element.value || '');
      value = rawValue;
    }
    const dirty = { tier, value, values, valueTruncated, causalToken };
    trackDirty(element, dirty);
    // Persist every trusted input synchronously in the originating DOM task.
    // Waiting for change/blur loses the action when an input handler navigates,
    // closes the page, or replaces the document. Consecutive values are cheap
    // recorder IR updates and are coalesced later by (page, selector), while a
    // causal navigation/dialog creates a hard boundary that preserves the exact
    // triggering value.
    const fingerprint = inputFingerprint(dirty);
    if (lastCommitted.get(element) !== fingerprint) {
      lastCommitted.set(element, fingerprint);
      send('input', element, {
        tier,
        value,
        values,
        valueTruncated,
        lifecycleFlush: false,
        causalToken,
      });
    }
  };
  // Native file selection normally emits input then change. Some Chromium paths (notably
  // setInputFiles([]) clear) emit only input. Capture both but collapse the same File wrappers
  // within one task so one chooser interaction remains one durable upload step.
  const recentFileSelections = new WeakMap();
  const captureFileSelection = (event, emptyOnly = false) => {
    if (!active || !event.isTrusted) return false;
    try {
      const element = eventTargetOf(event);
      if (
        !element
        || String(element.tagName || '').toLowerCase() !== 'input'
        || String(element.type || attr(element, 'type') || '').toLowerCase() !== 'file'
      ) return false;
      cancelPointerGesture();
      const rawCount = Number(element.files && element.files.length);
      const fileCount = Number.isSafeInteger(rawCount)
        ? Math.max(0, rawCount)
        : 0;
      if (emptyOnly && fileCount !== 0) return true;
      const comparableFiles = Array.from(element.files || []).slice(0, fileCount);
      const previous = recentFileSelections.get(element);
      const duplicate = previous
        && previous.fileCount === fileCount
        && (
          comparableFiles === null
          || (
            previous.files
            && previous.files.length === comparableFiles.length
            && comparableFiles.every((file, index) => previous.files[index] === file)
          )
        );
      if (duplicate) return true;
      const marker = { fileCount, files: comparableFiles };
      recentFileSelections.set(element, marker);
      setTimeout(() => {
        if (recentFileSelections.get(element) === marker) recentFileSelections.delete(element);
      }, 0);
      send('upload', element, {
        uploadMode: fileCount === 0 ? 'clear' : 'handoff',
        paths: [],
        fileCount,
        multiple: element.multiple === true,
        accept: attr(element, 'accept'),
      });
      return true;
    } catch (_) {
      return false;
    }
  };
  document.addEventListener('input', (event) => {
    if (captureFileSelection(event)) return;
    captureTrustedInput(event);
  }, true);

  // change/blur 只负责清理 causal/dirty 状态，或兼容没有先发 input 的浏览器路径。
  // 普通逐字符更新已在 captureTrustedInput 中同步持久化，并由编译器按页面和 selector
  // 合并成最终表单状态。
  const commitInput = (event) => {
    if (!active || !event.isTrusted) return;
    const element = inputTargetOf(eventTargetOf(event));
    if (!element || !isTextEntry(element)) return;
    commitElement(element);
  };
  document.addEventListener('change', (event) => {
    if (!active) return;
    if (captureFileSelection(event)) return;
    captureTrustedInput(event);
    commitInput(event);
  }, true);
  // Chromium dispatches cancel when a chooser closes without a selection. On a fresh empty
  // input this is an explicit no-file result and maps cleanly to Playwright setInputFiles([]);
  // when an earlier selection remains, cancel has no state transition and must not replay an
  // upload of stale files.
  document.addEventListener('cancel', (event) => {
    if (!active || !event.isTrusted) return;
    try {
      const element = eventTargetOf(event);
      if (
        !element
        || String(element.tagName || '').toLowerCase() !== 'input'
        || String(element.type || attr(element, 'type') || '').toLowerCase() !== 'file'
      ) return;
      captureFileSelection(event, true);
    } catch (_) { /* chooser cancellation must never affect the page */ }
  }, true);
  document.addEventListener('blur', commitInput, true);

  const flushDirtyInputs = () => {
    let committed = 0;
    const pending = Array.from(dirtyElements);
    for (let index = 0; index < pending.length; index += 1) {
      if (commitElement(pending[index], true)) committed += 1;
    }
    return committed;
  };
  const control = Object.freeze({
    schemaVersion: ${RECORDER_EVENT_SCHEMA_VERSION},
    recordingSchemaVersion: RECORDING_SCHEMA_VERSION,
    selectorFor: (element) => officialSelectorFor ? officialSelectorFor(element) : '',
    activate: () => { active = true; },
    flush: () => {
      const gestured = flushPendingPointerGesture(true);
      activePointerGesture = null;
      const hovered = flushPendingHover(null, true);
      const wheeled = flushPendingWheel(true);
      const scrolled = flushPendingScrolls(true);
      return gestured + hovered + wheeled + scrolled + flushDirtyInputs();
    },
    deactivate: () => {
      const committed = flushPendingPointerGesture(true)
        + flushPendingHover(null, true)
        + flushPendingWheel(true)
        + flushPendingScrolls(true)
        + flushDirtyInputs();
      activePointerGesture = null;
      active = false;
      return committed;
    },
    isActive: () => active,
  });
  try {
    Object.defineProperty(globalThis, CONTROL_NAME, {
      value: control, configurable: true, enumerable: false, writable: false,
    });
  } catch (_) {
    try { globalThis[CONTROL_NAME] = control; } catch (_) { /* random name avoids collision */ }
  }

  // Do not persist submit DOM events. requestSubmit() creates a browser-trusted event and
  // is observationally indistinguishable from the default form algorithm while it runs in
  // the same real click/key task. The actual human trigger is already recorded with stronger
  // evidence: a submitter click or Enter key. Keeping a second submit step adds no replay
  // capability (the compiler treats it as takeover) and only creates a transferable surface.
  document.addEventListener('submit', (event) => {
    if (!active || !event.isTrusted) return;
    // Intentionally empty. The listener is retained as an explicit security contract and
    // to make future "helpful" reintroduction of submit persistence stand out in review.
  }, true);

  // 只记功能键。普通字符已由 input 事件的最终值覆盖，逐键记录纯属噪声。
  const FUNCTION_KEYS = new Set(['Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'Insert',
    'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown', 'Home', 'End',
    'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12']);
  const SCROLL_KEYS = new Set([
    ' ', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
    'PageUp', 'PageDown', 'Home', 'End',
  ]);
  const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Meta', 'Alt', 'AltGraph', 'Process']);
  document.addEventListener('keydown', (event) => {
    if (!active || !event.isTrusted) return;
    const element = inputTargetOf(eventTargetOf(event));
    // 中文输入法选词时按的 Enter/方向键属于「打字」而不是工作流动作。
    // isComposing 与 keyCode 229 是浏览器给的组合态标记，页面改不了。
    if (event.isComposing || event.keyCode === 229) return;
    const key = typeof event.key === 'string' ? event.key : '';
    // Keyboard scrolling happens after keydown dispatch. Sample every possible
    // scroll surface before any recorder action or browser default action.
    if (SCROLL_KEYS.has(key)) primeScrollBaselines(eventTargetOf(event));
    // Modifier keydown already carries its own modifier flag. Without this guard it becomes
    // Control+Control, Meta+Meta, Alt+Alt, or Shift+Shift instead of a real workflow action.
    if (!key || MODIFIER_KEYS.has(key)) return;
    // Paste changes the editable through a following trusted input event. Recording the
    // shortcut as press as well would paste twice during replay; the final input is canonical.
    if ((event.metaKey || event.ctrlKey) && key.toLowerCase() === 'v') return;
    // 在输入框里直接按回车提交时，change 与 blur **都不会触发**——焦点没离开，
    // 值也没被浏览器判定为"已更改提交"。不在这里补一次，整段输入就丢了，
    // 轨迹里只剩一个孤零零的 Enter，编译期完全看不出用户搜了什么。
    if (key === 'Enter' && isTextEntry(element)) {
      const tag = String(element.tagName || '').toUpperCase();
      // A textarea/contenteditable Enter is a newline and will be included in the following
      // trusted input state. Single-line Enter is an activation/submission and needs both the
      // already-entered value and the key action.
      if (tag === 'TEXTAREA' || element.isContentEditable) return;
      commitInput({ target: element, isTrusted: true });
    }
    // keydown precedes Tab's default focus move, while blur/change arrive after it.
    // Commit the final field state now so replay fills before pressing Tab instead
    // of tabbing through an empty control and only filling it afterwards.
    if (key === 'Tab' && isTextEntry(element)) {
      commitInput({ target: element, isTrusted: true });
    }
    const activationSpace = key === ' ' && !isTextEntry(element);
    if (
      !FUNCTION_KEYS.has(key)
      && !activationSpace
      && !event.metaKey
      && !event.ctrlKey
      && !event.altKey
    ) return;
    const normalizedKey = activationSpace ? 'Space' : key;
    const combo = (event.ctrlKey ? 'Ctrl+' : '') + (event.metaKey ? 'Meta+' : '')
      + (event.altKey ? 'Alt+' : '') + (event.shiftKey ? 'Shift+' : '') + normalizedKey;
    send('key', element, { key: combo });
  }, true);

  // 滚动节流：滚动是连续事件，逐个记会淹没轨迹，只在停下来之后记一条净位移。
  //
  // 两个必须避开的坑：
  // 1. **基线不能在第一次 scroll 事件里采**。scroll 是滚动发生之后才派发的，
  //    那时位置已经是滚完的值——单次滚动手势算出来的位移恒为 0（实测如此）。
  //    document 在监听建立时取基线，动态内层容器在浏览器默认动作前的 trusted
  //    wheel/pointer/key/touch 事件里同步取，并在每次记录后更新为当前位置。
  // 2. **不能一律读 window 的坐标**。内层滚动容器、虚拟列表滚的是元素自己的
  //    scrollTop/scrollLeft，读 window 会得到 0，等于这类滚动全部丢失。
  const scrollPosition = (target) => {
    if (!target || target === document || target === document.documentElement
      || target === document.body || target === globalThis || target === window) {
      return { x: globalThis.scrollX, y: globalThis.scrollY, element: null };
    }
    return {
      x: Number(target.scrollLeft) || 0,
      y: Number(target.scrollTop) || 0,
      element: target,
    };
  };

  const scrollBases = new WeakMap();
  let documentScrollBase = { x: globalThis.scrollX, y: globalThis.scrollY };
  // A normal Map is intentional: lifecycle flush must enumerate every pending
  // scroll. Entries are deleted whenever the debounced action is emitted.
  const pendingScrolls = new Map();

  const baseOf = (element) => (element ? scrollBases.get(element) : documentScrollBase);
  const setBase = (element, value) => {
    if (element) scrollBases.set(element, value);
    else documentScrollBase = value;
  };

  const parentScrollNode = (node) => {
    try {
      if (node && node.parentElement) return node.parentElement;
      const root = node && node.getRootNode && node.getRootNode();
      return root && root.host && root.host.nodeType === 1 ? root.host : null;
    } catch (_) {
      return null;
    }
  };

  const possibleScrollSurfaces = (target) => {
    const surfaces = [];
    const seen = new Set();
    const add = (candidate) => {
      const position = scrollPosition(candidate);
      const key = position.element || document;
      if (seen.has(key)) return;
      seen.add(key);
      surfaces.push(position.element);
    };
    try {
      let node = target && target.nodeType === 1
        ? target
        : (target && target.parentElement ? target.parentElement : null);
      while (node && !seen.has(node)) {
        if (
          Number(node.scrollHeight || 0) > Number(node.clientHeight || 0)
          || Number(node.scrollWidth || 0) > Number(node.clientWidth || 0)
        ) add(node);
        node = parentScrollNode(node);
      }
    } catch (_) { /* document fallback below */ }
    // Wheel/touch/key scroll chaining can leave every inner container at its
    // boundary and move the page instead. Always sample the document too.
    add(document);
    return surfaces;
  };

  primeScrollBaselines = (target) => {
    const surfaces = possibleScrollSurfaces(target);
    let hasNewSurface = false;
    for (let index = 0; index < surfaces.length; index += 1) {
      const element = surfaces[index];
      if (!pendingScrolls.has(element || document)) {
        hasNewSurface = true;
        break;
      }
    }
    if (!hasNewSurface) return 0;
    // Final text state happened before this new scroll gesture. This may flush
    // an older pending scroll, so re-check pending state before sampling below.
    flushDirtyInputs();
    let primed = 0;
    for (let index = 0; index < surfaces.length; index += 1) {
      const element = surfaces[index];
      const key = element || document;
      if (pendingScrolls.has(key)) continue;
      const position = scrollPosition(element || document);
      setBase(element, { x: position.x, y: position.y });
      primed += 1;
    }
    return primed;
  };

  const primeFromTrustedEvent = (event) => {
    if (!active || !event.isTrusted) return;
    primeScrollBaselines(eventTargetOf(event));
  };

  // These events are dispatched before their browser default scrolling. Capture
  // all scrollable ancestors plus the document so chaining at an inner boundary
  // still has an exact baseline for whichever surface actually moves.
  document.addEventListener('wheel', primeFromTrustedEvent, true);
  document.addEventListener('pointerdown', primeFromTrustedEvent, true);
  document.addEventListener('touchstart', primeFromTrustedEvent, true);

  // v11 retains the browser input gesture itself.  This matters when a site
  // consumes wheel for canvas zoom/carousels and therefore emits no scroll
  // event at all.  Consecutive samples over the same target are coalesced while
  // the derived layout scroll is suppressed to avoid replaying one gesture
  // twice.  v10 keeps its existing post-layout net-scroll contract unchanged.
  let pendingWheel = null;
  let wheelScrollSuppressionUntil = 0;
  flushPendingWheel = (lifecycleFlush = false) => {
    if (!pendingWheel) return 0;
    const pending = pendingWheel;
    pendingWheel = null;
    clearTimeout(pending.timer);
    const dx = Math.trunc(pending.deltaX);
    const dy = Math.trunc(pending.deltaY);
    if (dx === 0 && dy === 0) return 0;
    send(
      'wheel',
      pending.element,
      { scrollX: dx, scrollY: dy, lifecycleFlush },
      false,
    );
    return 1;
  };
  document.addEventListener('wheel', (event) => {
    if (!RECORDING_V11 || !active || !event.isTrusted) return;
    cancelPointerGesture();
    const rawTarget = eventTargetOf(event);
    const element = rawTarget && rawTarget.nodeType === 1 ? rawTarget : null;
    if (pendingWheel && pendingWheel.element !== element) flushPendingWheel();
    if (!pendingWheel) {
      flushDirtyInputs();
      pendingWheel = {
        element,
        deltaX: 0,
        deltaY: 0,
        timer: 0,
      };
    }
    const deltaMode = Math.trunc(Number(event.deltaMode) || 0);
    const scale = deltaMode === 1
      ? 16
      : deltaMode === 2
        ? Math.max(1, Number(globalThis.innerHeight) || 800)
        : 1;
    pendingWheel.deltaX += (Number(event.deltaX) || 0) * scale;
    pendingWheel.deltaY += (Number(event.deltaY) || 0) * scale;
    clearTimeout(pendingWheel.timer);
    pendingWheel.timer = setTimeout(() => {
      if (active) flushPendingWheel();
    }, 180);
    // Native wheel scrolling and Chromium's smooth-scroll tail may outlive the
    // last WheelEvent.  Scroll listeners still refresh baselines in this window
    // but do not emit a second action.
    wheelScrollSuppressionUntil = Date.now() + 600;
  }, true);

  const flushOneScroll = (key, lifecycleFlush = false) => {
    const pending = pendingScrolls.get(key);
    if (!pending) return 0;
    pendingScrolls.delete(key);
    clearTimeout(pending.timer);
    const now = scrollPosition(pending.element || document);
    const base = baseOf(pending.element) || { x: 0, y: 0 };
    const dx = Math.round(now.x - base.x);
    const dy = Math.round(now.y - base.y);
    setBase(pending.element, { x: now.x, y: now.y });
    if (dx === 0 && dy === 0) return 0;
    send(
      'scroll',
      pending.element,
      { scrollX: dx, scrollY: dy, lifecycleFlush },
      false,
    );
    return 1;
  };
  flushPendingScrolls = (lifecycleFlush = false) => {
    let committed = 0;
    // Map iteration is insertion ordered, preserving the order in which distinct
    // scroll surfaces first became pending.
    for (const key of Array.from(pendingScrolls.keys())) {
      committed += flushOneScroll(key, lifecycleFlush);
    }
    return committed;
  };

  document.addEventListener('scroll', (event) => {
    if (!active || !event.isTrusted) return;
    // A native scrollbar drag or touch-pan has the stronger semantic scroll
    // result. Do not replay both the low-level pointer path and the net scroll.
    cancelPointerGesture();
    const position = scrollPosition(eventTargetOf(event));
    const element = position.element;
    const key = element || document;
    if (RECORDING_V11 && Date.now() <= wheelScrollSuppressionUntil) {
      setBase(element, { x: position.x, y: position.y });
      const previous = pendingScrolls.get(key);
      if (previous) {
        clearTimeout(previous.timer);
        pendingScrolls.delete(key);
      }
      return;
    }
    if (!pendingScrolls.has(key)) {
      // Scrollbar drags and keyboard scrolling may have no preceding wheel.
      // Preserve any dirty form value before assigning this scroll's queue slot.
      flushDirtyInputs();
    }
    if (element && !scrollBases.has(element)) {
      // A trusted wheel/pointer/key/touch precursor normally installed the
      // exact baseline. If a browser supplies no observable precursor, adopt
      // the current position and intentionally emit no guessed absolute delta.
      scrollBases.set(element, { x: position.x, y: position.y });
    }
    const previous = pendingScrolls.get(key);
    if (previous) clearTimeout(previous.timer);
    const pending = { element, timer: 0 };
    pending.timer = setTimeout(() => {
      if (active) flushOneScroll(key);
    }, 250);
    pendingScrolls.set(key, pending);
  }, true);
})();`;
}

/** 解析注入脚本上报的 JSON。任何不合法载荷一律丢弃，不做修补。 */
export function parseRecorderEvent(raw: string): RecorderEvent | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  const hasSchemaVersion = Object.prototype.hasOwnProperty.call(record, 'schemaVersion');
  // 缺省只为兼容升级过程中的旧注入脚本。声明了版本却不是当前版本，说明双方对
  // 字段语义没有共识，必须整条丢弃，不能“尽量解析”后把未知内容写进持久轨迹。
  if (hasSchemaVersion && record.schemaVersion !== RECORDER_EVENT_SCHEMA_VERSION) return null;
  const schemaVersion = hasSchemaVersion ? RECORDER_EVENT_SCHEMA_VERSION : 1;
  const seq = Number(record.seq);
  if (
    schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    && record.causalId !== 0
  ) return null;
  const causalToken = schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    ? Number(record.causalToken ?? 0)
    : 0;
  if (
    !Number.isSafeInteger(causalToken)
    || causalToken < 0
    || causalToken > 9_007_199_254_740_991
  ) return null;
  const type = String(record.type ?? '');
  const allowed: RecorderEventType[] = [
    'click', 'dblclick', 'drag', 'drop', 'hover', 'input', 'upload', 'submit', 'key',
    'pointerGesture', 'scroll', 'wheel', 'navigate',
  ];
  if (!Number.isInteger(seq) || seq <= 0) return null;
  if (!allowed.includes(type as RecorderEventType)) return null;
  // upload first became a durable action in v5. Treating an unversioned legacy packet as
  // upload would give old fields new semantics and bypass the fixed-shape contract below.
  if (type === 'upload' && schemaVersion !== RECORDER_EVENT_SCHEMA_VERSION) return null;
  if (type === 'drop' && schemaVersion !== RECORDER_EVENT_SCHEMA_VERSION) return null;
  if (type === 'pointerGesture' && schemaVersion !== RECORDER_EVENT_SCHEMA_VERSION) return null;
  const tier = String(record.tier ?? '');
  const tiers: FieldTier[] = ['plain', 'identifier', 'secret', 'handoff'];
  // tier 是描述性元数据。未知值不能触发数据丢失，按 plain 继续保留完整动作证据。
  const safeTier = (
    tiers.includes(tier as FieldTier)
      ? tier
      : 'plain'
  ) as FieldTier;
  const sourceValue = typeof record.value === 'string' ? record.value : '';
  const rawValue = sourceValue;
  const valueTruncated = schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    ? false
    : record.valueTruncated === true;
  const parseTarget = (raw: unknown): RecorderTarget | null => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const rawTarget = raw as Record<string, unknown>;
    const rawFramePath = rawTarget.framePath;
    if (
      schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
      && (
        typeof rawTarget.contentEditable !== 'boolean'
        || !Array.isArray(rawFramePath)
        || rawFramePath.some(
          (item) => (
            typeof item !== 'string'
            || !item
          ),
        )
      )
    ) return null;
    return {
      tag: sanitizeRecorderEvidenceToken(
        String(rawTarget.tag ?? '').toLowerCase(),
      ),
      text: String(rawTarget.text ?? ''),
      ariaLabel: String(rawTarget.ariaLabel ?? ''),
      href: String(rawTarget.href ?? ''),
      ordinal: Number.isFinite(Number(rawTarget.ordinal))
        ? Math.max(0, Math.trunc(Number(rawTarget.ordinal)))
        : 0,
      id: sanitizeRecorderEvidenceToken(rawTarget.id),
      name: sanitizeRecorderEvidenceToken(rawTarget.name),
      role: sanitizeRecorderEvidenceToken(
        String(rawTarget.role ?? '').toLowerCase(),
      ),
      inputType: sanitizeRecorderEvidenceToken(
        String(rawTarget.inputType ?? '').toLowerCase(),
      ),
      contentEditable: rawTarget.contentEditable === true,
      testId: sanitizeRecorderEvidenceToken(rawTarget.testId),
      testIdAttribute: (
        rawTarget.testIdAttribute === 'data-testid'
        || rawTarget.testIdAttribute === 'data-test'
        || rawTarget.testIdAttribute === 'data-qa'
      ) ? rawTarget.testIdAttribute : '',
      // Playwright selector 语法包含 Unicode、转义、内部引擎与跨 frame 分段，
      // schema v10 原样保存，避免录制层破坏可执行 selector。
      cssPath: sanitizeSelectorFragment(rawTarget.cssPath),
      framePath: Array.isArray(rawFramePath)
        ? (
          schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
            ? [...rawFramePath]
            : rawFramePath.map(sanitizeSelectorFragment).filter(Boolean)
        )
        : [],
    };
  };
  const target: RecorderTarget | null = parseTarget(record.target);
  const dragTarget: RecorderTarget | null = type === 'drag'
    ? parseTarget(record.dragTarget)
    : null;
  if (type === 'drag' && !dragTarget) return null;
  const rawRecordedSelector = typeof record.recordedSelector === 'string'
    ? record.recordedSelector
    : '';
  const rawRecordedDragSelector = typeof record.recordedDragSelector === 'string'
    ? record.recordedDragSelector
    : '';
  let selectorSource: RecorderEvent['selectorSource'] = 'unavailable';
  let recordedSelector = '';
  let recordedDragSelector = '';
  if (schemaVersion === RECORDER_EVENT_SCHEMA_VERSION) {
    const declaredSource = record.selectorSource;
    if (
      declaredSource !== undefined
      && declaredSource !== 'playwright'
      && declaredSource !== 'unavailable'
    ) return null;
    selectorSource = declaredSource === 'playwright' ? 'playwright' : 'unavailable';
    if (selectorSource === 'playwright') {
      if (
        !target
        || !rawRecordedSelector
        || (type === 'drag' && !rawRecordedDragSelector)
        || (type !== 'drag' && Boolean(rawRecordedDragSelector))
      ) return null;
      recordedSelector = rawRecordedSelector;
      recordedDragSelector = type === 'drag' ? rawRecordedDragSelector : '';
    } else if (rawRecordedSelector || rawRecordedDragSelector) {
      return null;
    }
  }
  const rawValues = record.values;
  let values: string[] = [];
  if (schemaVersion === RECORDER_EVENT_SCHEMA_VERSION) {
    if (
      !Array.isArray(rawValues)
      || rawValues.some((item) => typeof item !== 'string')
    ) return null;
    const multiSelectInput = (
      type === 'input'
      && target?.tag === 'select'
      && target.inputType === 'select-multiple'
    );
    // v6 is fixed-shape: only select-multiple may populate values. This keeps
    // ordinary text and legacy select-one semantics unambiguous.
    if (!multiSelectInput && rawValues.length !== 0) return null;
    values = multiSelectInput ? [...rawValues] : [];
  }
  const pointerAction = (
    type === 'click'
    || type === 'dblclick'
    || type === 'drag'
    || type === 'pointerGesture'
  );
  const clickCountAction = type === 'click' || type === 'dblclick' || type === 'drag';
  const rawClickButton = String(record.clickButton ?? '');
  const clickButtons: RecorderClickButton[] = ['left', 'middle', 'right'];
  if (
    schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    && pointerAction
    && !clickButtons.includes(rawClickButton as RecorderClickButton)
  ) return null;
  const clickButton = pointerAction && clickButtons.includes(rawClickButton as RecorderClickButton)
    ? rawClickButton as RecorderClickButton
    : (pointerAction ? 'left' : undefined);
  const modifierWhitelist: RecorderModifier[] = ['Alt', 'Control', 'Meta', 'Shift'];
  const rawModifiers = record.modifiers;
  if (
    schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    && pointerAction
    && (
      !Array.isArray(rawModifiers)
      || rawModifiers.length > modifierWhitelist.length
      || rawModifiers.some((item) => !modifierWhitelist.includes(item as RecorderModifier))
      || new Set(rawModifiers).size !== rawModifiers.length
    )
  ) return null;
  const modifiers = pointerAction && Array.isArray(rawModifiers)
    ? modifierWhitelist.filter((modifier) => rawModifiers.includes(modifier))
    : [];
  const rawPointerType = record.pointerType;
  const pointerTypes: RecorderPointerType[] = ['mouse', 'pen', 'touch'];
  if (
    schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    && (
      type === 'pointerGesture'
        ? (
            rawPointerType !== undefined
            && rawPointerType !== ''
            && !pointerTypes.includes(rawPointerType as RecorderPointerType)
          )
        : rawPointerType !== undefined && rawPointerType !== ''
    )
  ) return null;
  const pointerType: RecorderPointerType | undefined = type === 'pointerGesture'
    ? (
        pointerTypes.includes(rawPointerType as RecorderPointerType)
          ? rawPointerType as RecorderPointerType
          : 'mouse'
      )
    : undefined;
  const rawClickCount = Number(record.clickCount);
  if (
    schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    && clickCountAction
    && (!Number.isSafeInteger(rawClickCount) || rawClickCount < 1)
  ) return null;
  if (
    schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    && type === 'pointerGesture'
    && rawClickCount !== 0
  ) return null;
  const rawPosition = record.position;
  let position: RecorderPoint | null = null;
  if (schemaVersion === RECORDER_EVENT_SCHEMA_VERSION && rawPosition !== null) {
    if (
      (type !== 'click' && type !== 'dblclick' && type !== 'hover')
      || !rawPosition
      || typeof rawPosition !== 'object'
      || Array.isArray(rawPosition)
      || Object.keys(rawPosition as Record<string, unknown>).length !== 2
      || !Object.prototype.hasOwnProperty.call(rawPosition, 'x')
      || !Object.prototype.hasOwnProperty.call(rawPosition, 'y')
    ) return null;
    const point = rawPosition as Record<string, unknown>;
    if (
      typeof point.x !== 'number'
      || typeof point.y !== 'number'
      || !Number.isFinite(point.x)
      || !Number.isFinite(point.y)
      || point.x < 0
      || point.y < 0
    ) return null;
    position = { x: point.x, y: point.y };
  }
  const parseDragPoint = (rawPoint: unknown): RecorderPoint | null | false => {
    if (rawPoint === null) return null;
    if (
      !rawPoint
      || typeof rawPoint !== 'object'
      || Array.isArray(rawPoint)
      || Object.keys(rawPoint as Record<string, unknown>).length !== 2
      || !Object.prototype.hasOwnProperty.call(rawPoint, 'x')
      || !Object.prototype.hasOwnProperty.call(rawPoint, 'y')
    ) return false;
    const point = rawPoint as Record<string, unknown>;
    if (
      typeof point.x !== 'number'
      || typeof point.y !== 'number'
      || !Number.isFinite(point.x)
      || !Number.isFinite(point.y)
      || point.x < 0
      || point.y < 0
    ) return false;
    return { x: point.x, y: point.y };
  };
  let dragSourcePosition: RecorderPoint | null = null;
  let dragTargetPosition: RecorderPoint | null = null;
  if (schemaVersion === RECORDER_EVENT_SCHEMA_VERSION) {
    const sourcePoint = parseDragPoint(record.dragSourcePosition);
    const targetPoint = parseDragPoint(record.dragTargetPosition);
    if (
      sourcePoint === false
      || targetPoint === false
      || type !== 'drag'
      && (sourcePoint !== null || targetPoint !== null)
    ) return null;
    dragSourcePosition = type === 'drag' ? sourcePoint : null;
    dragTargetPosition = type === 'drag' ? targetPoint : null;
  }
  const rawGestureStart = record.gestureStart ?? null;
  const rawGesturePoints = record.gesturePoints ?? [];
  let gestureStart: RecorderPointerSample | null = null;
  const gesturePoints: RecorderGesturePoint[] = [];
  const pointerTelemetryRanges = {
    pressure: [0, 1],
    tangentialPressure: [-1, 1],
    tiltX: [-90, 90],
    tiltY: [-90, 90],
    twist: [0, 359],
    width: [0, Number.POSITIVE_INFINITY],
    height: [0, Number.POSITIVE_INFINITY],
  } as const;
  const parsePointerSample = (
    rawSample: unknown,
    elapsed: boolean,
  ): RecorderPointerSample | RecorderGesturePoint | null => {
    if (
      !rawSample
      || typeof rawSample !== 'object'
      || Array.isArray(rawSample)
    ) return null;
    const sample = rawSample as Record<string, unknown>;
    const required = elapsed ? ['x', 'y', 'elapsedMs'] : ['x', 'y'];
    const allowed = new Set([
      ...required,
      ...Object.keys(pointerTelemetryRanges),
    ]);
    if (
      required.some((name) => !Object.prototype.hasOwnProperty.call(sample, name))
      || Object.keys(sample).some((name) => !allowed.has(name))
      || typeof sample.x !== 'number'
      || typeof sample.y !== 'number'
      || !Number.isFinite(sample.x)
      || !Number.isFinite(sample.y)
    ) return null;
    const clean: RecorderPointerSample = { x: sample.x, y: sample.y };
    const telemetryNames = Object.keys(
      pointerTelemetryRanges,
    ) as Array<keyof typeof pointerTelemetryRanges>;
    for (const name of telemetryNames) {
      if (!Object.prototype.hasOwnProperty.call(sample, name)) continue;
      const value = sample[name];
      const [minimum, maximum] = pointerTelemetryRanges[name];
      if (
        typeof value !== 'number'
        || !Number.isFinite(value)
        || value < minimum
        || value > maximum
      ) return null;
      clean[name] = value;
    }
    if (!elapsed) return clean;
    if (
      typeof sample.elapsedMs !== 'number'
      || !Number.isFinite(sample.elapsedMs)
    ) return null;
    return { ...clean, elapsedMs: sample.elapsedMs };
  };
  if (schemaVersion === RECORDER_EVENT_SCHEMA_VERSION) {
    if (type === 'pointerGesture') {
      if (
        !Array.isArray(rawGesturePoints)
        || rawGesturePoints.length === 0
      ) return null;
      const start = parsePointerSample(rawGestureStart, false);
      if (!start || Object.prototype.hasOwnProperty.call(start, 'elapsedMs')) return null;
      gestureStart = start;
      let previousElapsedMs = 0;
      for (const rawPoint of rawGesturePoints) {
        const parsedPoint = parsePointerSample(rawPoint, true);
        if (
          !parsedPoint
          || !Object.prototype.hasOwnProperty.call(parsedPoint, 'elapsedMs')
        ) return null;
        const point = parsedPoint as RecorderGesturePoint;
        if (point.elapsedMs < previousElapsedMs) return null;
        previousElapsedMs = point.elapsedMs;
        gesturePoints.push(point);
      }
    } else if (
      rawGestureStart !== null
      || !Array.isArray(rawGesturePoints)
      || rawGesturePoints.length !== 0
    ) {
      return null;
    }
  }
  // Dialog decisions are generated only by BrowserHost from CDP. The injected
  // recorder must keep this surface empty so a page cannot manufacture a
  // durable accept/dismiss decision through the document-world binding.
  if (
    schemaVersion === RECORDER_EVENT_SCHEMA_VERSION
    && (
      record.dialogAction !== ''
      || record.dialogType !== ''
      || record.dialogText !== ''
    )
  ) return null;
  const uploadAction = type === 'upload';
  const dropAction = type === 'drop';
  const rawUploadMode = String(record.uploadMode ?? '');
  const rawPaths = record.paths;
  const rawFileCount = Number(record.fileCount);
  const rawMultiple = record.multiple;
  const rawAccept = record.accept;
  const rawDropData = record.dropData;
  let dropData: Record<string, string> = {};
  if (schemaVersion === RECORDER_EVENT_SCHEMA_VERSION) {
    if (uploadAction) {
      if (
        !Number.isSafeInteger(rawFileCount)
        || rawFileCount < 0
        || typeof rawMultiple !== 'boolean'
        || typeof rawAccept !== 'string'
        || !Array.isArray(rawPaths)
        || rawPaths.length !== 0
        || (
          rawUploadMode !== (rawFileCount === 0 ? 'clear' : 'handoff')
        )
      ) return null;
    } else if (
      rawUploadMode !== ''
      || !Array.isArray(rawPaths)
      || rawPaths.length !== 0
      || rawMultiple !== false
      || rawAccept !== ''
    ) {
      return null;
    }
    if (dropAction) {
      if (
        !Number.isSafeInteger(rawFileCount)
        || rawFileCount < 0
        || !rawDropData
        || typeof rawDropData !== 'object'
        || Array.isArray(rawDropData)
        || Object.entries(rawDropData as Record<string, unknown>).some(
          ([mime, payload]) => typeof mime !== 'string' || typeof payload !== 'string',
        )
      ) return null;
      dropData = Object.fromEntries(
        Object.entries(rawDropData as Record<string, string>),
      );
    } else if (
      (!uploadAction && rawFileCount !== 0)
      || !rawDropData
      || typeof rawDropData !== 'object'
      || Array.isArray(rawDropData)
      || Object.keys(rawDropData as Record<string, unknown>).length !== 0
    ) {
      return null;
    }
  }
  const targetEvidence: RecorderTargetEvidence = target ? 'synchronous' : 'none';

  let provenance: RecorderProvenance;
  if (schemaVersion === RECORDER_EVENT_SCHEMA_VERSION) {
    const rawProvenance = record.provenance as Record<string, unknown> | null | undefined;
    if (
      !rawProvenance
      || typeof rawProvenance !== 'object'
      || rawProvenance.schemaVersion !== RECORDER_PROVENANCE_SCHEMA_VERSION
      || rawProvenance.source !== 'document-world'
      || rawProvenance.capturePhase !== 'event-callback'
      || rawProvenance.browserTrusted !== true
      || rawProvenance.targetEvidence !== targetEvidence
      || rawProvenance.nativeInput !== 'unverified'
    ) {
      return null;
    }
    // 不把页面对象原样透传；按当前版本重新构造，未知 provenance 字段自然丢弃。
    provenance = {
      schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
      source: 'document-world',
      capturePhase: 'event-callback',
      browserTrusted: true,
      targetEvidence,
      nativeInput: 'unverified',
    };
  } else {
    provenance = {
      schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
      source: 'legacy-isolated-world',
      capturePhase: 'event-callback',
      browserTrusted: true,
      targetEvidence,
      nativeInput: 'unverified',
    };
  }

  const parsed = {
    schemaVersion,
    provenance,
    seq,
    causalId: 0,
    causalToken,
    target,
    dragTarget,
    recordedSelector,
    recordedDragSelector,
    selectorSource,
    type: type as RecorderEventType,
    url: typeof record.url === 'string' ? record.url : '',
    hint: typeof record.hint === 'string' ? record.hint : '',
    tier: safeTier,
    value: rawValue,
    values,
    valueTruncated,
    lifecycleFlush: (
      type === 'input'
      || type === 'scroll'
      || type === 'hover'
      || type === 'wheel'
      || type === 'pointerGesture'
    )
      && record.lifecycleFlush === true,
    key: typeof record.key === 'string' ? record.key : '',
    clickButton,
    clickCount: clickCountAction && Number.isFinite(rawClickCount)
      ? Math.max(1, Math.trunc(rawClickCount))
      : 0,
    position,
    dragSourcePosition,
    dragTargetPosition,
    gestureStart,
    gesturePoints,
    modifiers,
    pointerType,
    dialogAction: '',
    dialogType: '',
    dialogText: '',
    scrollX: Number.isFinite(Number(record.scrollX))
      ? Math.trunc(Number(record.scrollX))
      : 0,
    scrollY: Number.isFinite(Number(record.scrollY))
      ? Math.trunc(Number(record.scrollY))
      : 0,
    uploadMode: uploadAction
      ? (rawFileCount === 0 ? 'clear' : 'handoff')
      : '',
    // Page-origin packets never carry paths. BrowserHost fills this only after resolving
    // File wrappers from the exact CDP execution context/session that raised the event.
    paths: [],
    fileCount: uploadAction || dropAction ? rawFileCount : 0,
    multiple: uploadAction ? rawMultiple === true : false,
    accept: uploadAction && typeof rawAccept === 'string' ? rawAccept : '',
    dropData,
  };
  return retainRecorderEvidence(
    parsed as unknown as Record<string, unknown>,
    safeTier,
  ) as unknown as RecorderEvent;
}

export function parseRecorderControlEvent(
  raw: string,
): {
  type: 'causal-begin' | 'causal-end';
  seq: number;
  token: number;
} | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (
    !value
    || typeof value !== 'object'
    || Array.isArray(value)
  ) return null;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  const seq = Number(record.seq);
  const token = Number(record.token ?? 0);
  const inputControl = record.type === 'causal-begin' || token > 0;
  if (
    record.schemaVersion !== RECORDER_EVENT_SCHEMA_VERSION
    || (record.type !== 'causal-begin' && record.type !== 'causal-end')
    || (
      inputControl
        ? keys.length !== 4 || !keys.includes('token')
        : keys.length !== 3 || keys.includes('token')
    )
    || !Number.isSafeInteger(seq)
    || seq <= 0
    || !Number.isSafeInteger(token)
    || token < 0
    || token > 9_007_199_254_740_991
    || record.type === 'causal-begin'
    && token <= 0
  ) return null;
  return { type: record.type, seq, token };
}
