/**
 * 页面观察面：把 Playwright 的 AI aria 快照无损翻译成 Crew ref 契约。
 *
 * Playwright 快照 ref 只在最近一次 aria snapshot 的生命周期内有效。Crew 对外使用
 * 自己的 `@eN`，但不裁剪 Playwright 返回的文本、节点或 ref；开启 richMetadata 时，
 * 每一个 ref 都会尝试补齐导航、控件类型等 DOM 元数据。
 */

import { locatorFromRef, snapshotRefSelector } from './playwright-compat';
import { classifyFieldTier } from '../browser-recorder';

import type { Locator, Page } from './playwright-compat';
import type { FieldProbe, FieldTier } from '../browser-recorder';

/**
 * DOM 元数据探针的并发度只控制调度压力，不限制输出数量。所有记录最终都会被尝试，
 * 每次 evaluate 各自使用调用者给出的 Playwright timeout。
 */
const FINGERPRINT_CONCURRENCY = 16;

export interface RefRecord {
  /**
   * 完整 Playwright 选择器。快照 ref 的私有 `aria-ref` 拼法只由 compat 层产生；
   * 稳定 selector 则由录制/locate 传入。
   */
  selector: string;
  /** Playwright 原始 ref（`e5` / `f1e2`）；稳定 selector 来路为空。 */
  playwrightRef: string;
  /** Playwright 快照给出的真实 accessible role/name。 */
  role: string;
  name: string;
  /** `elementSecurity` 中的 opaque 键。 */
  securityKey: string;
  /** Rich metadata hash, or a lightweight compatibility token. */
  security: string;
  /** 当前解析后的导航目标。 */
  navigation: string;
  /** 下载/提交授权绑定的解析后目标。 */
  downloadNavigation: string;
  /** 兼容既有能力档：仅提交控件为 `submit`。 */
  action: string;
  /** 更细的动作类别，参与安全指纹并供审计/未来策略使用。 */
  actionKind: string;
  /** 页面内复核使用的动态可访问语义；与 Playwright 快照语义一同保留供审计。 */
  semanticRole: string;
  semanticName: string;
  /** 指纹采集时的 baseURI，已同时进入 security material。 */
  documentBaseURI: string;
  /** 元素所属真实 document 的 location.href；与可被 <base> 改写的 baseURI 分开。 */
  documentURL: string;
  /** 回放字段证明；全部来自生成 security 的同一次 DOM 采集。 */
  tag: string;
  inputType: string;
  contentEditable: boolean;
  fieldTier: FieldTier;
}

export interface EngineSnapshot {
  text: string;
  url: string;
  title: string;
  refs: Map<string, RefRecord>;
  refKeys: Record<string, string>;
  refActions: Record<string, string>;
  elementSecurity: Record<string, string>;
  elementNavigation: Record<string, string>;
  /** 兼容旧协议；完整快照始终为空字符串。 */
  truncated: string;
}

export interface FingerprintResult {
  security: string;
  navigation: string;
  downloadNavigation: string;
  action: string;
  actionKind: string;
  accessibleRole: string;
  accessibleName: string;
  documentBaseURI: string;
  documentURL: string;
  tag: string;
  inputType: string;
  contentEditable: boolean;
  fieldTier: FieldTier;
  /** 兼容旧协议；生产探针完整序列化时为 true。 */
  complete: boolean;
}

interface FingerprintMaterial {
  material: string;
  navigation: string;
  downloadNavigation: string;
  action: string;
  actionKind: string;
  accessibleRole: string;
  accessibleName: string;
  documentBaseURI: string;
  documentURL: string;
  tag: string;
  inputType: string;
  contentEditable: boolean;
  fieldProbe: FieldProbe;
  complete: boolean;
}

interface SnapshotBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface ParsedSnapshotNode {
  role: string;
  name: string;
  indent: number;
  playwrightRef?: string;
  refStart?: number;
  refEnd?: number;
  box?: SnapshotBox;
  boxStart?: number;
  boxEnd?: number;
}

interface SnapshotAttribute {
  name: string;
  value: string | null;
  raw: string;
}

const PLAYWRIGHT_REF = /^(?:f\d+)?e\d+$/;
const NODE_PREFIX = /^(\s*-\s+)(.*)$/;
const NODE_KEY = /^([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+("(?:[^"\\]|\\.)*"))?(.*)$/;
const ATTRIBUTE_NAME = /^[a-zA-Z][a-zA-Z0-9_-]*$/;
const BOX_INTEGER = '-?(?:0|[1-9][0-9]*)';
const BOX_VALUE = new RegExp(
  `^(${BOX_INTEGER}),(${BOX_INTEGER}),(${BOX_INTEGER}),(${BOX_INTEGER})$`,
);

/**
 * 取 Playwright YAML 行的 node key。
 *
 * renderer 在 key 含 `: ` 等 YAML 特殊字符时会把整个 key 放进单引号；普通行则在
 * 第一个“不位于 JSON name 双引号内”的 `:` 处分隔 value。只解析这个 key，因而
 * `- text: [ref=e99]` 和节点正文里的伪 ref 永远不会进入 ref 注册流程。
 */
function snapshotNodeKey(line: string): {
  key: string;
  rawStart: number;
  rawEnd: number;
} | null {
  const prefix = NODE_PREFIX.exec(line);
  if (!prefix) return null;
  const rawStart = prefix[1].length;
  const rest = prefix[2];
  if (!rest) return null;

  if (rest.startsWith("'")) {
    let index = 1;
    let decoded = '';
    while (index < rest.length) {
      if (rest[index] !== "'") {
        decoded += rest[index];
        index += 1;
        continue;
      }
      if (rest[index + 1] === "'") {
        decoded += "'";
        index += 2;
        continue;
      }
      const suffix = rest.slice(index + 1);
      if (suffix && !/^:(?:\s.*)?$/.test(suffix)) return null;
      return { key: decoded, rawStart: rawStart + 1, rawEnd: rawStart + index };
    }
    return null;
  }

  let quoted = false;
  let escaped = false;
  let keyEnd = rest.length;
  for (let index = 0; index < rest.length; index += 1) {
    const char = rest[index];
    if (quoted) {
      if (escaped) {
        escaped = false;
      } else if (char === '\\') {
        escaped = true;
      } else if (char === '"') {
        quoted = false;
      }
      continue;
    }
    if (char === '"') {
      quoted = true;
      continue;
    }
    if (char === ':' && (index + 1 === rest.length || /\s/.test(rest[index + 1]))) {
      keyEnd = index;
      break;
    }
  }
  if (quoted) return null;
  return {
    key: rest.slice(0, keyEnd),
    rawStart,
    rawEnd: rawStart + keyEnd,
  };
}

/**
 * 严格解析一个 aria node 行。
 *
 * ref 必须是 node key 中唯一、独立的结构属性，且满足 Playwright ref grammar。
 * accessible name 内、正文内或额外伪造的 `[ref=...]` 都不会被接受。
 */
function parseSnapshotAttributes(value: string): SnapshotAttribute[] | null {
  const attributes: SnapshotAttribute[] = [];
  let cursor = 0;
  while (cursor < value.length) {
    // Playwright createKey() always separates attributes with whitespace.
    if (!/\s/.test(value[cursor] ?? '')) return null;
    while (cursor < value.length && /\s/.test(value[cursor] ?? '')) cursor += 1;
    if (cursor === value.length) break;
    if (value[cursor] !== '[') return null;
    const close = value.indexOf(']', cursor + 1);
    if (close < 0) return null;
    const raw = value.slice(cursor, close + 1);
    const content = value.slice(cursor + 1, close);
    // Attribute values emitted by ariaSnapshot never contain whitespace/brackets.
    if (!content || /[\s\[\]]/.test(content)) return null;
    const equals = content.indexOf('=');
    const name = equals < 0 ? content : content.slice(0, equals);
    const attributeValue = equals < 0 ? null : content.slice(equals + 1);
    if (
      !ATTRIBUTE_NAME.test(name)
      || (equals >= 0 && (!attributeValue || attributeValue.includes('=')))
    ) {
      return null;
    }
    attributes.push({ name, value: attributeValue, raw });
    cursor = close + 1;
  }
  return attributes;
}

function parseSnapshotBox(value: string | null): SnapshotBox | null {
  if (!value) return null;
  const match = BOX_VALUE.exec(value);
  if (!match) return null;
  const values = match.slice(1).map(Number);
  if (
    values.length !== 4
    || values.some((item) => !Number.isSafeInteger(item))
    || (values[2] ?? -1) < 0
    || (values[3] ?? -1) < 0
  ) {
    return null;
  }
  return {
    x: values[0] ?? 0,
    y: values[1] ?? 0,
    width: values[2] ?? 0,
    height: values[3] ?? 0,
  };
}

function structuralTokenRange(
  line: string,
  extracted: { rawStart: number; rawEnd: number },
  token: string,
  options: { includeLeadingSpace: boolean },
): { start: number; end: number } | null {
  const rawKey = line.slice(extracted.rawStart, extracted.rawEnd);
  const relativeStart = rawKey.lastIndexOf(token);
  if (relativeStart < 0) return null;
  let start = extracted.rawStart + relativeStart;
  const end = start + token.length;
  if (end > extracted.rawEnd) return null;
  if (options.includeLeadingSpace && start > extracted.rawStart && /\s/.test(line[start - 1] ?? '')) {
    start -= 1;
  }
  return { start, end };
}

function parseSnapshotNode(line: string): ParsedSnapshotNode | null {
  const indentation = /^ */.exec(line)?.[0] ?? '';
  // renderAriaTree() uses exactly two ASCII spaces per depth and "- ". Tabs,
  // odd indentation or other YAML list spellings are not Playwright node lines.
  if (indentation.length % 2 !== 0 || !line.startsWith('- ', indentation.length)) return null;
  const extracted = snapshotNodeKey(line);
  if (!extracted) return null;
  const node = NODE_KEY.exec(extracted.key);
  if (!node) return null;

  let name = '';
  if (node[2]) {
    try {
      const parsed = JSON.parse(node[2]);
      if (typeof parsed !== 'string') return null;
      name = parsed;
    } catch {
      return null;
    }
  }

  const attributes = parseSnapshotAttributes(node[3] ?? '');
  if (!attributes) return null;
  const refs = attributes.filter((attribute) => attribute.name === 'ref');
  if (refs.length > 1) return null;
  const playwrightRef = refs[0]?.value ?? '';
  if (playwrightRef && !PLAYWRIGHT_REF.test(playwrightRef)) return null;

  let refRange: { start: number; end: number } | null = null;
  if (playwrightRef) {
    refRange = structuralTokenRange(
      line,
      extracted,
      refs[0]?.raw ?? '',
      { includeLeadingSpace: false },
    );
    if (!refRange) return null;
  }

  const boxes = attributes.filter((attribute) => attribute.name === 'box');
  // The pinned Playwright appends box last. An earlier/duplicate/invalid box can only be
  // malformed input, so it is treated as untrusted text rather than geometry.
  const structuralBox = boxes.length === 1 && attributes.at(-1) === boxes[0]
    ? parseSnapshotBox(boxes[0]?.value ?? null)
    : null;
  const boxRange = structuralBox
    ? structuralTokenRange(
        line,
        extracted,
        boxes[0]?.raw ?? '',
        { includeLeadingSpace: true },
      )
    : null;

  return {
    role: node[1].toLowerCase(),
    name,
    indent: indentation.length,
    ...(playwrightRef && refRange
      ? {
          playwrightRef,
          refStart: refRange.start,
          refEnd: refRange.end,
        }
      : {}),
    ...(structuralBox && boxRange
      ? {
          box: structuralBox,
          boxStart: boxRange.start,
          boxEnd: boxRange.end,
        }
      : {}),
  };
}

/** 阻止上层文本 regex 把页面正文里的伪 ref/box 当成结构元数据。 */
function sanitizeUntrustedMetadataText(value: string): string {
  return value
    .replace(/\[ref=/g, '[page-ref=')
    .replace(/\[box=/g, '[page-box=');
}

/**
 * 页面内元素材料。
 *
 * 这是 Locator.evaluate 的回调，必须完全自包含。它把节点的动态语义、document base、
 * 解析后的目的地、form 语义、完整 select option 集合和动作类别绑定到同一份材料。
 * 不截断属性、文本或关联 label，避免长控件在快照与回放间失真。
 */
function fingerprintInPage(element: Element): FingerprintMaterial {
  const normalized = (value: string): string => value.replace(/\s+/g, ' ').trim();
  const elementText = (root: Element): string => normalized(root.textContent ?? '');
  const tag = element.tagName.toUpperCase();
  const document = element.ownerDocument;
  let contentEditable = false;
  try {
    const inheritedEditable = (element as HTMLElement).isContentEditable === true;
    const root = element.getRootNode();
    const parent = element.parentElement
      ?? ('host' in root ? (root as ShadowRoot).host : null);
    // Playwright fill acts on the locator itself; inherited editable descendants must not
    // be treated as independent form controls. Bind the capability only to the editing host.
    contentEditable = inheritedEditable
      && (!parent || (parent as HTMLElement).isContentEditable !== true);
  } catch {}
  const documentBaseURI = document.baseURI;
  // 与 baseURI 分开保存：页面可用 <base href> 合法改写 baseURI，但无法借此改变
  // 元素实际所属 frame 的 document.location。回放必须用后者做 frame host 约束。
  const documentURL = document.location.href;

  const resolveURL = (value: string): string => {
    if (!value) return '';
    try {
      return new URL(value, documentBaseURI).toString();
    } catch {
      return '';
    }
  };

  const explicitRole = normalized(element.getAttribute('role') ?? '').split(' ')[0] ?? '';
  let inputType = (element.getAttribute('type') ?? '').toLowerCase();
  if (tag === 'INPUT' || tag === 'BUTTON' || tag === 'SELECT' || tag === 'TEXTAREA') {
    try {
      // 使用浏览器归一化后的 IDL 属性：缺失/非法 input type 会成为 text，
      // select/textarea/button 也得到与录制器完全相同的控件类型。
      inputType = String(
        (element as Element & { type?: unknown }).type ?? inputType,
      ).toLowerCase();
    } catch {}
  }
  const implicitRole = (): string => {
    if ((tag === 'A' || tag === 'AREA') && element.hasAttribute('href')) return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'SELECT') return element.hasAttribute('multiple') || Number(element.getAttribute('size') ?? '0') > 1
      ? 'listbox'
      : 'combobox';
    if (tag === 'OPTION') return 'option';
    if (tag === 'IMG') return 'img';
    if (tag === 'INPUT') {
      if (['button', 'submit', 'reset', 'image'].includes(inputType)) return 'button';
      if (inputType === 'checkbox') return 'checkbox';
      if (inputType === 'radio') return 'radio';
      if (inputType === 'range') return 'slider';
      if (inputType === 'number') return 'spinbutton';
      if (inputType === 'search') return 'searchbox';
      if (inputType !== 'hidden') return 'textbox';
    }
    return '';
  };

  const labelParts: string[] = [];
  const labelledBy = normalized(element.getAttribute('aria-labelledby') ?? '');
  if (labelledBy) {
    const ids = labelledBy.split(/\s+/).filter(Boolean);
    const root = element.getRootNode();
    for (const id of ids) {
      const labelled = (
        'getElementById' in root
          ? (root as Document | ShadowRoot).getElementById(id)
          : null
      ) ?? document.getElementById(id);
      if (labelled) {
        labelParts.push(elementText(labelled));
      }
    }
  }
  const controlLabels = (element as Element & { labels?: NodeListOf<HTMLLabelElement> | null }).labels;
  if (controlLabels) {
    for (let index = 0; index < controlLabels.length; index += 1) {
      const label = controlLabels[index];
      if (label) labelParts.push(elementText(label));
    }
  }

  const text = elementText(element);
  const ariaLabel = normalized(element.getAttribute('aria-label') ?? '');
  const labelledName = normalized(labelParts.join(' '));
  const valueName = ['button', 'submit', 'reset'].includes(inputType)
    ? normalized(element.getAttribute('value') ?? '')
    : '';
  const imageName = tag === 'IMG' || inputType === 'image'
    ? normalized(element.getAttribute('alt') ?? '')
    : '';
  const titleName = normalized(element.getAttribute('title') ?? '');
  const accessibleName = labelledName || ariaLabel || imageName || valueName || text || titleName;
  const accessibleRole = explicitRole || implicitRole();
  const fieldProbe: FieldProbe = {
    type: inputType,
    autocomplete: element.getAttribute('autocomplete') ?? '',
    name: element.getAttribute('name') ?? '',
    id: element.getAttribute('id') ?? '',
    placeholder: element.getAttribute('placeholder') ?? '',
    ariaLabel: element.getAttribute('aria-label') ?? '',
    labelText: labelParts.join(' '),
  };

  let beforeContent = '';
  let afterContent = '';
  try {
    const view = document.defaultView;
    beforeContent = view?.getComputedStyle(element, '::before').content ?? '';
    afterContent = view?.getComputedStyle(element, '::after').content ?? '';
  } catch {
    // CSS generated content is supplementary. Failure does not make the DOM identity incomplete.
  }

  const formProperty = (element as Element & { form?: HTMLFormElement | null }).form;
  let form = formProperty ?? null;
  const explicitForm = element.getAttribute('form');
  if (!form && explicitForm) {
    const candidate = document.getElementById(explicitForm);
    form = candidate?.tagName.toUpperCase() === 'FORM' ? candidate as HTMLFormElement : null;
  }
  if (!form) {
    const closest = element.closest('form');
    form = closest?.tagName.toUpperCase() === 'FORM' ? closest as HTMLFormElement : null;
  }

  const hrefRaw = tag === 'A' || tag === 'AREA' ? element.getAttribute('href') ?? '' : '';
  const navigation = resolveURL(hrefRaw);
  const submitType = (element.getAttribute('type') ?? (tag === 'BUTTON' ? 'submit' : '')).toLowerCase();
  const isSubmit =
    (tag === 'BUTTON' && submitType === 'submit'
      && Boolean(form || explicitForm || element.hasAttribute('formaction')))
    || (tag === 'INPUT' && (submitType === 'submit' || submitType === 'image'));
  const formActionRaw = element.hasAttribute('formaction')
    ? element.getAttribute('formaction') ?? ''
    : form?.getAttribute('action') ?? '';
  const formNavigation = isSubmit
    ? resolveURL(formActionRaw || document.location.href)
    : '';
  const downloadNavigation = navigation || formNavigation;
  const action = isSubmit ? 'submit' : '';

  let actionKind = 'activate';
  if (isSubmit) actionKind = 'submit';
  else if (tag === 'INPUT' && inputType === 'file') actionKind = 'upload';
  else if (tag === 'SELECT') actionKind = 'select';
  else if (['checkbox', 'radio'].includes(inputType) || ['checkbox', 'radio', 'switch'].includes(accessibleRole)) {
    actionKind = 'toggle';
  } else if (
    tag === 'TEXTAREA'
    || (tag === 'INPUT' && !['button', 'submit', 'reset', 'image', 'file', 'hidden'].includes(inputType))
    || contentEditable
  ) {
    actionKind = 'input';
  } else if (navigation) {
    actionKind = element.hasAttribute('download') ? 'download' : 'navigate';
  }

  const parts: string[] = [
    `tag\0${tag}`,
    `document-url\0${documentURL}`,
    `document-base\0${documentBaseURI}`,
    `accessible-role\0${accessibleRole}`,
    `accessible-name\0${accessibleName}`,
    `semantic-text\0${text}`,
    `label-text\0${labelParts.join(' ')}`,
    `pseudo-before\0${beforeContent}`,
    `pseudo-after\0${afterContent}`,
    `navigation\0${navigation}`,
    `download-navigation\0${downloadNavigation}`,
    `form-navigation\0${formNavigation}`,
    `action\0${action}`,
    `action-kind\0${actionKind}`,
    `content-editable\0${contentEditable}`,
  ];

  const securityAttributes = new Set([
    'action', 'alt', 'autocomplete', 'disabled', 'download', 'enctype', 'form', 'formaction',
    'formenctype', 'formmethod', 'formnovalidate', 'formtarget', 'href', 'id', 'label', 'method',
    'contenteditable', 'multiple', 'name', 'novalidate', 'placeholder', 'readonly', 'rel',
    'required', 'role', 'size',
    'src', 'target', 'type', 'value',
  ]);
  const attributeNames: string[] = [];
  for (let index = 0; index < element.attributes.length; index += 1) {
    attributeNames.push(element.attributes[index].name);
  }
  attributeNames.sort();
  for (const attributeName of attributeNames) {
    if (
      !securityAttributes.has(attributeName)
      && !attributeName.startsWith('aria-')
      && !attributeName.startsWith('on')
    ) {
      continue;
    }
    // Text/password input 的 value attribute 可能含秘密，且并非目标身份；提交按钮的
    // value 则是可见语义，仍需绑定。
    if (
      attributeName === 'value'
      && tag === 'INPUT'
      && !['button', 'submit', 'reset', 'image', 'checkbox', 'radio'].includes(inputType)
    ) {
      continue;
    }
    parts.push(
      `attr\0${attributeName}\0${element.getAttribute(attributeName) ?? ''}`,
    );
  }

  if (form) {
    parts.push('form\0present\0true');
    for (const attributeName of ['action', 'method', 'target', 'enctype', 'novalidate', 'rel']) {
      parts.push(
        `form\0${attributeName}\0${form.getAttribute(attributeName) ?? ''}`,
      );
    }
  }

  if (tag === 'SELECT') {
    const select = element as HTMLSelectElement;
    const optionCount = select.options.length;
    parts.push(`options\0count\0${optionCount}`);
    for (let index = 0; index < optionCount; index += 1) {
      const option = select.options[index];
      if (!option) continue;
      parts.push([
        'option',
        String(index),
        option.getAttribute('value') ?? option.textContent ?? '',
        option.getAttribute('label') || elementText(option),
        option.hasAttribute('disabled') ? 'disabled' : 'enabled',
      ].join('\0'));
    }
  }

  return {
    material: parts.join('\n'),
    navigation,
    downloadNavigation,
    action,
    actionKind,
    accessibleRole,
    accessibleName,
    documentBaseURI,
    documentURL,
    tag: tag.toLowerCase(),
    inputType,
    contentEditable,
    fieldProbe,
    complete: true,
  };
}

function securityKey(role: string, name: string, occurrence: number): string {
  return `${role}\0${name}\0${occurrence}`;
}

/**
 * Cheap capability hint used before the optional rich DOM probe completes.
 *
 * It is deliberately advisory: the action layer checks the concrete element and lets
 * Playwright enforce editability/actionability. Unknown/custom roles remain executable.
 */
function actionKindFromRole(role: string): string {
  if (['textbox', 'searchbox', 'spinbutton', 'slider'].includes(role)) return 'input';
  if (['combobox', 'listbox'].includes(role)) return 'select';
  if (['checkbox', 'radio', 'switch'].includes(role)) return 'toggle';
  return 'activate';
}

export interface CaptureOptions {
  /** 兼容调用协议；无损快照不会因 compact/full 模式丢弃页面行。 */
  full: boolean;
  /**
   * Request layout boxes only for an explicit consumer. Playwright's normal
   * MCP snapshot path leaves this off; collecting every bounding box forces a
   * full layout walk on large pages even when the rendered snapshot removes
   * those attributes.
   */
  boxes?: boolean;
  /**
   * 缺省/false 时只解析 ariaSnapshot 本身，不再发 locator.evaluate/title 等后续 Runtime
   * 调用。Electron OOPIF 在 ariaSnapshot 后并发补元数据可能卡住主 Runtime，
   * 因此普通生产快照默认也是 ref-only；只在显式诊断契约中开启。
   */
  richMetadata?: boolean;
  /**
   * 兼容旧调用协议。完整 ref/元数据不再按 viewport 分配预算，因此该提示不影响结果。
   */
  viewport?: { width: number; height: number };
  hash: (value: string) => string;
  timeoutMs: number;
}

export type SnapshotFindQuery =
  | { text: string; regex?: never }
  | { text?: never; regex: string };

export class SnapshotFindError extends Error {}

async function fingerprintLocator(
  locator: Locator,
  hash: (value: string) => string,
  timeoutMs: number,
): Promise<FingerprintResult> {
  const state = await locator.evaluate(fingerprintInPage, undefined, { timeout: timeoutMs });
  // Older deterministic test doubles may not return `complete`; the production page
  // callback always returns true now that it serializes the complete element material.
  const complete = state.complete !== false;
  let fieldTier: FieldTier = 'secret';
  try {
    fieldTier = classifyFieldTier(state.fieldProbe);
  } catch {
    // 分类异常按最敏感处理；这与录制器页面侧的 fail-closed 语义一致。
  }
  const tag = String(state.tag ?? '').toLowerCase();
  const inputType = String(state.inputType ?? '').toLowerCase();
  const contentEditable = state.contentEditable === true;
  const attestedMaterial = [
    state.material,
    `attested-tag\0${tag}`,
    `attested-input-type\0${inputType}`,
    `attested-content-editable\0${contentEditable}`,
    `attested-field-tier\0${fieldTier}`,
  ].join('\n');
  return {
    security: hash(attestedMaterial),
    navigation: state.navigation,
    downloadNavigation: state.downloadNavigation,
    action: state.action,
    actionKind: state.actionKind ?? '',
    accessibleRole: state.accessibleRole ?? '',
    accessibleName: state.accessibleName ?? '',
    documentBaseURI: state.documentBaseURI ?? '',
    documentURL: state.documentURL ?? '',
    tag,
    inputType,
    contentEditable,
    fieldTier,
    complete,
  };
}

/**
 * 以固定并发度完成所有指纹。并发度只保护 CDP 调度稳定性，不是能力预算；每一条记录
 * 都会启动一次探针，并各自使用调用者给出的 Playwright timeout。
 */
async function fingerprintRecords(
  page: Page,
  records: Array<[string, RefRecord]>,
  options: CaptureOptions,
): Promise<void> {
  let next = 0;
  const worker = async (): Promise<void> => {
    while (next < records.length) {
      const index = next;
      next += 1;
      const entry = records[index];
      if (!entry) return;
      const [, record] = entry;
      try {
        const state = await fingerprintLocator(
          locatorFromRef(page, record.playwrightRef),
          options.hash,
          options.timeoutMs,
        );
        // Every exposed ref already has a lightweight functional baseline. Rich
        // metadata is optional; an oversized/custom control must not lose its ref
        // merely because the supplementary probe could not build a full fingerprint.
        record.security = state.security;
        record.navigation = state.navigation;
        record.downloadNavigation = state.downloadNavigation;
        record.action = state.action;
        record.actionKind = state.actionKind || record.actionKind;
        record.semanticRole = state.accessibleRole;
        record.semanticName = state.accessibleName;
        record.documentBaseURI = state.documentBaseURI;
        record.documentURL = state.documentURL;
        record.tag = state.tag;
        record.inputType = state.inputType;
        record.contentEditable = state.contentEditable;
        record.fieldTier = state.fieldTier;
      } catch {
        // Keep the lightweight baseline and role-derived capability hint.
      }
    }
  };

  const count = Math.min(FINGERPRINT_CONCURRENCY, records.length);
  await Promise.all(Array.from({ length: count }, () => worker()));
}

export interface AriaIdentity {
  role: string;
  name: string;
}

/**
 * Read the browser's actual current accessible role/name for one strict locator.
 *
 * Default-mode ariaSnapshot is intentionally used instead of reimplementing the accessible
 * name algorithm. It refreshes Playwright's aria-ref cache; callers must first materialize a
 * stable selector and invalidate Crew's current ref generation.
 */
export async function ariaIdentityForLocator(
  locator: Locator,
  timeoutMs: number,
): Promise<AriaIdentity> {
  const text = await locator.ariaSnapshot({
    // The observation baseline itself comes from mode=ai. Default mode may omit the
    // locator root (for example a bare contenteditable) and return only its first
    // paragraph, which compares two different nodes. Re-read the same Playwright AI/AX
    // projection so role/name semantics are exactly commensurate with the snapshot ref.
    mode: 'ai',
    // Playwright treats depth=0 as “unbounded” because the renderer checks this option
    // for truthiness. depth=1 is the smallest supported bounded tree and still gives us
    // the target root when it contains an image/span child.
    depth: 1,
    timeout: timeoutMs,
  });
  if (!text) throw new Error('目标的可访问语义为空');
  const lines = text.split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) throw new Error('目标的可访问语义为空');
  const first = parseSnapshotNode(lines[0] ?? '');
  if (!first || first.indent !== 0) throw new Error('无法解析目标的根可访问语义');
  for (const line of lines.slice(1)) {
    const parsed = parseSnapshotNode(line);
    if (parsed?.indent === 0) throw new Error('目标没有唯一的根可访问语义');
  }
  return { role: first.role, name: first.name };
}

function renderSnapshotLine(
  original: string,
  parsed: ParsedSnapshotNode | null,
  nativeRef?: string,
): string {
  if (!parsed) return sanitizeUntrustedMetadataText(original);
  const replacements: Array<{ start: number; end: number; text: string }> = [];
  if (
    parsed.box
    && parsed.boxStart !== undefined
    && parsed.boxEnd !== undefined
  ) {
    replacements.push({ start: parsed.boxStart, end: parsed.boxEnd, text: '' });
  }
  if (
    parsed.playwrightRef
    && parsed.refStart !== undefined
    && parsed.refEnd !== undefined
  ) {
    let start = parsed.refStart;
    if (!nativeRef && start > 0 && /\s/.test(original[start - 1] ?? '')) start -= 1;
    replacements.push({
      start,
      end: parsed.refEnd,
      text: nativeRef ? `[ref=${nativeRef}]` : '',
    });
  }
  replacements.sort((a, b) => a.start - b.start);

  let cursor = 0;
  let rendered = '';
  for (const replacement of replacements) {
    if (replacement.start < cursor) continue;
    rendered += sanitizeUntrustedMetadataText(original.slice(cursor, replacement.start));
    rendered += replacement.text;
    cursor = replacement.end;
  }
  rendered += sanitizeUntrustedMetadataText(original.slice(cursor));
  return rendered;
}

async function snapshotFromRaw(
  page: Page,
  options: CaptureOptions,
  raw: string,
): Promise<EngineSnapshot> {
  const richMetadata = options.richMetadata === true;
  const refs = new Map<string, RefRecord>();
  const refKeys: Record<string, string> = {};
  const refActions: Record<string, string> = {};
  const elementSecurity: Record<string, string> = {};
  const elementNavigation: Record<string, string> = {};
  const occurrences = new Map<string, number>();
  const rawLines = raw.split('\n');
  const parsedLines = rawLines.map(parseSnapshotNode);
  const nativeRefByLine = new Map<number, string>();
  let exposedRefs = 0;
  for (let lineIndex = 0; lineIndex < rawLines.length; lineIndex += 1) {
    const parsed = parsedLines[lineIndex];
    if (
      !parsed?.playwrightRef
      || parsed.refStart === undefined
      || parsed.refEnd === undefined
    ) {
      continue;
    }
    exposedRefs += 1;
    const nativeRef = `@e${exposedRefs}`;
    nativeRefByLine.set(lineIndex, nativeRef);
    // The normal functional path needs only Playwright's exact aria-ref
    // locator. Do not hash or build an auxiliary security namespace for every
    // node. Rich diagnostics may still request the historical metadata
    // explicitly without imposing that cost on snapshots used for automation.
    let key = nativeRef;
    if (richMetadata) {
      const occurrenceIdentity = `${parsed.role}\0${parsed.name}`;
      const occurrence = (occurrences.get(occurrenceIdentity) ?? 0) + 1;
      occurrences.set(occurrenceIdentity, occurrence);
      key = securityKey(parsed.role, parsed.name, occurrence);
    }
    refs.set(nativeRef, {
      selector: snapshotRefSelector(parsed.playwrightRef),
      playwrightRef: parsed.playwrightRef,
      role: parsed.role,
      name: parsed.name,
      securityKey: key,
      security: richMetadata
        ? options.hash(`functional-ref\0${parsed.playwrightRef}\0${key}`)
        : '',
      navigation: '',
      downloadNavigation: '',
      action: '',
      actionKind: actionKindFromRole(parsed.role),
      semanticRole: '',
      semanticName: '',
      documentBaseURI: '',
      documentURL: '',
      tag: '',
      inputType: '',
      contentEditable: false,
      fieldTier: 'secret',
    });
  }

  if (richMetadata) await fingerprintRecords(page, [...refs], options);

  if (richMetadata) {
    for (const [nativeRef, record] of refs) {
      refKeys[nativeRef] = record.securityKey;
      elementSecurity[record.securityKey] = record.security;
      if (record.navigation) elementNavigation[record.securityKey] = record.navigation;
      if (record.action) refActions[nativeRef] = record.action;
    }
  }

  return {
    text: rawLines.map((line, lineIndex) => renderSnapshotLine(
      line,
      parsedLines[lineIndex] ?? null,
      nativeRefByLine.get(lineIndex),
    )).join('\n'),
    url: page.url(),
    title: richMetadata ? await page.title().catch(() => '') : '',
    refs,
    refKeys,
    refActions,
    elementSecurity,
    elementNavigation,
    truncated: '',
  };
}

async function captureRawSnapshot(
  page: Page,
  options: CaptureOptions,
): Promise<string> {
  // Snapshot the Page itself, matching playwright-core's current MCP backend.
  // A body-scoped locator drops non-HTML roots (SVG/XML), frameset documents
  // and pages whose body is replaced while the snapshot call is queued.
  return await page.ariaSnapshot({
    mode: 'ai',
    boxes: options.boxes === true,
    timeout: options.timeoutMs,
  });
}

export async function captureSnapshot(
  page: Page,
  options: CaptureOptions,
): Promise<EngineSnapshot> {
  const raw = await captureRawSnapshot(page, options);
  return await snapshotFromRaw(page, options, raw);
}

const FIND_CONTEXT_LINES = 3;

function compileFindRegex(source: string): RegExp {
  const literal = /^\/(.*)\/([a-z]*)$/.exec(source);
  const pattern = literal ? literal[1] : source;
  const flags = literal ? literal[2].replace(/g/g, '') : '';
  return new RegExp(pattern, flags);
}

function findIndentOf(line: string): number {
  return line.length - line.trimStart().length;
}

function findAncestorIndices(
  lines: string[],
  indents: number[],
  index: number,
): number[] {
  const result: number[] = [];
  let indent = indents[index] ?? 0;
  for (let current = index - 1; current >= 0 && indent > 0; current -= 1) {
    if (!lines[current]?.trim()) continue;
    if ((indents[current] ?? 0) < indent) {
      result.push(current);
      indent = indents[current] ?? 0;
    }
  }
  return result.reverse();
}

/**
 * Playwright MCP-compatible find over one exact aria snapshot.
 *
 * The full ref table and the filtered snippets are both derived from ``raw``.
 * No second aria capture is allowed: Playwright replaces its aria-ref cache on
 * every capture, so mixing snippets from one tree with refs from another would
 * make a returned ref intrinsically stale.
 */
export async function captureSnapshotForFind(
  page: Page,
  options: CaptureOptions,
  query: SnapshotFindQuery,
): Promise<EngineSnapshot> {
  const hasText = typeof query.text === 'string' && Boolean(query.text);
  const hasRegex = typeof query.regex === 'string' && Boolean(query.regex);
  if (hasText === hasRegex) {
    throw new SnapshotFindError(
      hasText
        ? 'Provide only one of "text" or "regex", not both.'
        : 'Provide either "text" or "regex" to search for.',
    );
  }

  let queryLabel: string;
  let matches: (line: string) => boolean;
  if (hasRegex) {
    let regex: RegExp;
    try {
      regex = compileFindRegex(query.regex as string);
    } catch {
      throw new SnapshotFindError('Invalid regular expression');
    }
    queryLabel = String(regex);
    matches = (line: string): boolean => {
      // Sticky/global expressions retain state. Upstream explicitly resets it
      // for every snapshot line; ``g`` itself is removed during compilation.
      regex.lastIndex = 0;
      return regex.test(line);
    };
  } else {
    const text = query.text as string;
    queryLabel = `"${text}"`;
    const needle = text.toLowerCase();
    matches = (line: string): boolean => line.toLowerCase().includes(needle);
  }

  const raw = await captureRawSnapshot(page, options);
  const snapshot = await snapshotFromRaw(page, options, raw);
  const rawLines = raw.split('\n');
  const renderedLines = snapshot.text.split('\n');
  const indents = rawLines.map(findIndentOf);
  const matchedLines: number[] = [];
  for (let index = 0; index < rawLines.length; index += 1) {
    if (matches(rawLines[index] ?? '')) matchedLines.push(index);
  }
  if (!matchedLines.length) {
    snapshot.text = `No matches found for ${queryLabel}.`;
    return snapshot;
  }

  const windows: Array<{ start: number; end: number }> = [];
  for (const line of matchedLines) {
    const start = Math.max(0, line - FIND_CONTEXT_LINES);
    const end = Math.min(rawLines.length - 1, line + FIND_CONTEXT_LINES);
    const last = windows.at(-1);
    if (last && start <= last.end + 1) last.end = Math.max(last.end, end);
    else windows.push({ start, end });
  }

  const path = new Set<number>();
  for (const match of matchedLines) {
    path.add(match);
    for (const ancestor of findAncestorIndices(rawLines, indents, match)) {
      path.add(ancestor);
    }
  }

  // Upstream repeats the root path for separate snippets. A repeated ref would
  // violate Crew's one-native-ref/one-public-ref invariant, so retain the
  // readable ancestor line but expose its structural ref only the first time.
  const emittedRefs = new Set<string>();
  const renderLine = (index: number): string => (
    (renderedLines[index] ?? '').replace(
      /(\s)\[ref=(@e[1-9]\d*)\]/g,
      (_token, whitespace: string, nativeRef: string) => {
        if (emittedRefs.has(nativeRef)) return whitespace;
        emittedRefs.add(nativeRef);
        return `${whitespace}[ref=${nativeRef}]`;
      },
    )
  );

  const snippets = windows.map((window) => {
    const indices = findAncestorIndices(rawLines, indents, window.start);
    for (let index = window.start; index <= window.end; index += 1) {
      indices.push(index);
    }
    const out: string[] = [];
    for (let offset = 0; offset < indices.length; offset += 1) {
      const index = indices[offset] ?? 0;
      const previous = indices[offset - 1];
      if (
        offset > 0
        && previous !== undefined
        && index > previous + 1
        && !path.has(index)
        && !path.has(previous)
      ) {
        out.push(`${' '.repeat(indents[index] ?? 0)}...`);
      }
      out.push(renderLine(index));
    }
    return out.join('\n');
  });
  const matchWord = matchedLines.length === 1 ? 'match' : 'matches';
  snapshot.text = (
    `Found ${matchedLines.length} ${matchWord} for ${queryLabel}:\n\n`
    + snippets.join('\n\n----\n\n')
  );
  return snapshot;
}

/** 对任意稳定 selector 重新计算完整安全状态。 */
export async function fingerprintRef(
  page: Page,
  selector: string,
  hash: (value: string) => string,
  timeoutMs: number,
): Promise<FingerprintResult> {
  return await fingerprintLocator(page.locator(selector), hash, timeoutMs);
}

/** 对已解析 Locator 计算指纹；动作层借此避免重新拼接私有 aria-ref selector。 */
export async function fingerprintResolvedLocator(
  locator: Locator,
  hash: (value: string) => string,
  timeoutMs: number,
): Promise<FingerprintResult> {
  return await fingerprintLocator(locator, hash, timeoutMs);
}
