/**
 * 录制器契约单测：字段分级、完整动作证据与上报载荷解析。
 */
import { describe, it, expect } from 'vitest';
import { runInNewContext } from 'node:vm';

import {
  RECORDER_BINDING,
  RECORDER_CONTROL,
  RECORDER_EVENT_SCHEMA_VERSION,
  RECORDER_PROVENANCE_SCHEMA_VERSION,
  RECORDER_TARGET_STASH,
  classifyFieldTier,
  retainRecorderEvidence,
  parseRecorderEvent,
  recorderScript,
} from '../../src/main/browser-recorder';

function probe(overrides: Partial<Parameters<typeof classifyFieldTier>[0]> = {}) {
  return {
    type: 'text', autocomplete: '', name: '', id: '', placeholder: '', ariaLabel: '',
    labelText: '',
    ...overrides,
  };
}

/**
 * 在一个最小隔离世界里真正运行注入脚本，再直接调用浏览器交给它的 blur listener。
 * 这覆盖 attr → tierOf → classifyFieldTier → send → binding 的整条链；只测导出的
 * classifyFieldTier 会漏掉“上游先 lowerCase、camelCase 边界已丢”的接线 bug。
 */
function captureInjectedInput(
  attributes: Record<string, string>,
  value: string,
  eventName: 'beforeinput' | 'blur' | 'submit' = 'blur',
  options: {
    associatedLabelText?: string;
    labelledByText?: string;
    labelledByInShadow?: boolean;
    priorBeforeInput?: boolean;
    expectedEmissions?: number;
  } = {},
): any {
  const listeners = new Map<string, Array<(event: Record<string, unknown>) => void>>();
  const documentNodes: Array<Record<string, any>> = [];
  const nodesById = new Map<string, Record<string, any>>();
  const ownerDocument: Record<string, any> = {
    querySelectorAll: () => documentNodes,
    defaultView: {
      NodeFilter: { SHOW_TEXT: 4 },
      getComputedStyle(element: Record<string, any>) {
        return {
          borderLeftWidth: String(element.__borderLeftWidth ?? '0px'),
          borderTopWidth: String(element.__borderTopWidth ?? '0px'),
        };
      },
    },
    createTreeWalker(root: Record<string, any>) {
      const nodes = Array.isArray(root.__textNodes) ? root.__textNodes : [];
      let index = 0;
      return {
        nextNode: () => nodes[index++] ?? null,
      };
    },
    getElementById(id: string) {
      return nodesById.get(id) ?? null;
    },
  };
  const element: Record<string, any> = {
    nodeType: 1,
    tagName: 'INPUT',
    type: attributes.type || 'text',
    value: eventName === 'blur' ? '' : value,
    selectionStart: 0,
    selectionEnd: 0,
    checked: false,
    isContentEditable: false,
    innerText: '',
    textContent: '',
    parentElement: null,
    ownerDocument,
    getAttribute(name: string) {
      return attributes[name] ?? '';
    },
  };
  const form: Record<string, any> = {
    nodeType: 1,
    tagName: 'FORM',
    elements: [],
    ownerDocument,
    getAttribute: () => '',
    closest: () => null,
  };
  const labelElement = (text: string, id = ''): Record<string, any> => {
    const label = {
      nodeType: 1,
      tagName: 'LABEL',
      ownerDocument,
      __textNodes: [{ nodeValue: text }],
      getAttribute: () => '',
    };
    if (id) nodesById.set(id, label);
    return label;
  };
  element.labels = options.associatedLabelText === undefined
    ? []
    : [labelElement(options.associatedLabelText)];
  if (options.labelledByText !== undefined) {
    const id = attributes['aria-labelledby'] || 'field-label';
    const label = labelElement(options.labelledByText, options.labelledByInShadow ? '' : id);
    const shadowRoot = {
      host: null,
      getElementById: (candidate: string) => candidate === id ? label : null,
    };
    element.getRootNode = () => options.labelledByInShadow ? shadowRoot : ownerDocument;
  } else {
    element.getRootNode = () => ownerDocument;
  }
  documentNodes.push(element);
  const document = {
    documentElement: {},
    body: {},
    addEventListener(name: string, listener: (event: Record<string, unknown>) => void) {
      const current = listeners.get(name) ?? [];
      current.push(listener);
      listeners.set(name, current);
    },
    getElementsByTagName: () => [element],
    createTreeWalker: ownerDocument.createTreeWalker,
    getElementById: ownerDocument.getElementById,
  };
  const windowValue: Record<string, unknown> = {};
  windowValue.parent = windowValue;
  const emitted: string[] = [];
  runInNewContext(recorderScript(), {
    document,
    window: windowValue,
    location: { href: 'https://example.com/login' },
    CSS: { escape: (raw: string) => raw },
    setTimeout,
    clearTimeout,
    [RECORDER_BINDING]: (payload: string) => emitted.push(payload),
  });
  const listener = listeners.get(eventName)?.[0];
  expect(listener).toBeTypeOf('function');
  if (eventName === 'blur' && options.priorBeforeInput !== false) {
    const keyDown = listeners.get('keydown')?.[0];
    const beforeInput = listeners.get('beforeinput')?.[0];
    const input = listeners.get('input')?.[0];
    expect(keyDown).toBeTypeOf('function');
    expect(beforeInput).toBeTypeOf('function');
    expect(input).toBeTypeOf('function');
    for (const character of Array.from(value)) {
      keyDown?.({
        target: element,
        isTrusted: true,
        key: character,
        code: 'KeyA',
        keyCode: character.toUpperCase().charCodeAt(0),
        isComposing: false,
        metaKey: false,
        ctrlKey: false,
        altKey: false,
        shiftKey: false,
      });
      beforeInput?.({
        target: element,
        isTrusted: true,
        inputType: 'insertText',
        data: character,
        isComposing: false,
      });
      const start = element.selectionStart;
      const end = element.selectionEnd;
      element.value = element.value.slice(0, start) + character + element.value.slice(end);
      element.selectionStart = start + character.length;
      element.selectionEnd = element.selectionStart;
      input?.({
        target: element,
        isTrusted: true,
        inputType: 'insertText',
        data: character,
        isComposing: false,
      });
    }
  } else if (eventName === 'blur') {
    element.value = value;
  }
  listener?.({
    target: eventName === 'submit' ? form : element,
    isTrusted: true,
    submitter: null,
  });
  const actions = emitted
    .map((payload) => JSON.parse(payload) as Record<string, any>)
    .filter((payload) => !String(payload.type || '').startsWith('causal-'));
  const expected = options.expectedEmissions ?? (
    eventName === 'blur' && options.priorBeforeInput !== false
      ? Array.from(value).length
      : 1
  );
  expect(actions).toHaveLength(expected);
  return actions.at(-1) ?? null;
}

function injectedRecorderHarness(options: {
  initiallyActive?: boolean;
  officialSelectorSource?: string;
  recordingSchemaVersion?: 10 | 11;
} = {}) {
  const listeners = new Map<string, Array<(event: Record<string, any>) => void>>();
  const nodes: Array<Record<string, any>> = [];
  const nodesById = new Map<string, Record<string, any>>();
  const document: Record<string, any> = {
    nodeType: 9,
    documentElement: null,
    body: null,
    defaultView: {
      NodeFilter: { SHOW_TEXT: 4 },
      getComputedStyle(element: Record<string, any>) {
        return {
          borderLeftWidth: String(element.__borderLeftWidth ?? '0px'),
          borderTopWidth: String(element.__borderTopWidth ?? '0px'),
        };
      },
    },
    addEventListener(name: string, listener: (event: Record<string, any>) => void) {
      const current = listeners.get(name) ?? [];
      current.push(listener);
      listeners.set(name, current);
    },
    getElementsByTagName(tag: string) {
      return nodes.filter((node) => String(node.tagName || '').toLowerCase() === tag);
    },
    querySelectorAll(selector: string) {
      if (selector.startsWith('#')) {
        const node = nodesById.get(selector.slice(1));
        return node ? [node] : [];
      }
      return nodes;
    },
    getElementById(id: string) {
      return nodesById.get(id) ?? null;
    },
    createTreeWalker(root: Record<string, any>) {
      const textNodes = Array.isArray(root.__textNodes) ? root.__textNodes : [];
      let index = 0;
      return { nextNode: () => textNodes[index++] ?? null };
    },
  };
  const interactive = new Set([
    'A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'LABEL', 'SUMMARY',
  ]);
  const makeElement = (
    tagName: string,
    attributes: Record<string, string> = {},
  ): Record<string, any> => {
    const element: Record<string, any> = {
      nodeType: 1,
      tagName: tagName.toUpperCase(),
      nodeName: tagName.toUpperCase(),
      type: attributes.type || (tagName.toUpperCase() === 'BUTTON' ? 'submit' : 'text'),
      value: '',
      selectionStart: 0,
      selectionEnd: 0,
      checked: false,
      isContentEditable: attributes.contenteditable === 'true',
      innerText: '',
      parentElement: null,
      children: [],
      labels: [],
      form: null,
      ownerDocument: document,
      __textNodes: [],
      getAttribute(name: string) {
        return attributes[name] ?? '';
      },
      getRootNode() {
        return document;
      },
      getBoundingClientRect() {
        return { left: 0, top: 0, width: 100, height: 100 };
      },
      closest(selector: string) {
        let current: Record<string, any> | null = element;
        while (current) {
          if (selector === 'form' && current.tagName === 'FORM') return current;
          if (
            selector !== 'form'
            && (
              interactive.has(current.tagName)
              || current.isContentEditable
              || Boolean(current.getAttribute?.('role'))
              || Boolean(current.getAttribute?.('onclick'))
            )
          ) return current;
          current = current.parentElement;
        }
        return null;
      },
    };
    nodes.push(element);
    if (attributes.id) nodesById.set(attributes.id, element);
    return element;
  };
  const connect = (parent: Record<string, any>, child: Record<string, any>) => {
    parent.children.push(child);
    child.parentElement = parent;
    if (parent.tagName === 'FORM') child.form = parent;
  };
  document.documentElement = makeElement('html');
  document.body = makeElement('body');
  connect(document.documentElement, document.body);

  const emitted: Array<Record<string, any>> = [];
  const windowValue: Record<string, unknown> = {};
  windowValue.parent = windowValue;
  const sandbox: Record<string, any> = {
    document,
    window: windowValue,
    location: { href: 'https://example.com/recording' },
    CSS: { escape: (raw: string) => raw },
    setTimeout,
    clearTimeout,
    scrollX: 0,
    scrollY: 0,
    [RECORDER_BINDING]: (payload: string) => emitted.push(JSON.parse(payload)),
  };
  runInNewContext(recorderScript({
    initiallyActive: options.initiallyActive,
    recordingSchemaVersion: options.recordingSchemaVersion,
    officialSelectorSource: options.officialSelectorSource,
  }), sandbox);
  const dispatch = (name: string, event: Record<string, any>) => {
    for (const listener of listeners.get(name) ?? []) listener(event);
  };
  const trustedKey = (
    target: Record<string, any>,
    key = 'a',
    code = 'KeyA',
  ) => dispatch('keydown', {
    target,
    isTrusted: true,
    key,
    code,
    keyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0,
    isComposing: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
  });
  const trustedPointer = (target: Record<string, any>) => dispatch('pointerdown', {
    target,
    isTrusted: true,
    isPrimary: true,
    button: 0,
    clientX: 20,
    clientY: 30,
    screenX: 120,
    screenY: 230,
  });
  const trustedText = (target: Record<string, any>, text: string) => {
    for (const character of Array.from(text)) {
      trustedKey(target, character, 'KeyA');
      dispatch('beforeinput', {
        target,
        isTrusted: true,
        inputType: 'insertText',
        data: character,
        isComposing: false,
      });
      const editingHost = target.isContentEditable && target.parentElement?.isContentEditable
        ? target.parentElement
        : target;
      if (editingHost.isContentEditable) {
        const current = editingHost.__textNodes
          .map((node: { nodeValue?: string }) => String(node.nodeValue || ''))
          .join('');
        editingHost.__textNodes = [{ nodeValue: current + character }];
        editingHost.innerText = current + character;
      } else {
        const start = Number.isSafeInteger(editingHost.selectionStart)
          ? editingHost.selectionStart
          : editingHost.value.length;
        const end = Number.isSafeInteger(editingHost.selectionEnd)
          ? editingHost.selectionEnd
          : start;
        editingHost.value = editingHost.value.slice(0, start)
          + character
          + editingHost.value.slice(end);
        editingHost.selectionStart = start + character.length;
        editingHost.selectionEnd = editingHost.selectionStart;
      }
      dispatch('input', {
        target,
        isTrusted: true,
        inputType: 'insertText',
        data: character,
        isComposing: false,
      });
    }
  };
  return {
    document,
    emitted,
    makeElement,
    connect,
    dispatch,
    trustedKey,
    trustedPointer,
    trustedText,
    actions: () => emitted.filter(
      (payload) => !String(payload.type || '').startsWith('causal-'),
    ),
    control: () => sandbox[RECORDER_CONTROL] as {
      activate(): void;
      flush(): number;
      deactivate(): number;
      isActive(): boolean;
      selectorFor(element: Record<string, unknown>): string;
    },
    stash: () => sandbox[RECORDER_TARGET_STASH] as Map<number, unknown>,
    globalKeys: () => Object.keys(sandbox),
    setDocumentScroll: (x: number, y: number) => {
      sandbox.scrollX = x;
      sandbox.scrollY = y;
    },
    windowScrollTarget: () => windowValue,
  };
}

describe('classifyFieldTier', () => {
  it('密码框判为 secret 元数据', () => {
    expect(classifyFieldTier(probe({ type: 'password' }))).toBe('secret');
    expect(classifyFieldTier(probe({ type: 'PASSWORD' }))).toBe('secret');
    expect(classifyFieldTier(probe({ autocomplete: 'current-password' }))).toBe('secret');
    expect(classifyFieldTier(probe({ autocomplete: 'new-password' }))).toBe('secret');
    // 有些站点密码框不写 type=password，只能靠命名兜住
    expect(classifyFieldTier(probe({ name: 'loginPwd' }))).toBe('secret');
    expect(classifyFieldTier(probe({ placeholder: '请输入密码' }))).toBe('secret');
    expect(classifyFieldTier(probe({ ariaLabel: '口令' }))).toBe('secret');
    expect(classifyFieldTier(probe({ labelText: '登录密码' }))).toBe('secret');
  });

  it('验证码判为 handoff 元数据', () => {
    expect(classifyFieldTier(probe({ autocomplete: 'one-time-code' }))).toBe('handoff');
    expect(classifyFieldTier(probe({ name: 'smsCode' }))).toBe('handoff');
    expect(classifyFieldTier(probe({ id: 'captcha' }))).toBe('handoff');
    expect(classifyFieldTier(probe({ placeholder: '短信验证码' }))).toBe('handoff');
    expect(classifyFieldTier(probe({ ariaLabel: '图形校验码' }))).toBe('handoff');
    expect(classifyFieldTier(probe({ labelText: '短信验证码' }))).toBe('handoff');
  });

  it('账号类判为 identifier —— 可记录并参数化', () => {
    expect(classifyFieldTier(probe({ autocomplete: 'username' }))).toBe('identifier');
    expect(classifyFieldTier(probe({ autocomplete: 'email' }))).toBe('identifier');
    expect(classifyFieldTier(probe({ autocomplete: 'tel' }))).toBe('identifier');
    expect(classifyFieldTier(probe({ name: 'staffNo' }))).toBe('identifier');
    expect(classifyFieldTier(probe({ placeholder: '请输入工号' }))).toBe('identifier');
    expect(classifyFieldTier(probe({ ariaLabel: '手机号' }))).toBe('identifier');
  });

  it('普通输入判为 plain', () => {
    expect(classifyFieldTier(probe({ name: 'keyword' }))).toBe('plain');
    expect(classifyFieldTier(probe({ placeholder: '搜索榜单内容' }))).toBe('plain');
  });

  it('敏感判定优先于账号判定', () => {
    // 「密码」和「账号」同时命中时必须按更严的那档走，否则密码会被当账号记录下来
    expect(classifyFieldTier(probe({ name: 'accountPassword' }))).toBe('secret');
    expect(classifyFieldTier(probe({ placeholder: '账号密码' }))).toBe('secret');
    expect(classifyFieldTier(probe({ name: 'userOtp' }))).toBe('handoff');
  });

  it('camelCase 与各种分隔符都能拆出词来', () => {
    // 这是上一版的真实漏判：`loginPwd` 小写成 `loginpwd` 后 `pwd` 前面是字母，
    // 词边界不成立 → 密码框被判成 plain → 密码进轨迹。
    expect(classifyFieldTier(probe({ name: 'loginPwd' }))).toBe('secret');
    expect(classifyFieldTier(probe({ name: 'userOtp' }))).toBe('handoff');
    expect(classifyFieldTier(probe({ name: 'login_pwd' }))).toBe('secret');
    expect(classifyFieldTier(probe({ id: 'sms-code' }))).toBe('handoff');
    expect(classifyFieldTier(probe({ name: 'staff.no' }))).toBe('identifier');
  });

  it('不把碰巧含子串的词误判', () => {
    // `hotelName` 里有 tel、`captchaeous` 之类的粘连词不该命中
    expect(classifyFieldTier(probe({ name: 'hotelName' }))).toBe('plain');
    expect(classifyFieldTier(probe({ name: 'passwordless' }))).toBe('plain');
    expect(classifyFieldTier(probe({ name: 'accountant' }))).toBe('plain');
  });
});

describe('recorderScript', () => {
  it('自包含且不引用宿主变量', () => {
    const source = recorderScript();
    expect(source.startsWith('(() => {')).toBe(true);
    expect(source).toContain(RECORDER_BINDING);
    expect(source).toContain(RECORDER_TARGET_STASH);
  });

  it('生成的脚本语法合法', () => {
    // 脚本是拼出来的字符串，没有编译器把关。任何拼接错误都要在这里暴露，
    // 而不是等注入到用户正在操作的页面上才炸。
    expect(() => new Function(recorderScript())).not.toThrow();
  });

  it('同步使用 Playwright InjectedScript 生成目标 selector，不回退启发式 CSS', () => {
    const harness = injectedRecorderHarness({
      officialSelectorSource: `
        module.exports = {
          InjectedScript: () => class {
            generateSelectorSimple(element) {
              return 'internal:testid=[data-testid='
                + JSON.stringify(element.getAttribute('data-testid')) + 's]';
            }
          },
        };
      `,
    });
    const button = harness.makeElement('button', {
      id: 'brittle-runtime-id',
      'data-testid': 'save-ticket',
    });
    harness.connect(harness.document.body, button);

    harness.dispatch('click', {
      target: button,
      isTrusted: true,
      detail: 1,
      button: 0,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    });

    expect(harness.actions()).toHaveLength(1);
    expect(harness.actions()[0]).toMatchObject({
      selectorSource: 'playwright',
      recordedSelector: 'internal:testid=[data-testid="save-ticket"s]',
      recordedDragSelector: '',
    });
    expect(harness.control().selectorFor(button))
      .toBe('internal:testid=[data-testid="save-ticket"s]');
  });

  it('嵌进脚本的分级函数与被单测的那份行为一致', () => {
    // `classifyFieldTier.toString()` 嵌入的是**转译后**的函数体。若它意外引用了
    // 模块作用域的标识符（helper、常量、TS 降级产生的临时变量），在页面里就会
    // ReferenceError —— 而那条路径是「判定失败按 secret 兜底」，密码不会泄漏，
    // 但所有账号字段都会被误判成 secret，参数化能力静默失效。这条测试把
    // 「嵌入的那份能独立运行」钉死。
    const source = recorderScript();
    const match = /const classifyFieldTier = ([\s\S]*?);\n {2}const BINDING_NAME/.exec(source);
    expect(match).not.toBeNull();
    const embedded = new Function(`return (${match![1]});`)() as typeof classifyFieldTier;

    const cases = [
      probe({ type: 'password' }),
      probe({ autocomplete: 'one-time-code' }),
      probe({ name: 'loginPwd' }),
      probe({ name: 'staffNo' }),
      probe({ name: 'hotelName' }),
      probe({ placeholder: '短信验证码' }),
    ];
    for (const item of cases) {
      expect(embedded(item)).toBe(classifyFieldTier(item));
    }
  });

  it('真实注入链保留密码与验证码的完整可回放证据', () => {
    const password = captureInjectedInput({ name: 'loginPwd' }, 'S3ntinel-Password!');
    expect(password).toMatchObject({
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      tier: 'secret',
      value: 'S3ntinel-Password!',
      url: 'https://example.com/login',
      hint: 'input loginPwd',
      target: expect.objectContaining({
        tag: 'input',
        name: 'loginPwd',
        inputType: 'text',
      }),
    });
    expect(password.provenance).toEqual({
      schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
      source: 'document-world',
      capturePhase: 'event-callback',
      browserTrusted: true,
      targetEvidence: 'synchronous',
      nativeInput: 'unverified',
    });
    expect(parseRecorderEvent(JSON.stringify(password))).toMatchObject({
      tier: 'secret',
      value: 'S3ntinel-Password!',
      url: 'https://example.com/login',
      target: expect.objectContaining({ name: 'loginPwd' }),
    });

    const otp = captureInjectedInput({ id: 'smsCode' }, '963852');
    expect(otp).toMatchObject({
      tier: 'handoff',
      value: '963852',
      url: 'https://example.com/login',
      target: expect.objectContaining({ id: 'smsCode' }),
    });
    expect(parseRecorderEvent(JSON.stringify(otp))).toMatchObject({
      tier: 'handoff',
      value: '963852',
      target: expect.objectContaining({ id: 'smsCode' }),
    });
  });

  it('真实注入链识别外部 label 与 ShadowRoot aria-labelledby', () => {
    const external = captureInjectedInput(
      { type: 'text', name: 'field' },
      'SENTINEL-external-label',
      'blur',
      { associatedLabelText: '登录密码' },
    );
    expect(external).toMatchObject({
      tier: 'secret',
      value: 'SENTINEL-external-label',
      target: expect.objectContaining({ name: 'field' }),
    });

    const otp = captureInjectedInput(
      { type: 'text', name: 'field' },
      '246810',
      'blur',
      { associatedLabelText: '短信验证码' },
    );
    expect(otp).toMatchObject({
      tier: 'handoff',
      value: '246810',
      target: expect.objectContaining({ name: 'field' }),
    });

    const shadow = captureInjectedInput(
      { type: 'text', name: 'field', 'aria-labelledby': 'shadow-label' },
      'SENTINEL-shadow-label',
      'blur',
      { labelledByText: '安全口令', labelledByInShadow: true },
    );
    expect(shadow).toMatchObject({
      tier: 'secret',
      value: 'SENTINEL-shadow-label',
      target: expect.objectContaining({ name: 'field' }),
    });
  });

  it('超长属性与标签完整参与分级，不因固定前缀截断而漏判', () => {
    const oversizedAttribute = captureInjectedInput(
      { type: 'text', name: `${'x'.repeat(4_097)}Password` },
      'SENTINEL-oversized-attribute',
    );
    expect(oversizedAttribute).toMatchObject({
      tier: 'secret',
      value: 'SENTINEL-oversized-attribute',
    });

    const oversizedLabel = captureInjectedInput(
      { type: 'text', name: 'field' },
      'SENTINEL-oversized-label',
      'blur',
      { associatedLabelText: `${'x'.repeat(1_025)} Password` },
    );
    expect(oversizedLabel).toMatchObject({
      tier: 'secret',
      value: 'SENTINEL-oversized-label',
    });

    // Explicit native evidence remains authoritative even when every optional label is broken.
    const explicitPassword = captureInjectedInput(
      { type: 'password', 'aria-labelledby': 'missing-label' },
      'SENTINEL-password',
    );
    expect(explicitPassword).toMatchObject({
      tier: 'secret',
      value: 'SENTINEL-password',
      target: expect.objectContaining({ inputType: 'password' }),
    });
  });

  it('同步目标证据在事件回调内生成，不依赖导航后的旧 DOM', () => {
    const event = captureInjectedInput({
      name: 'accountName',
      id: 'loginAccount',
      role: 'textbox',
      type: 'text',
      'data-testid': 'login-account',
    }, 'A-123');
    expect(event.tier).toBe('identifier');
    expect(event.target).toMatchObject({
      id: 'loginAccount',
      name: 'accountName',
      role: 'textbox',
      inputType: 'text',
      testId: 'login-account',
      testIdAttribute: 'data-testid',
      cssPath: '#loginAccount',
      framePath: [],
    });
    expect(event.provenance.targetEvidence).toBe('synchronous');
  });

  it('只接受受信事件，合成事件一律忽略', () => {
    // 页面可以 dispatchEvent 造一个 click。isTrusted 是浏览器给的、页面改不了的
    // 标记——没有这道门，页面就能往轨迹里塞「用户点了同意」。
    const source = recorderScript();
    const guards = source.match(/!event\.isTrusted/g) ?? [];
    expect(guards.length).toBeGreaterThanOrEqual(4);
  });

  it('只把真正的文本输入控件当输入源', () => {
    // 实测 bug：早先用「元素有没有 value 属性」判定，而 HTMLButtonElement 也有
    // value —— 于是点完按钮一失焦就多发一条假的 input 事件，轨迹里凭空多出
    // 一步「在某按钮里输入了空字符串」。
    const source = recorderScript();
    expect(source).toContain('NON_TEXT_INPUT_TYPES');
    expect(source).toContain('isTextEntry');
    for (const type of ['button', 'submit', 'reset', 'image', 'file', 'hidden']) {
      expect(source).toContain(`'${type}'`);
    }
    // 判定必须落到 isTextEntry 上，不能再退回裸的 value 存在性检查
    expect(source).not.toContain("!('value' in element)");
  });

  it('文件框 change 形成 v5 upload，一次保留多文件 wrapper 且不重复记 click', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', {
      id: 'hidden-attachments',
      type: 'file',
      accept: '.pdf,image/*',
      hidden: '',
    });
    field.multiple = true;
    const first = { name: '合同.pdf' };
    const second = { name: '现场.png' };
    field.files = [first, second];
    harness.connect(harness.document.body, field);

    harness.dispatch('click', {
      target: field,
      isTrusted: true,
      button: 0,
      detail: 1,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    });
    harness.dispatch('input', { target: field, isTrusted: true });
    harness.dispatch('change', { target: field, isTrusted: true });

    expect(harness.actions()).toEqual([
      expect.objectContaining({
        schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
        type: 'upload',
        uploadMode: 'handoff',
        paths: [],
        fileCount: 2,
        multiple: true,
        accept: '.pdf,image/*',
        target: expect.objectContaining({
          id: 'hidden-attachments',
          inputType: 'file',
        }),
      }),
    ]);
    const entry = harness.stash().get(harness.actions()[0].seq) as {
      files?: unknown[];
    };
    // Target evidence is frozen synchronously in the emitted event. Only the
    // opaque File wrappers need a short-lived renderer-side handle.
    expect(entry.files).toEqual([first, second]);
    // The snapshot must not follow a later live FileList replacement.
    field.files = [{ name: 'later.txt' }];
    expect(entry.files).toEqual([first, second]);
  });

  it('大批量文件与超长 accept 证据完整进入同一个 upload 步骤', () => {
    const harness = injectedRecorderHarness();
    const accept = `.type-${'x'.repeat(100_000)}`;
    const field = harness.makeElement('input', {
      id: 'directory-upload',
      type: 'file',
      accept,
    });
    const files = Array.from({ length: 1_000 }, (_, index) => ({ name: `文件-${index}.bin` }));
    field.multiple = true;
    field.files = files;
    harness.connect(harness.document.body, field);

    harness.dispatch('change', { target: field, isTrusted: true });

    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'upload',
        fileCount: files.length,
        accept,
      }),
    ]);
    const entry = harness.stash().get(harness.actions()[0].seq) as { files?: unknown[] };
    expect(entry.files).toEqual(files);
  });

  it('空文件选择与 fresh chooser cancel 记录 clear，已有选择的 cancel 不伪造上传', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', { id: 'attachments', type: 'file' });
    field.files = [];
    field.multiple = false;
    harness.connect(harness.document.body, field);

    harness.dispatch('input', { target: field, isTrusted: true });
    harness.dispatch('change', { target: field, isTrusted: true });
    harness.dispatch('cancel', { target: field, isTrusted: true });
    field.files = [{ name: 'kept.pdf' }];
    harness.dispatch('cancel', { target: field, isTrusted: true });
    harness.dispatch('change', { target: field, isTrusted: false });

    expect(harness.actions()).toHaveLength(1);
    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'upload', uploadMode: 'clear', paths: [], fileCount: 0,
      }),
    ]);
  });

  it('注入脚本的注释里不能出现反引号', () => {
    // 脚本体在模板字符串里，注释中的反引号会直接终止模板 —— 实测踩过，
    // 表现是 esbuild 报 "Expected ; but found" 而 tsc 不一定拦得住。
    const source = recorderScript();
    for (const line of source.split('\n')) {
      const comment = line.indexOf('//');
      if (comment < 0) continue;
      expect(line.slice(comment)).not.toContain('`');
    }
  });

  it('hint 绝不读取输入控件的 value', () => {
    // 这是审查查出的 P0：hintOf 原本写的是 innerText || value。密码框没有
    // innerText、也常常没有 aria-label，于是明文密码进了 hint —— 而后续各层
    // 只清 value 不清 hint，密码就一路落盘到轨迹文件。
    const source = recorderScript();
    const hintBody = source.slice(source.indexOf('const hintOf'), source.indexOf('const targetOf'));
    expect(hintBody).toContain('isTextEntry(element)');
    expect(hintBody).not.toContain('element.value');
  });

  it('点击往上找可交互祖先，不记 span', () => {
    // 用户点的是按钮，DOM 给的常是按钮里的 span/svg。直接记 event.target 会让
    // 轨迹出现「点了一个 span」——既对不上快照行，也判断不出它是提交按钮。
    const source = recorderScript();
    expect(source).toContain('INTERACTIVE');
    expect(source).toContain('closest(INTERACTIVE)');
    expect(source).toContain('const element = pointerTargetOf(event)');
    expect(source).toContain("send('click', element");
  });

  it('滚动基线在监听建立时就取，且区分内层容器', () => {
    // 两个实测坑：scroll 是滚完之后才派发的，在第一次事件里取基线会让单次
    // 滚动的位移恒为 0；以及内层容器滚的是自己的 scrollTop，读 window 得到 0。
    const source = recorderScript();
    expect(source).toContain('documentScrollBase = { x: globalThis.scrollX');
    expect(source).toContain('target.scrollTop');
    expect(source).toContain('scrollBases');
    expect(source).toContain("addEventListener('wheel', primeFromTrustedEvent");
    expect(source).toContain("addEventListener('pointerdown', primeFromTrustedEvent");
    expect(source).toContain("addEventListener('touchstart', primeFromTrustedEvent");
    expect(source).toContain('SCROLL_KEYS.has(key)');
  });

  it('输入法组合态的按键不记', () => {
    // 中文选词按的 Enter 是打字，不是工作流动作
    const source = recorderScript();
    expect(source).toContain('event.isComposing');
    expect(source).toContain('event.keyCode === 229');
  });

  it('同值不重复提交，contenteditable 也算输入源', () => {
    const source = recorderScript();
    expect(source).toContain('lastCommitted');
    expect(source).toContain('element.isContentEditable');
  });

  it('Enter 保留 textbox 最终值及 textbox/form/button 的键盘语义，不重复 derived click', () => {
    const harness = injectedRecorderHarness();
    const form = harness.makeElement('form', { id: 'search-form' });
    form.elements = [];
    const textbox = harness.makeElement('input', { id: 'search-textbox', type: 'text' });
    const submitter = harness.makeElement('button', { id: 'search-submit', type: 'submit' });
    const ordinaryButton = harness.makeElement('button', {
      id: 'keyboard-button',
      type: 'button',
    });
    harness.connect(harness.document.body, form);
    harness.connect(form, textbox);
    harness.connect(form, submitter);
    harness.connect(harness.document.body, ordinaryButton);
    form.elements.push(textbox, submitter);

    harness.trustedText(textbox, '最终查询词');
    harness.trustedKey(textbox, 'Enter', 'Enter');
    // Chromium dispatches this trusted zero-detail click for implicit form submission.
    harness.dispatch('click', {
      target: submitter,
      isTrusted: true,
      button: 0,
      detail: 0,
    });
    harness.trustedKey(form, 'Enter', 'Enter');
    harness.trustedKey(ordinaryButton, 'Enter', 'Enter');
    harness.dispatch('click', {
      target: ordinaryButton,
      isTrusted: true,
      button: 0,
      detail: 0,
    });
    harness.trustedKey(ordinaryButton, ' ', 'Space');
    harness.dispatch('click', {
      target: ordinaryButton,
      isTrusted: true,
      button: 0,
      detail: 0,
    });

    const actions = harness.actions();
    expect(actions.filter((event) => event.type === 'input').at(-1)).toMatchObject({
      type: 'input',
      value: '最终查询词',
      target: { id: 'search-textbox' },
    });
    expect(actions.filter((event) => event.type === 'key')).toMatchObject([
      {
        type: 'key',
        key: 'Enter',
        target: { id: 'search-textbox' },
      },
      {
        type: 'key',
        key: 'Enter',
        target: { id: 'search-form' },
      },
      {
        type: 'key',
        key: 'Enter',
        target: { id: 'keyboard-button' },
      },
      {
        type: 'key',
        key: 'Space',
        target: { id: 'keyboard-button' },
      },
    ]);
    expect(actions.filter((event) => event.type === 'click')).toEqual([]);
  });

  it('modifier-only 与 Cmd/Ctrl+V 不生成 press，快捷键 A/C/X/Z 及最终粘贴 input 保留', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', { id: 'shortcut-input', type: 'text' });
    harness.connect(harness.document.body, field);
    const keydown = (
      key: string,
      modifiers: Partial<{
        altKey: boolean;
        ctrlKey: boolean;
        metaKey: boolean;
        shiftKey: boolean;
      }> = {},
    ) => harness.dispatch('keydown', {
      target: field,
      isTrusted: true,
      key,
      code: key.length === 1 ? `Key${key.toUpperCase()}` : key,
      keyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0,
      isComposing: false,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
      ...modifiers,
    });

    keydown('Control', { ctrlKey: true });
    keydown('Meta', { metaKey: true });
    keydown('Alt', { altKey: true });
    keydown('Shift', { shiftKey: true });
    keydown('a', { ctrlKey: true });
    keydown('c', { metaKey: true });
    keydown('x', { ctrlKey: true });
    keydown('z', { metaKey: true });
    keydown('v', { ctrlKey: true });
    keydown('V', { metaKey: true, shiftKey: true });

    field.value = '来自系统剪贴板的最终文本';
    harness.dispatch('input', {
      target: field,
      isTrusted: true,
      inputType: 'insertFromPaste',
      data: null,
      isComposing: false,
    });
    harness.dispatch('blur', { target: field, isTrusted: true });

    expect(
      harness.actions()
        .filter((event) => event.type === 'key')
        .map((event) => event.key),
    ).toEqual(['Ctrl+a', 'Meta+c', 'Ctrl+x', 'Meta+z']);
    expect(harness.actions().filter((event) => event.type === 'input')).toEqual([
      expect.objectContaining({
        value: '来自系统剪贴板的最终文本',
        target: expect.objectContaining({ id: 'shortcut-input' }),
      }),
    ]);
    const serialized = JSON.stringify(harness.actions());
    for (const forbidden of [
      'Control+Control',
      'Meta+Meta',
      'Alt+Alt',
      'Shift+Shift',
      'Ctrl+v',
      'Meta+V',
    ]) {
      expect(serialized).not.toContain(forbidden);
    }
  });

  it('textarea/contenteditable 的 Enter 由最终 input 表达，不提前提交旧值或重复 press', () => {
    const harness = injectedRecorderHarness();
    const textarea = harness.makeElement('textarea', { id: 'multiline' });
    harness.connect(harness.document.body, textarea);
    harness.trustedText(textarea, '第一行');
    harness.trustedKey(textarea, 'Enter', 'Enter');
    textarea.value += '\n';
    harness.dispatch('input', {
      target: textarea,
      isTrusted: true,
      inputType: 'insertLineBreak',
      data: null,
      isComposing: false,
    });
    harness.dispatch('blur', { target: textarea, isTrusted: true });

    const inputs = harness.actions().filter((event) => event.type === 'input');
    expect(inputs.at(-1)).toMatchObject({
      type: 'input',
      value: '第一行\n',
      target: expect.objectContaining({ id: 'multiline' }),
    });
    expect(harness.actions().filter((event) => event.type === 'key')).toEqual([]);
  });

  it('checkbox/radio 记勾选状态而不是占位的 "on"', () => {
    // value 属性对 checkbox/radio 恒为 "on"（HTML 默认值），记它毫无意义。
    const source = recorderScript();
    expect(source).toContain("inputType === 'checkbox' || inputType === 'radio'");
    expect(source).toContain("element.checked ? 'checked' : 'unchecked'");
  });

  it('内层滚动的基线取当前位置，首条不会把绝对位置当增量', () => {
    // 基线从 0 起算的话，从 500 滚到 600 会报 600 而不是 100。
    const source = recorderScript();
    expect(source).toContain('scrollBases.set(element, { x: position.x, y: position.y })');
  });

  it('点击能穿过 Shadow DOM 边界找到可交互祖先', () => {
    // closest 不穿 shadow 边界，Web Component 里的按钮点击会记成一个无名宿主
    // 元素，编译期对不上任何快照行。
    const source = recorderScript();
    expect(source).toContain('getRootNode');
    expect(source).toContain('root.host');
  });

  it('composed event 使用 Shadow DOM 内真实目标而不是被 retarget 的 host', () => {
    const harness = injectedRecorderHarness();
    const host = harness.makeElement('div', { id: 'upload-widget' });
    const button = harness.makeElement('button', { id: 'shadow-submit', type: 'button' });
    harness.connect(harness.document.body, host);
    harness.dispatch('click', {
      target: host,
      composedPath: () => [button, host, harness.document],
      isTrusted: true,
      button: 0,
      detail: 1,
    });
    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'click',
        target: expect.objectContaining({ id: 'shadow-submit' }),
      }),
    ]);
  });

  it('每次 trusted input 在原 DOM task 内持久化，blur 仅去重清理', () => {
    const source = recorderScript();
    expect(source).toContain("addEventListener('beforeinput'");
    expect(source).toContain("addEventListener('change'");
    expect(source).toContain("addEventListener('blur'");
    expect(source).toContain("addEventListener('input'");
    expect(source).toContain('trackDirty(element');
    expect(source).toContain('Persist every trusted input synchronously');
  });

  it('trusted input 在 blur 或页面生命周期前立即发出每个最新值', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', { id: 'navigate-on-input', type: 'text' });
    harness.connect(harness.document.body, field);

    field.value = 'a';
    harness.dispatch('input', {
      target: field,
      isTrusted: true,
      inputType: 'insertText',
      data: 'a',
    });
    expect(harness.actions()).toMatchObject([
      { type: 'input', value: 'a', target: { id: 'navigate-on-input' } },
    ]);

    // This assertion runs before blur/change/deactivate. In a real page the
    // input handler may navigate or close the document immediately after this
    // callback, so the latest value must already be across the binding.
    field.value = 'ab';
    harness.dispatch('input', {
      target: field,
      isTrusted: true,
      inputType: 'insertText',
      data: 'b',
    });
    expect(
      harness.actions().filter((event) => event.type === 'input').map((event) => event.value),
    ).toEqual(['a', 'ab']);

    harness.dispatch('blur', { target: field, isTrusted: true });
    expect(
      harness.actions().filter((event) => event.type === 'input').map((event) => event.value),
    ).toEqual(['a', 'ab']);
  });

  it('空文本控件的真实 click 独立保留，等待编译器在确有 input 时降噪', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', { id: 'empty-textbox', type: 'text' });
    harness.connect(harness.document.body, field);

    harness.dispatch('click', {
      target: field,
      isTrusted: true,
      button: 0,
      detail: 1,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    });

    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'click',
        value: '',
        target: expect.objectContaining({
          id: 'empty-textbox',
          inputType: 'text',
        }),
      }),
    ]);
  });

  it('页面脚本触发的 trusted blur/requestSubmit 没有真实编辑因果时不会入轨迹', () => {
    const blur = captureInjectedInput(
      { type: 'text', name: 'ordinary' },
      'page-assigned-value',
      'blur',
      { priorBeforeInput: false, expectedEmissions: 0 },
    );
    expect(blur).toBeNull();

    const submit = captureInjectedInput(
      {},
      '',
      'submit',
      { expectedEmissions: 0 },
    );
    expect(submit).toBeNull();
  });

  it('trusted input 通吃粘贴、中文 IME、替换与删除，并提交最终 DOM 值', () => {
    const samples = [
      { inputType: 'insertFromPaste', value: '粘贴内容', composing: false },
      { inputType: 'insertCompositionText', value: '中文输入', composing: true },
      { inputType: 'insertReplacementText', value: '替换完成', composing: false },
      { inputType: 'deleteContentBackward', value: '', composing: false },
    ];
    for (const sample of samples) {
      const harness = injectedRecorderHarness();
      const field = harness.makeElement('input', { id: 'general-input' });
      field.value = sample.inputType.startsWith('delete') ? '待删除' : '';
      harness.connect(harness.document.body, field);
      harness.dispatch('beforeinput', {
        target: field,
        isTrusted: true,
        inputType: sample.inputType,
        data: sample.value,
        isComposing: sample.composing,
      });
      field.value = sample.value;
      harness.dispatch('input', {
        target: field,
        isTrusted: true,
        inputType: sample.inputType,
        data: sample.value,
        isComposing: sample.composing,
      });
      harness.dispatch('blur', { target: field, isTrusted: true });
      expect(harness.actions().filter((event) => event.type === 'input')).toEqual([
        expect.objectContaining({
          value: sample.value,
          valueTruncated: false,
          target: expect.objectContaining({ id: 'general-input' }),
        }),
      ]);
    }
  });

  it('contenteditable 按官方 recorder 的 innerText 原样保留换行与空格', () => {
    const harness = injectedRecorderHarness();
    const editor = harness.makeElement('div', {
      id: 'exact-rich-text',
      contenteditable: 'true',
    });
    harness.connect(harness.document.body, editor);
    const exact = '第一行\n  第二行  ';
    editor.innerText = exact;
    editor.__textNodes = [{ nodeValue: '会被证据归一化但不应作为表单值' }];
    harness.dispatch('input', {
      target: editor,
      isTrusted: true,
      inputType: 'insertText',
      data: null,
      isComposing: false,
    });
    editor.innerText = '页面在事件后改写';
    harness.dispatch('blur', { target: editor, isTrusted: true });

    expect(harness.actions().filter((event) => event.type === 'input')).toEqual([
      expect.objectContaining({
        value: exact,
        values: [],
        valueTruncated: false,
        target: expect.objectContaining({
          id: 'exact-rich-text',
          contentEditable: true,
        }),
      }),
    ]);
  });

  it('select-multiple 按 DOM 顺序记录全部 selectedOptions.value', () => {
    const harness = injectedRecorderHarness();
    const select = harness.makeElement('select', {
      id: 'team-members',
      type: 'select-multiple',
    });
    select.multiple = true;
    select.selectedOptions = [
      { value: 'alpha' },
      { value: 'gamma' },
    ];
    select.value = 'alpha';
    harness.connect(harness.document.body, select);
    harness.dispatch('input', { target: select, isTrusted: true });
    harness.dispatch('blur', { target: select, isTrusted: true });

    expect(harness.actions().filter((event) => event.type === 'input')).toEqual([
      expect.objectContaining({
        value: 'alpha',
        values: ['alpha', 'gamma'],
        valueTruncated: false,
        target: expect.objectContaining({
          id: 'team-members',
          tag: 'select',
          inputType: 'select-multiple',
        }),
      }),
    ]);
  });

  it('blur 只提交最后一个 trusted input 观察到的值，不重新读取页面后改写', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', { id: 'stable-final-value' });
    harness.connect(harness.document.body, field);
    harness.trustedText(field, 'human-value');
    field.value = 'page-replaced-after-input';
    harness.dispatch('blur', { target: field, isTrusted: true });
    const inputs = harness.actions().filter((event) => event.type === 'input');
    expect(inputs.at(-1)).toMatchObject({ value: 'human-value' });
    expect(JSON.stringify(harness.actions())).not.toContain('page-replaced-after-input');
  });

  it('stop/pause 不重复已同步持久化且停用后不再采集', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', { id: 'stop-with-focus' });
    harness.connect(harness.document.body, field);
    harness.trustedText(field, '尚未失焦');
    const immediateCount = Array.from('尚未失焦').length;
    expect(harness.actions()).toHaveLength(immediateCount);
    expect(harness.actions().at(-1)).toMatchObject({
      type: 'input',
      value: '尚未失焦',
      target: expect.objectContaining({ id: 'stop-with-focus' }),
    });

    expect(harness.control().deactivate()).toBe(0);
    expect(harness.control().isActive()).toBe(false);
    expect(harness.actions()).toHaveLength(immediateCount);

    field.value = '停用后不采集';
    harness.dispatch('input', { target: field, isTrusted: true });
    harness.dispatch('blur', { target: field, isTrusted: true });
    expect(harness.actions()).toHaveLength(immediateCount);

    harness.control().activate();
    field.value = '恢复后采集';
    harness.dispatch('input', { target: field, isTrusted: true });
    harness.dispatch('blur', { target: field, isTrusted: true });
    expect(harness.actions()).toHaveLength(immediateCount + 1);
    expect(harness.actions().at(-1)).toMatchObject({ value: '恢复后采集' });
  });

  it('document-start dormant 模式不读取或记录，控制全局不可枚举', () => {
    const harness = injectedRecorderHarness({ initiallyActive: false });
    const field = harness.makeElement('input', { id: 'dormant' });
    harness.connect(harness.document.body, field);
    field.value = 'inactive-value';
    harness.dispatch('input', { target: field, isTrusted: true });
    harness.dispatch('blur', { target: field, isTrusted: true });
    expect(harness.actions()).toEqual([]);
    expect(harness.globalKeys()).not.toContain(RECORDER_CONTROL);
    expect(harness.globalKeys()).not.toContain(RECORDER_TARGET_STASH);

    harness.control().activate();
    field.value = 'active-value';
    harness.dispatch('input', { target: field, isTrusted: true });
    harness.dispatch('blur', { target: field, isTrusted: true });
    expect(harness.actions()[0]).toMatchObject({ value: 'active-value' });
  });

  it('同一输入突发的每个 trusted edit task 都重武装同一个 causal token', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('input', { id: 'react-query', type: 'text' });
    harness.connect(harness.document.body, field);

    harness.trustedText(field, 'ab');
    harness.dispatch('blur', { target: field, isTrusted: true });

    const begins = harness.emitted.filter((event) => event.type === 'causal-begin');
    expect(begins.length).toBeGreaterThanOrEqual(2);
    expect(new Set(begins.map((event) => event.seq)).size).toBe(begins.length);
    expect(new Set(begins.map((event) => event.token)).size).toBe(1);
    const input = harness.actions().filter((event) => event.type === 'input').at(-1);
    expect(input).toMatchObject({
      value: 'ab',
      causalToken: begins[0].token,
    });
  });

  it('超长文本完整记录，不截断也不伪造 valueTruncated', () => {
    const harness = injectedRecorderHarness();
    const field = harness.makeElement('textarea', { id: 'long-value' });
    harness.connect(harness.document.body, field);
    const value = '长文本🚀\\n'.repeat(20_000);
    field.value = value;
    harness.dispatch('input', { target: field, isTrusted: true });
    harness.dispatch('blur', { target: field, isTrusted: true });
    const input = harness.actions().find((event) => event.type === 'input');
    expect(input.value).toBe(value);
    expect(input.valueTruncated).toBe(false);
  });

  it('任意数量 trusted 输入都立即持久化且生命周期 flush 不重复', () => {
    const harness = injectedRecorderHarness();
    const count = 1_000;
    for (let index = 0; index < count; index += 1) {
      const field = harness.makeElement('input', { id: `dirty-${index}` });
      harness.connect(harness.document.body, field);
      field.value = `value-${index}`;
      harness.dispatch('input', { target: field, isTrusted: true });
    }
    expect(harness.actions()).toHaveLength(count);
    expect(harness.control().deactivate()).toBe(0);
    expect(harness.actions()).toHaveLength(count);
    // Ordinary inputs must not retain live DOM nodes in the Host handoff stash.
    expect(harness.stash().size).toBe(0);
    expect(harness.actions()[0]).toMatchObject({ value: 'value-0', target: { id: 'dirty-0' } });
    expect(harness.actions().at(-1)).toMatchObject({
      value: `value-${count - 1}`,
      target: { id: `dirty-${count - 1}` },
    });
  });

  it('trusted 点击穿过 composed target 链，普通输入独立按自身 input 记录', () => {
    const harness = injectedRecorderHarness();
    const button = harness.makeElement('button', { id: 'real-button', type: 'button' });
    const child = harness.makeElement('span');
    child.__textNodes = [{ nodeValue: '真实按钮' }];
    const unrelated = harness.makeElement('input', { id: 'unrelated', name: 'unrelated' });
    harness.connect(harness.document.body, button);
    harness.connect(button, child);
    harness.connect(harness.document.body, unrelated);

    harness.trustedPointer(child);
    harness.dispatch('click', { target: child, isTrusted: true, detail: 1 });
    unrelated.value = 'borrowed-pointer-proof';
    harness.dispatch('input', { target: unrelated, isTrusted: true });
    harness.dispatch('blur', { target: unrelated, isTrusted: true });

    expect(harness.actions()).toHaveLength(2);
    expect(harness.actions()[0]).toMatchObject({
      type: 'click',
      target: { id: 'real-button', tag: 'button' },
    });
    expect(harness.actions()[1]).toMatchObject({
      type: 'input',
      value: 'borrowed-pointer-proof',
      target: { id: 'unrelated' },
    });
  });

  it('requestSubmit 即使产生 trusted submit 也不持久化，保留更强的 click/key 触发证据', () => {
    const harness = injectedRecorderHarness();
    const form = harness.makeElement('form', { id: 'form' });
    form.elements = [];
    const submitter = harness.makeElement('button', { id: 'submitter', type: 'submit' });
    harness.connect(harness.document.body, form);
    harness.connect(form, submitter);
    form.elements.push(submitter);

    harness.trustedPointer(submitter);
    harness.dispatch('click', { target: submitter, isTrusted: true, detail: 1 });
    // Page code can call form.requestSubmit(submitter) synchronously inside the real click.
    // There is no browser surface that distinguishes that submit event from default submit;
    // the recorder therefore never persists this redundant, transferable event class.
    harness.dispatch('submit', {
      target: form,
      isTrusted: true,
      submitter,
    });

    expect(harness.actions()).toHaveLength(1);
    expect(harness.actions()[0]).toMatchObject({
      type: 'click',
      target: { id: 'submitter', inputType: 'submit' },
    });
  });

  it('原生可编辑控件只记录最终 input 状态，不再额外记录 click', () => {
    const harness = injectedRecorderHarness();
    const checkbox = harness.makeElement('input', {
      id: 'remember',
      type: 'checkbox',
    });
    const label = harness.makeElement('label', { id: 'remember-label' });
    label.control = checkbox;
    harness.connect(harness.document.body, checkbox);
    harness.connect(harness.document.body, label);

    harness.dispatch('click', { target: label, isTrusted: true, detail: 1 });
    checkbox.checked = true;
    harness.dispatch('input', { target: checkbox, isTrusted: true });
    harness.dispatch('change', { target: checkbox, isTrusted: true });

    expect(harness.actions()).toHaveLength(1);
    expect(harness.actions()[0]).toMatchObject({
      type: 'input',
      value: 'checked',
      target: { id: 'remember' },
    });
  });

  it('多击由 click.detail 精确表达，内部 drag 独立且不读取 DataTransfer', () => {
    const harness = injectedRecorderHarness();
    const source = harness.makeElement('button', { id: 'source', type: 'button' });
    const destination = harness.makeElement('button', { id: 'destination', type: 'button' });
    harness.connect(harness.document.body, source);
    harness.connect(harness.document.body, destination);

    harness.dispatch('click', { target: source, isTrusted: true, detail: 2 });
    harness.dispatch('dragstart', {
      target: source,
      isTrusted: true,
      get dataTransfer() {
        throw new Error('DataTransfer must not be read');
      },
    });
    harness.dispatch('drop', {
      target: destination,
      isTrusted: true,
      get dataTransfer() {
        throw new Error('DataTransfer must not be read');
      },
    });
    harness.dispatch('click', { target: destination, isTrusted: true, detail: 1 });

    expect(harness.actions().map((event) => event.type)).toEqual(['click', 'drag']);
    expect(harness.actions()[0]).toMatchObject({ clickCount: 2 });
    expect(harness.actions()[1]).toMatchObject({
      target: { id: 'source' },
      dragTarget: { id: 'destination' },
      dragSourcePosition: null,
      dragTargetPosition: null,
    });
  });

  it('内部 drag 精确保留 Playwright padding-box 源点与目标点', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const source = harness.makeElement('div', { id: 'source', role: 'button' });
    const destination = harness.makeElement('div', { id: 'destination', role: 'button' });
    source.__borderLeftWidth = '2.5px';
    source.__borderTopWidth = '3px';
    source.getBoundingClientRect = () => ({ left: 10, top: 20, width: 100, height: 80 });
    destination.__borderLeftWidth = '1px';
    destination.__borderTopWidth = '4.25px';
    destination.getBoundingClientRect = () => ({ left: 200, top: 300, width: 120, height: 90 });
    harness.connect(harness.document.body, source);
    harness.connect(harness.document.body, destination);

    harness.dispatch('dragstart', {
      target: source,
      isTrusted: true,
      clientX: 22.75,
      clientY: 38.5,
    });
    harness.dispatch('drop', {
      target: destination,
      isTrusted: true,
      clientX: 231.5,
      clientY: 340.75,
    });

    expect(harness.actions()).toHaveLength(1);
    expect(harness.actions()[0]).toMatchObject({
      type: 'drag',
      target: { id: 'source' },
      dragTarget: { id: 'destination' },
      dragSourcePosition: { x: 10.25, y: 15.5 },
      dragTargetPosition: { x: 30.5, y: 36.5 },
    });
    expect(parseRecorderEvent(JSON.stringify(harness.actions()[0]))).toMatchObject({
      dragSourcePosition: { x: 10.25, y: 15.5 },
      dragTargetPosition: { x: 30.5, y: 36.5 },
    });
    expect(parseRecorderEvent(JSON.stringify({
      ...harness.actions()[0],
      dragSourcePosition: { x: -1, y: 2 },
    }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({
      ...harness.actions()[0],
      type: 'click',
      dragTarget: null,
      recordedDragSelector: '',
      dragSourcePosition: { x: 1, y: 2 },
    }))).toBeNull();
  });

  it('v11 外部 trusted drop 无上限保留文件 wrappers 与全部同步字符串 MIME', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const destination = harness.makeElement('div', { id: 'drop-zone', role: 'region' });
    harness.connect(harness.document.body, destination);
    const files = Array.from(
      { length: 1_001 },
      (_, index) => ({ name: `外部-${index}.bin` }),
    );
    const values: Record<string, string> = {
      'text/plain': `plain-${'x'.repeat(100_000)}`,
      'text/uri-list': 'https://example.test/a?token=exact#fragment',
      'application/x-custom': '\u0000exact\u0001payload',
      'application/json': '{"exact":true}',
    };

    harness.dispatch('drop', {
      target: destination,
      isTrusted: true,
      dataTransfer: {
        files,
        types: ['text/plain', 'text/uri-list', 'Files', 'application/x-custom'],
        items: [
          { kind: 'string', type: 'application/json' },
          { kind: 'string', type: 'text/plain' },
          { kind: 'file', type: 'application/octet-stream' },
        ],
        getData(type: string) {
          return values[type] ?? '';
        },
      },
    });

    expect(harness.actions()).toHaveLength(1);
    const event = harness.actions()[0];
    expect(event).toMatchObject({
      type: 'drop',
      target: { id: 'drop-zone' },
      paths: [],
      fileCount: files.length,
      dropData: values,
    });
    expect(event.dropData).not.toHaveProperty('Files');
    expect(harness.stash().get(event.seq)).toEqual({ files });
    expect(parseRecorderEvent(JSON.stringify(event))).toMatchObject({
      type: 'drop',
      fileCount: files.length,
      dropData: values,
    });
  });

  it('v11 外部纯文本 drop 保留空文件列表和显式 text/plain', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const destination = harness.makeElement('textarea', { id: 'plain-drop' });
    harness.connect(harness.document.body, destination);
    harness.dispatch('drop', {
      target: destination,
      isTrusted: true,
      dataTransfer: {
        files: [],
        types: ['text/plain'],
        items: [{ kind: 'string', type: 'text/plain' }],
        getData: (type: string) => type === 'text/plain' ? '纯文本\nexact' : '',
      },
    });
    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'drop',
        fileCount: 0,
        dropData: { 'text/plain': '纯文本\nexact' },
      }),
    ]);
    expect(harness.stash().get(harness.actions()[0].seq)).toEqual({ files: [] });
  });

  it('v10 rollback 不把外部 drop 伪装成内部 drag', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 10 });
    const destination = harness.makeElement('div', { id: 'drop-zone', role: 'region' });
    harness.connect(harness.document.body, destination);
    harness.dispatch('drop', {
      target: destination,
      isTrusted: true,
      dataTransfer: {
        files: [{ name: 'outside.txt' }],
        types: ['text/plain'],
        items: [],
        getData: () => 'outside',
      },
    });
    expect(harness.actions()).toEqual([]);
  });

  it('v4 保留 click button/count/modifiers，并以 auxclick/contextmenu 覆盖中键与右键', () => {
    const harness = injectedRecorderHarness();
    const button = harness.makeElement('button', { id: 'pointer-target', type: 'button' });
    harness.connect(harness.document.body, button);

    harness.dispatch('click', {
      target: button,
      isTrusted: true,
      button: 0,
      detail: 1,
      ctrlKey: true,
      shiftKey: true,
    });
    harness.dispatch('auxclick', {
      target: button,
      isTrusted: true,
      button: 1,
      detail: 1,
      metaKey: true,
    });
    harness.dispatch('contextmenu', {
      target: button,
      isTrusted: true,
      button: 2,
      detail: 1,
      altKey: true,
    });

    expect(harness.actions()).toMatchObject([
      {
        type: 'click',
        clickButton: 'left',
        clickCount: 1,
        modifiers: ['Control', 'Shift'],
      },
      {
        type: 'click',
        clickButton: 'middle',
        clickCount: 1,
        modifiers: ['Meta'],
      },
      {
        type: 'click',
        clickButton: 'right',
        clickCount: 1,
        modifiers: ['Alt'],
      },
    ]);
  });

  it('与 Playwright recorder 一致：只给 CANVAS 点击记录元素内坐标', () => {
    const harness = injectedRecorderHarness();
    const canvas = harness.makeElement('canvas', { id: 'chart' });
    const button = harness.makeElement('button', { id: 'ordinary', type: 'button' });
    harness.connect(harness.document.body, canvas);
    harness.connect(harness.document.body, button);

    harness.dispatch('click', {
      target: canvas,
      isTrusted: true,
      button: 0,
      detail: 1,
      offsetX: 127.5,
      offsetY: 42.25,
    });
    harness.dispatch('click', {
      target: button,
      isTrusted: true,
      button: 0,
      detail: 1,
      offsetX: 9,
      offsetY: 8,
    });

    expect(harness.actions()).toMatchObject([
      {
        type: 'click',
        target: { tag: 'canvas', id: 'chart' },
        position: { x: 127.5, y: 42.25 },
      },
      {
        type: 'click',
        target: { tag: 'button', id: 'ordinary' },
        position: null,
      },
    ]);
  });

  it('v11 canvas signature 在 pointerup 后的同任务 click 只落一条连续手势', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const canvas = harness.makeElement('canvas', { id: 'signature-pad' });
    canvas.getBoundingClientRect = () => ({
      left: 100.25,
      top: 200.5,
      width: 640,
      height: 320,
    });
    harness.connect(harness.document.body, canvas);

    harness.dispatch('pointerdown', {
      target: canvas,
      isTrusted: true,
      isPrimary: true,
      pointerId: 7,
      button: 0,
      clientX: 110.5,
      clientY: 220.25,
      timeStamp: 1_000,
      shiftKey: true,
    });
    harness.dispatch('pointermove', {
      target: canvas,
      isTrusted: true,
      pointerId: 7,
      clientX: 120.75,
      clientY: 230.5,
      timeStamp: 1_006.5,
    });
    harness.dispatch('pointermove', {
      target: canvas,
      isTrusted: true,
      pointerId: 7,
      clientX: 130.5,
      clientY: 241,
      timeStamp: 1_013,
    });
    harness.dispatch('pointerup', {
      target: canvas,
      isTrusted: true,
      pointerId: 7,
      clientX: 137.125,
      clientY: 248.75,
      timeStamp: 1_027.25,
    });
    // Chromium dispatches this after pointerup for the same physical gesture.
    harness.dispatch('click', {
      target: canvas,
      isTrusted: true,
      detail: 1,
      button: 0,
      offsetX: 36.875,
      offsetY: 48.25,
    });

    expect(harness.actions()).toHaveLength(1);
    const gesture = harness.actions()[0];
    expect(gesture).toMatchObject({
      type: 'pointerGesture',
      target: { tag: 'canvas', id: 'signature-pad' },
      clickButton: 'left',
      modifiers: ['Shift'],
      clickCount: 0,
      position: null,
      gestureStart: { x: 10.25, y: 19.75 },
    });
    expect(gesture.gesturePoints.at(-1)).toEqual({
      x: 36.875,
      y: 48.25,
      elapsedMs: 27.25,
    });
    expect(parseRecorderEvent(JSON.stringify(gesture))).toMatchObject({
      type: 'pointerGesture',
      gestureStart: { x: 10.25, y: 19.75 },
      gesturePoints: expect.any(Array),
    });
  });

  it('v11 custom slider 使用 role=slider 根的 border-box 浮点坐标', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const slider = harness.makeElement('div', {
      id: 'custom-volume',
      role: 'slider',
    });
    const thumb = harness.makeElement('span', { id: 'decorative-thumb' });
    slider.getBoundingClientRect = () => ({
      left: 100.5,
      top: 10.25,
      width: 300,
      height: 40,
    });
    harness.connect(harness.document.body, slider);
    harness.connect(slider, thumb);

    harness.dispatch('pointerdown', {
      target: thumb,
      isTrusted: true,
      isPrimary: true,
      pointerId: 9,
      button: 0,
      clientX: 95.25,
      clientY: 30.5,
      timeStamp: 200,
      altKey: true,
    });
    harness.dispatch('pointermove', {
      target: thumb,
      isTrusted: true,
      pointerId: 9,
      clientX: 180.125,
      clientY: 30.5,
      timeStamp: 225.5,
    });
    harness.dispatch('pointerup', {
      target: thumb,
      isTrusted: true,
      pointerId: 9,
      clientX: 240.875,
      clientY: 30.5,
      timeStamp: 241,
    });
    harness.dispatch('click', {
      target: thumb,
      isTrusted: true,
      detail: 1,
      button: 0,
    });

    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'pointerGesture',
        target: expect.objectContaining({ id: 'custom-volume', role: 'slider' }),
        gestureStart: { x: -5.25, y: 20.25 },
        gesturePoints: expect.arrayContaining([
          { x: 140.375, y: 20.25, elapsedMs: 41 },
        ]),
        modifiers: ['Alt'],
      }),
    ]);
    expect(parseRecorderEvent(JSON.stringify(harness.actions()[0]))?.gestureStart)
      .toEqual({ x: -5.25, y: 20.25 });
  });

  it('v11 保留 pen 类型、每个采样遥测和原地压力变化', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const canvas = harness.makeElement('canvas', { id: 'pen-pad' });
    canvas.getBoundingClientRect = () => ({
      left: 50,
      top: 60,
      width: 500,
      height: 300,
    });
    harness.connect(harness.document.body, canvas);
    const telemetry = {
      pointerType: 'pen',
      tangentialPressure: -0.25,
      tiltX: 12,
      tiltY: -13,
      twist: 41,
      width: 8,
      height: 6,
    };

    harness.dispatch('pointerdown', {
      target: canvas,
      isTrusted: true,
      isPrimary: true,
      pointerId: 21,
      button: 0,
      clientX: 60,
      clientY: 70,
      timeStamp: 100,
      pressure: 0.2,
      ...telemetry,
    });
    // Pressure can change while the stylus is stationary. This is drawing
    // evidence and must not be collapsed as a geometric duplicate.
    harness.dispatch('pointermove', {
      target: canvas,
      isTrusted: true,
      pointerId: 21,
      clientX: 60,
      clientY: 70,
      timeStamp: 105,
      pressure: 0.55,
      ...telemetry,
    });
    harness.dispatch('pointermove', {
      target: canvas,
      isTrusted: true,
      pointerId: 21,
      clientX: 75,
      clientY: 80,
      timeStamp: 111,
      pressure: 0.8,
      ...telemetry,
    });
    harness.dispatch('pointerup', {
      target: canvas,
      isTrusted: true,
      pointerId: 21,
      clientX: 90,
      clientY: 95,
      timeStamp: 120,
      pressure: 0,
      ...telemetry,
    });
    harness.dispatch('click', {
      target: canvas,
      isTrusted: true,
      detail: 1,
      button: 0,
    });

    const gesture = harness.actions()[0];
    expect(gesture).toMatchObject({
      type: 'pointerGesture',
      pointerType: 'pen',
      gestureStart: {
        x: 10,
        y: 10,
        pressure: 0.2,
        tangentialPressure: -0.25,
        tiltX: 12,
        tiltY: -13,
        twist: 41,
        width: 8,
        height: 6,
      },
    });
    expect(gesture.gesturePoints).toEqual([
      expect.objectContaining({
        x: 10,
        y: 10,
        elapsedMs: 5,
        pressure: 0.55,
      }),
      expect.objectContaining({
        x: 25,
        y: 20,
        elapsedMs: 11,
        pressure: 0.8,
      }),
      expect.objectContaining({
        x: 40,
        y: 35,
        elapsedMs: 20,
        pressure: 0,
      }),
    ]);
    expect(parseRecorderEvent(JSON.stringify(gesture))).toMatchObject({
      pointerType: 'pen',
      gestureStart: expect.objectContaining({ pressure: 0.2, tiltX: 12 }),
      gesturePoints: expect.arrayContaining([
        expect.objectContaining({ pressure: 0.55 }),
      ]),
    });

    const impossiblePressure = structuredClone(gesture);
    impossiblePressure.gesturePoints[0].pressure = 2;
    expect(parseRecorderEvent(JSON.stringify(impossiblePressure))).toBeNull();
  });

  it('v11 native range 的 input 语义取消低层 pointerGesture', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const range = harness.makeElement('input', {
      id: 'native-volume',
      type: 'range',
    });
    harness.connect(harness.document.body, range);

    harness.dispatch('pointerdown', {
      target: range,
      isTrusted: true,
      isPrimary: true,
      pointerId: 11,
      button: 0,
      clientX: 10,
      clientY: 10,
      timeStamp: 100,
    });
    harness.dispatch('pointermove', {
      target: range,
      isTrusted: true,
      pointerId: 11,
      clientX: 80,
      clientY: 10,
      timeStamp: 120,
    });
    range.value = '73';
    harness.dispatch('input', { target: range, isTrusted: true });
    harness.dispatch('pointerup', {
      target: range,
      isTrusted: true,
      pointerId: 11,
      clientX: 80,
      clientY: 10,
      timeStamp: 125,
    });
    harness.dispatch('click', { target: range, isTrusted: true, detail: 1 });

    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'input',
        value: '73',
        target: expect.objectContaining({ id: 'native-volume', inputType: 'range' }),
      }),
    ]);
  });

  it('v11 scrollbar/scroll result cancels the lower-level pointer trajectory', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const scroller = harness.makeElement('div', { id: 'gesture-scroller' });
    Object.assign(scroller, {
      scrollTop: 100,
      scrollLeft: 0,
      scrollHeight: 1_000,
      clientHeight: 100,
      scrollWidth: 100,
      clientWidth: 100,
    });
    harness.connect(harness.document.body, scroller);

    harness.dispatch('pointerdown', {
      target: scroller,
      isTrusted: true,
      isPrimary: true,
      pointerId: 12,
      button: 0,
      clientX: 20,
      clientY: 20,
      timeStamp: 100,
    });
    harness.dispatch('pointermove', {
      target: scroller,
      isTrusted: true,
      pointerId: 12,
      clientX: 20,
      clientY: 60,
      timeStamp: 120,
    });
    scroller.scrollTop = 160;
    harness.dispatch('scroll', { target: scroller, isTrusted: true });
    harness.dispatch('pointerup', {
      target: scroller,
      isTrusted: true,
      pointerId: 12,
      clientX: 20,
      clientY: 60,
      timeStamp: 125,
    });
    expect(harness.control().flush()).toBe(1);

    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'scroll',
        scrollX: 0,
        scrollY: 60,
      }),
    ]);
  });

  it('v11 native HTML dragstart cancels pointer trajectory and keeps dragTo only', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const source = harness.makeElement('div', { id: 'native-drag-source', role: 'button' });
    const target = harness.makeElement('div', { id: 'native-drag-target', role: 'button' });
    harness.connect(harness.document.body, source);
    harness.connect(harness.document.body, target);

    harness.dispatch('pointerdown', {
      target: source,
      isTrusted: true,
      isPrimary: true,
      pointerId: 13,
      button: 0,
      clientX: 5,
      clientY: 5,
      timeStamp: 10,
    });
    harness.dispatch('pointermove', {
      target: source,
      isTrusted: true,
      pointerId: 13,
      clientX: 30,
      clientY: 30,
      timeStamp: 20,
    });
    harness.dispatch('dragstart', { target: source, isTrusted: true });
    harness.dispatch('drop', { target, isTrusted: true });
    harness.dispatch('pointerup', {
      target,
      isTrusted: true,
      pointerId: 13,
      clientX: 80,
      clientY: 80,
      timeStamp: 30,
    });
    harness.dispatch('click', { target, isTrusted: true, detail: 1 });

    expect(harness.actions().map((event) => event.type)).toEqual(['drag']);
  });

  it('v11 将 hover-menu 停留折叠成一个父菜单 hover，而不为最终 click 追加噪声', async () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const parent = harness.makeElement('button', { id: 'products', type: 'button' });
    const child = harness.makeElement('a', { id: 'reports', href: '/reports' });
    harness.connect(harness.document.body, parent);
    harness.connect(harness.document.body, child);

    harness.dispatch('pointerover', {
      target: parent,
      relatedTarget: null,
      isTrusted: true,
      offsetX: 4.25,
      offsetY: 8.5,
    });
    await new Promise((resolve) => setTimeout(resolve, 130));
    harness.dispatch('pointerover', {
      target: child,
      relatedTarget: parent,
      isTrusted: true,
    });
    harness.dispatch('click', {
      target: child,
      isTrusted: true,
      detail: 1,
    });

    expect(harness.actions().map((event) => event.type)).toEqual([
      'hover',
      'click',
    ]);
    expect(harness.actions()[0]).toMatchObject({
      type: 'hover',
      target: { id: 'products' },
      position: null,
    });
  });

  it('v11 records a consumed wheel gesture once even when it causes a native scroll', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 11 });
    const canvas = harness.makeElement('canvas', { id: 'zoom-surface' });
    Object.assign(canvas, {
      scrollTop: 0,
      scrollLeft: 0,
      scrollHeight: 2_000,
      clientHeight: 200,
      scrollWidth: 200,
      clientWidth: 200,
    });
    harness.connect(harness.document.body, canvas);

    harness.dispatch('wheel', {
      target: canvas,
      isTrusted: true,
      deltaX: 2,
      deltaY: -33,
      deltaMode: 0,
    });
    canvas.scrollTop = 33;
    harness.dispatch('scroll', { target: canvas, isTrusted: true });
    expect(harness.control().flush()).toBe(1);

    expect(harness.actions()).toEqual([
      expect.objectContaining({
        type: 'wheel',
        target: expect.objectContaining({ id: 'zoom-surface' }),
        scrollX: 2,
        scrollY: -33,
      }),
    ]);
  });

  it('v10 keeps wheel represented only by the post-layout scroll delta', () => {
    const harness = injectedRecorderHarness({ recordingSchemaVersion: 10 });
    const scroller = harness.makeElement('div', { id: 'v10-scroller' });
    Object.assign(scroller, {
      scrollTop: 100,
      scrollLeft: 0,
      scrollHeight: 2_000,
      clientHeight: 200,
      scrollWidth: 200,
      clientWidth: 200,
    });
    harness.connect(harness.document.body, scroller);

    harness.dispatch('wheel', {
      target: scroller,
      isTrusted: true,
      deltaY: 50,
      deltaMode: 0,
    });
    scroller.scrollTop = 150;
    harness.dispatch('scroll', { target: scroller, isTrusted: true });
    harness.control().flush();

    expect(harness.actions().map((event) => event.type)).toEqual(['scroll']);
  });

  it('wheel 先采内层容器基线，第一次 scroll 就保留真实净位移', async () => {
    const harness = injectedRecorderHarness();
    const scroller = harness.makeElement('div', { id: 'scroller' });
    scroller.scrollTop = 500;
    scroller.scrollLeft = 0;
    scroller.scrollHeight = 2_000;
    scroller.clientHeight = 200;
    scroller.scrollWidth = 200;
    scroller.clientWidth = 200;
    harness.connect(harness.document.body, scroller);

    harness.dispatch('wheel', { target: scroller, isTrusted: true, deltaY: 100 });
    scroller.scrollTop = 600;
    harness.dispatch('scroll', { target: scroller, isTrusted: true });
    await new Promise((resolve) => setTimeout(resolve, 300));

    expect(harness.actions()).toHaveLength(1);
    expect(harness.actions()[0]).toMatchObject({
      type: 'scroll',
      scrollX: 0,
      scrollY: 100,
      target: { id: 'scroller' },
    });
  });

  it('wheel、滚动条 pointerdown、keydown 与 touchstart 都保留内层首帧净位移', () => {
    for (const precursor of ['wheel', 'pointerdown', 'keydown', 'touchstart'] as const) {
      const harness = injectedRecorderHarness();
      const scroller = harness.makeElement('div', { id: `scroller-${precursor}` });
      Object.assign(scroller, {
        scrollTop: 500,
        scrollLeft: 40,
        scrollHeight: 2_000,
        clientHeight: 200,
        scrollWidth: 800,
        clientWidth: 200,
      });
      harness.connect(harness.document.body, scroller);

      if (precursor === 'keydown') {
        harness.dispatch('keydown', {
          target: scroller,
          isTrusted: true,
          key: 'PageDown',
          code: 'PageDown',
          keyCode: 34,
          isComposing: false,
          metaKey: false,
          ctrlKey: false,
          altKey: false,
          shiftKey: false,
        });
      } else {
        harness.dispatch(precursor, {
          target: scroller,
          isTrusted: true,
          button: 0,
          isPrimary: true,
        });
      }
      scroller.scrollLeft = 25;
      scroller.scrollTop = 575;
      harness.dispatch('scroll', { target: scroller, isTrusted: true });
      expect(harness.control().flush()).toBe(1);

      expect(harness.actions().filter((event) => event.type === 'scroll')).toEqual([
        expect.objectContaining({
          scrollX: -15,
          scrollY: 75,
          target: expect.objectContaining({ id: `scroller-${precursor}` }),
        }),
      ]);
    }
  });

  it('预事件同时采所有可滚动祖先，内层到边界后的外层首帧不丢', () => {
    const harness = injectedRecorderHarness();
    const outer = harness.makeElement('div', { id: 'outer-scroller' });
    const inner = harness.makeElement('div', { id: 'inner-scroller' });
    const child = harness.makeElement('span', { id: 'wheel-target' });
    Object.assign(outer, {
      scrollTop: 1_000,
      scrollLeft: 0,
      scrollHeight: 4_000,
      clientHeight: 400,
      scrollWidth: 400,
      clientWidth: 400,
    });
    Object.assign(inner, {
      scrollTop: 800,
      scrollLeft: 0,
      scrollHeight: 1_000,
      clientHeight: 200,
      scrollWidth: 200,
      clientWidth: 200,
    });
    harness.connect(harness.document.body, outer);
    harness.connect(outer, inner);
    harness.connect(inner, child);

    harness.dispatch('wheel', { target: child, isTrusted: true, deltaY: 120 });
    // The inner surface is already at its boundary, so scroll chaining moves
    // the outer ancestor even though the wheel target is nested inside inner.
    outer.scrollTop = 1_120;
    harness.dispatch('scroll', { target: outer, isTrusted: true });
    expect(harness.control().flush()).toBe(1);

    expect(harness.actions().filter((event) => event.type === 'scroll')).toEqual([
      expect.objectContaining({
        scrollX: 0,
        scrollY: 120,
        target: expect.objectContaining({ id: 'outer-scroller' }),
      }),
    ]);
  });

  it('document 与 window scroll 对四类 trusted 预事件使用同一精确基线', () => {
    for (const scrollTarget of ['document', 'window'] as const) {
      for (const precursor of ['wheel', 'pointerdown', 'keydown', 'touchstart'] as const) {
        const harness = injectedRecorderHarness();
        harness.setDocumentScroll(250, 800);
        const eventTarget = harness.document.body;
        if (precursor === 'keydown') {
          harness.dispatch('keydown', {
            target: eventTarget,
            isTrusted: true,
            key: 'PageDown',
            code: 'PageDown',
            keyCode: 34,
            isComposing: false,
            metaKey: false,
            ctrlKey: false,
            altKey: false,
            shiftKey: false,
          });
        } else {
          harness.dispatch(precursor, {
            target: eventTarget,
            isTrusted: true,
            button: 0,
            isPrimary: true,
          });
        }
        harness.setDocumentScroll(265, 910);
        harness.dispatch('scroll', {
          target: scrollTarget === 'document'
            ? harness.document
            : harness.windowScrollTarget(),
          isTrusted: true,
        });
        expect(harness.control().flush()).toBe(1);

        expect(harness.actions().filter((event) => event.type === 'scroll')).toEqual([
          expect.objectContaining({
            scrollX: 15,
            scrollY: 110,
            target: null,
          }),
        ]);
      }
    }
  });

  it('无可观察预事件的未知内层首帧不拿绝对位置冒充 delta', () => {
    const harness = injectedRecorderHarness();
    const scroller = harness.makeElement('div', { id: 'unprimed-scroller' });
    Object.assign(scroller, {
      scrollTop: 500,
      scrollLeft: 0,
      scrollHeight: 2_000,
      clientHeight: 200,
      scrollWidth: 200,
      clientWidth: 200,
    });
    harness.connect(harness.document.body, scroller);

    harness.dispatch('wheel', { target: scroller, isTrusted: false, deltaY: 100 });
    scroller.scrollTop = 600;
    harness.dispatch('scroll', { target: scroller, isTrusted: true });
    expect(harness.control().flush()).toBe(0);
    expect(harness.actions().filter((event) => event.type === 'scroll')).toEqual([]);

    harness.dispatch('pointerdown', {
      target: scroller,
      isTrusted: true,
      button: 0,
      isPrimary: true,
    });
    scroller.scrollTop = 650;
    harness.dispatch('scroll', { target: scroller, isTrusted: true });
    expect(harness.control().flush()).toBe(1);
    expect(harness.actions().filter((event) => event.type === 'scroll')).toEqual([
      expect.objectContaining({ scrollX: 0, scrollY: 50 }),
    ]);
  });

  it('待提交 scroll 在后续 click 与停止录制前同步落账且顺序不反转', () => {
    const harness = injectedRecorderHarness();
    const scroller = harness.makeElement('div', { id: 'ordered-scroller' });
    const button = harness.makeElement('button', { id: 'after-scroll', type: 'button' });
    Object.assign(scroller, {
      scrollTop: 100,
      scrollLeft: 0,
      scrollHeight: 2_000,
      clientHeight: 200,
      scrollWidth: 200,
      clientWidth: 200,
    });
    harness.connect(harness.document.body, scroller);
    harness.connect(harness.document.body, button);

    harness.dispatch('wheel', { target: scroller, isTrusted: true, deltaY: 80 });
    scroller.scrollTop = 180;
    harness.dispatch('scroll', { target: scroller, isTrusted: true });
    harness.dispatch('click', { target: button, isTrusted: true, detail: 1 });
    expect(harness.actions().map((event) => event.type)).toEqual(['scroll', 'click']);
    expect(harness.actions()[0]).toMatchObject({ scrollY: 80 });

    scroller.scrollTop = 230;
    harness.dispatch('scroll', { target: scroller, isTrusted: true });
    expect(harness.control().deactivate()).toBe(1);
    expect(harness.actions().at(-1)).toMatchObject({
      type: 'scroll',
      scrollY: 50,
      lifecycleFlush: true,
    });
  });

  it('Shadow DOM 内的 contenteditable 绑定到真实 editing host 并冻结 trusted 值', () => {
    const harness = injectedRecorderHarness();
    const component = harness.makeElement('div', { id: 'component' });
    const editor = harness.makeElement('div', { id: 'editor', contenteditable: 'true' });
    const nested = harness.makeElement('span');
    nested.isContentEditable = true;
    harness.connect(harness.document.body, component);
    // The editor is the root of an open shadow tree, not a light-DOM child.
    editor.parentElement = null;
    editor.getRootNode = () => ({ host: component });
    harness.connect(editor, nested);

    harness.trustedText(nested, 'human rich text');
    editor.__textNodes = [{ nodeValue: 'page-mutated rich text' }];
    harness.dispatch('blur', { target: nested, isTrusted: true });

    const inputs = harness.actions().filter((event) => event.type === 'input');
    expect(inputs).toHaveLength(Array.from('human rich text').length);
    expect(inputs.at(-1)).toMatchObject({
      type: 'input',
      value: 'human rich text',
      target: { id: 'editor', contentEditable: true },
    });
  });

  it('密码 beforeinput 不提前固化空占位，trusted input/blur 提交完整最终值', () => {
    const harness = injectedRecorderHarness();
    const ordinary = harness.makeElement('input', { id: 'ordinary' });
    const password = harness.makeElement('input', {
      id: 'password',
      type: 'password',
      name: 'password',
    });
    harness.connect(harness.document.body, ordinary);
    harness.connect(harness.document.body, password);

    harness.trustedKey(ordinary);
    harness.dispatch('beforeinput', { target: password, isTrusted: true });
    expect(harness.actions()).toHaveLength(0);

    password.value = 'must-cross-exactly';
    harness.dispatch('input', { target: password, isTrusted: true });
    harness.dispatch('blur', { target: password, isTrusted: true });
    expect(harness.actions()).toHaveLength(1);
    expect(harness.actions()[0]).toMatchObject({
      type: 'input',
      tier: 'secret',
      value: 'must-cross-exactly',
      valueTruncated: false,
      target: expect.objectContaining({ id: 'password', inputType: 'password' }),
    });
  });

  it('密码与 OTP 都按普通输入突发提交完整最终值', () => {
    for (const attributes of [
      { type: 'password', name: 'password' },
      { type: 'text', autocomplete: 'one-time-code', name: 'otp' },
    ]) {
      const event = captureInjectedInput(attributes, 'exact-replay-value');
      expect(event.type).toBe('input');
      expect(['secret', 'handoff']).toContain(event.tier);
      expect(event.value).toBe('exact-replay-value');
      expect(event.url).toBe('https://example.com/login');
      expect(event.target).toMatchObject({
        tag: 'input',
        name: attributes.name,
      });
      expect(event.provenance.targetEvidence).toBe('synchronous');
    }
  });
});

describe('parseRecorderEvent', () => {
  const base = { seq: 1, type: 'click', url: 'https://example.com', hint: 'button 提交' };

  it('丢弃非法 JSON 与非法类型', () => {
    expect(parseRecorderEvent('not json')).toBeNull();
    expect(parseRecorderEvent('null')).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({ ...base, type: 'eval' }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({ ...base, seq: 0 }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({ ...base, seq: 1.5 }))).toBeNull();
  });

  it('secret 与 handoff 档在宿主解析后仍保留完整值', () => {
    for (const tier of ['secret', 'handoff']) {
      const event = parseRecorderEvent(
        JSON.stringify({ ...base, type: 'input', tier, value: 'hunter2' }),
      );
      expect(event?.tier).toBe(tier);
      expect(event?.value).toBe('hunter2');
    }
  });

  it('identifier 与 plain 档保留值', () => {
    const event = parseRecorderEvent(
      JSON.stringify({ ...base, type: 'input', tier: 'identifier', value: 'A12345' }),
    );
    expect(event?.value).toBe('A12345');
  });

  it('未知分级降级为 plain 但不丢失动作证据', () => {
    const unknown = parseRecorderEvent(
      JSON.stringify({ ...base, type: 'input', tier: 'wildcard', value: 'x' }),
    );
    expect(unknown?.tier).toBe('plain');
    expect(unknown?.value).toBe('x');

    // 但点击、滚动这些事件本来就没有 tier 语义。早先把它们也兜底成 secret，
    // 结果是 hint 被抹成占位符，轨迹里看不出用户点了什么。
    for (const type of ['click', 'scroll', 'key', 'submit']) {
      const event = parseRecorderEvent(JSON.stringify({ ...base, type, hint: 'a 详情' }));
      expect(event?.tier).toBe('plain');
      expect(event?.hint).toBe('a 详情');
    }
  });

  it('保留事件时刻同步取下的元素身份', () => {
    // 这是跳页点击唯一可靠的对齐信息：宿主异步去取 backendNodeId 时文档已经换了
    // （实测点「详情」链接必然拿不到），而 href 能在三个同名链接里唯一确定是哪一行。
    const event = parseRecorderEvent(JSON.stringify({
      ...base,
      target: { tag: 'a', text: '详情', ariaLabel: '', href: '/feed/item?id=2', ordinal: 2 },
    }));
    expect(event?.target).toEqual({
      tag: 'a', text: '详情', ariaLabel: '', href: '/feed/item?id=2', ordinal: 2,
      id: '', name: '', role: '', inputType: '', testId: '', testIdAttribute: '',
      contentEditable: false,
      // 事件里没带 cssPath/framePath 时置空，而不是编造。宿主随后会因为拿不到临时
      // 路径而跳过 normalize()，这一步退回语义匹配。
      cssPath: '', framePath: [],
    });
  });

  it('cssPath / framePath 按白名单过滤后保留', () => {
    const event = parseRecorderEvent(JSON.stringify({
      ...base,
      target: {
        tag: 'a', text: '详情', ariaLabel: '', href: '', ordinal: 1,
        cssPath: '#list > li:nth-of-type(2) > a:nth-of-type(1)',
        framePath: ['#frame'],
      },
    }));
    expect(event?.target?.cssPath).toBe('#list > li:nth-of-type(2) > a:nth-of-type(1)');
    expect(event?.target?.framePath).toEqual(['#frame']);
  });

  it('完整保留页面与 Playwright 产生的选择器语法', () => {
    const event = parseRecorderEvent(JSON.stringify({
      ...base,
      target: {
        tag: 'a', text: '详情', ariaLabel: '', href: '', ordinal: 1,
        cssPath: '#a >> internal:role=button[name="删除全部"i]',
        framePath: ['#f >> internal:control=enter-frame'],
      },
    }));
    expect(event?.target?.cssPath).toBe('#a >> internal:role=button[name="删除全部"i]');
    expect(event?.target?.framePath).toEqual(['#f >> internal:control=enter-frame']);
  });

  it('secret/handoff 档完整保留 hint、目标与 URL', () => {
    for (const tier of ['secret', 'handoff']) {
      const event = parseRecorderEvent(JSON.stringify({
        ...base,
        type: 'input',
        tier,
        value: 'S3ntinel-Password!',
        hint: 'input S3ntinel-Password!',
        target: { tag: 'input', text: 'S3ntinel-Password!', ariaLabel: 'S3ntinel-Password!', href: '', ordinal: 1 },
      }));
      const serialized = JSON.stringify(event);
      expect(serialized).toContain('S3ntinel-Password!');
      expect(event?.hint).toBe('input S3ntinel-Password!');
      expect(event?.target).toMatchObject({
        text: 'S3ntinel-Password!',
        ariaLabel: 'S3ntinel-Password!',
      });
      expect(event?.url).toBe('https://example.com');
    }
  });

  it('privacy compatibility hook 对所有 tier 都是严格恒等映射', () => {
    const sentinel = 'S3NTINEL-private-90817';
    for (const tier of ['secret', 'handoff'] as const) {
      const original = {
        tier,
        selector: `internal:text=${sentinel}`,
        cssPath: `#${sentinel}`,
        framePath: [`#${sentinel}`],
        target: { text: sentinel },
        page: `页面 ${sentinel}`,
        pageTruncated: true,
        url: `https://example.com/${sentinel}`,
        hint: sentinel,
        value: sentinel,
        key: sentinel,
        provenance: {
          schemaVersion: 1,
          source: 'document-world',
        },
      };
      // 当前策略是**完整保留**，一个字节都不删——回放需要真实值。
      // 这不是遗漏：handoff 在编译期强制转 takeover，密码则由安装前的
      // 知情披露 + owner 私有 0600 存储承担代价。函数名必须如实反映这件事，
      // 否则读到调用点的人会以为这里在按分级抹值。
      const event = retainRecorderEvidence(original, tier);
      expect(event).toBe(original);
      expect(JSON.stringify(event)).toContain(sentinel);
    }
  });

  it('v2 provenance 严格校验且未知字段不会透传', () => {
    const payload = {
      ...base,
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      causalId: 0,
      clickButton: 'left',
      clickCount: 1,
      position: null,
      dragSourcePosition: null,
      dragTargetPosition: null,
      modifiers: [],
      dialogAction: '',
      dialogType: '',
      dialogText: '',
      values: [],
      uploadMode: '',
      paths: [],
      fileCount: 0,
      multiple: false,
      accept: '',
      dropData: {},
      target: {
        tag: 'button',
        text: '保存',
        id: 'saveButton',
        name: 'save',
        role: 'button',
        inputType: 'submit',
        contentEditable: false,
        testId: 'save-button',
        testIdAttribute: 'data-testid',
        cssPath: '#saveButton',
        framePath: [],
      },
      provenance: {
        schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
        source: 'document-world',
        capturePhase: 'event-callback',
        browserTrusted: true,
        targetEvidence: 'synchronous',
        nativeInput: 'unverified',
        injected: 'must-drop',
      },
    };
    const event = parseRecorderEvent(JSON.stringify(payload));
    expect(event?.schemaVersion).toBe(RECORDER_EVENT_SCHEMA_VERSION);
    expect(event?.target).toMatchObject({
      id: 'saveButton', name: 'save', role: 'button', inputType: 'submit',
      testId: 'save-button', testIdAttribute: 'data-testid',
    });
    expect(event?.provenance).toEqual({
      schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
      source: 'document-world',
      capturePhase: 'event-callback',
      browserTrusted: true,
      targetEvidence: 'synchronous',
      nativeInput: 'unverified',
    });
    expect(event).toMatchObject({
      clickButton: 'left',
      clickCount: 1,
      modifiers: [],
    });
    expect(JSON.stringify(event)).not.toContain('must-drop');

    const longText = '完整目标文本🚀'.repeat(20_000);
    const longSelector = `internal:role=button[name="${longText}"i]`;
    const deepFramePath = Array.from(
      { length: 100 },
      (_, index) => `internal:testid=[data-testid="frame-${index}"s]`,
    );
    const exact = parseRecorderEvent(JSON.stringify({
      ...payload,
      target: {
        ...payload.target,
        text: longText,
        ariaLabel: longText,
        href: `https://example.com/open?ticket=${longText}`,
        id: `按钮 ${longText}`,
        framePath: deepFramePath,
      },
      selectorSource: 'playwright',
      recordedSelector: longSelector,
      recordedDragSelector: '',
    }));
    expect(exact).toMatchObject({
      recordedSelector: longSelector,
      selectorSource: 'playwright',
      target: {
        text: longText,
        ariaLabel: longText,
        href: `https://example.com/open?ticket=${longText}`,
        id: `按钮 ${longText}`,
        framePath: deepFramePath,
      },
    });

    expect(parseRecorderEvent(JSON.stringify({ ...payload, schemaVersion: 99 }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({
      ...payload,
      provenance: { ...payload.provenance, browserTrusted: false },
    }))).toBeNull();
  });

  it('v5 upload 只接受页面可证明的固定形状，路径必须由 Host 补全', () => {
    const target = {
      tag: 'input',
      text: '',
      ariaLabel: '附件',
      href: '',
      ordinal: 1,
      id: 'attachment',
      name: '',
      role: '',
      inputType: 'file',
      contentEditable: false,
      testId: '',
      testIdAttribute: '',
      cssPath: '#attachment',
      framePath: [],
    };
    const provenance = {
      schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
      source: 'document-world',
      capturePhase: 'event-callback',
      browserTrusted: true,
      targetEvidence: 'synchronous',
      nativeInput: 'unverified',
    };
    const common = {
      ...base,
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      causalId: 0,
      type: 'upload',
      target,
      provenance,
      tier: 'plain',
      value: '',
      values: [],
      valueTruncated: false,
      lifecycleFlush: false,
      key: '',
      clickButton: '',
      clickCount: 0,
      position: null,
      dragSourcePosition: null,
      dragTargetPosition: null,
      modifiers: [],
      dialogAction: '',
      dialogType: '',
      dialogText: '',
      scrollX: 0,
      scrollY: 0,
      paths: [],
      multiple: true,
      accept: '.pdf',
      dropData: {},
    };
    expect(parseRecorderEvent(JSON.stringify({
      ...common,
      uploadMode: 'handoff',
      fileCount: 2,
    }))).toMatchObject({
      type: 'upload',
      uploadMode: 'handoff',
      paths: [],
      fileCount: 2,
      multiple: true,
      accept: '.pdf',
    });
    expect(parseRecorderEvent(JSON.stringify({
      ...common,
      uploadMode: 'clear',
      fileCount: 0,
    }))).toMatchObject({
      uploadMode: 'clear',
      paths: [],
      fileCount: 0,
    });

    // Document-world cannot author native paths or claim that it already resolved them.
    expect(parseRecorderEvent(JSON.stringify({
      ...common,
      uploadMode: 'paths',
      paths: ['/tmp/page-spoofed.pdf'],
      fileCount: 1,
    }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({
      ...common,
      uploadMode: 'handoff',
      fileCount: 2,
      paths: ['/tmp/partial.pdf'],
    }))).toBeNull();
  });

  it('v5 非 upload 事件要求 upload 字段保持统一空形状', () => {
    const payload = {
      ...base,
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      causalId: 0,
      clickButton: 'left',
      clickCount: 1,
      position: null,
      dragSourcePosition: null,
      dragTargetPosition: null,
      modifiers: [],
      dialogAction: '',
      dialogType: '',
      dialogText: '',
      values: [],
      uploadMode: '',
      paths: [],
      fileCount: 0,
      multiple: false,
      accept: '',
      dropData: {},
      provenance: {
        schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
        source: 'document-world',
        capturePhase: 'event-callback',
        browserTrusted: true,
        targetEvidence: 'none',
        nativeInput: 'unverified',
      },
    };
    expect(parseRecorderEvent(JSON.stringify(payload))).not.toBeNull();
    expect(parseRecorderEvent(JSON.stringify({ ...payload, fileCount: 1 }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({ ...payload, uploadMode: 'handoff' }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({ ...payload, paths: ['/tmp/x'] }))).toBeNull();
    const { accept: _accept, ...missingAccept } = payload;
    expect(parseRecorderEvent(JSON.stringify(missingAccept))).toBeNull();
  });

  it('external drop 使用严格固定形状并精确保留空或多 MIME data', () => {
    const payload = {
      ...base,
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      causalId: 0,
      type: 'drop',
      target: {
        tag: 'div',
        text: 'Drop here',
        ariaLabel: 'Drop here',
        href: '',
        ordinal: 1,
        id: 'drop-zone',
        name: '',
        role: 'region',
        inputType: '',
        contentEditable: false,
        testId: '',
        testIdAttribute: '',
        cssPath: '#drop-zone',
        framePath: [],
      },
      tier: 'plain',
      value: '',
      values: [],
      valueTruncated: false,
      lifecycleFlush: false,
      key: '',
      clickButton: '',
      clickCount: 0,
      position: null,
      dragSourcePosition: null,
      dragTargetPosition: null,
      modifiers: [],
      gestureStart: null,
      gesturePoints: [],
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
      provenance: {
        schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
        source: 'document-world',
        capturePhase: 'event-callback',
        browserTrusted: true,
        targetEvidence: 'synchronous',
        nativeInput: 'unverified',
      },
    };
    expect(parseRecorderEvent(JSON.stringify(payload))).toMatchObject({
      type: 'drop',
      fileCount: 0,
      dropData: {},
    });
    const exactData = {
      'text/plain': '\u0000exact\u0001payload',
      'text/uri-list': 'https://example.test/a?token=exact#fragment',
      '': '',
    };
    expect(parseRecorderEvent(JSON.stringify({
      ...payload,
      fileCount: 1_001,
      dropData: exactData,
    }))).toMatchObject({
      fileCount: 1_001,
      dropData: exactData,
    });
    const { dropData: _dropData, ...missingData } = payload;
    expect(parseRecorderEvent(JSON.stringify(missingData))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({
      ...payload,
      dropData: { 'text/plain': 42 },
    }))).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({
      ...payload,
      type: 'click',
      clickButton: 'left',
      clickCount: 1,
      dropData: { 'text/plain': 'must reject' },
    }))).toBeNull();
  });

  it('select-multiple 固定形状但不限制选项数量或值长度', () => {
    const payload = {
      ...base,
      schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
      causalId: 0,
      type: 'input',
      tier: 'plain',
      value: 'alpha',
      values: ['alpha', 'gamma'],
      valueTruncated: false,
      lifecycleFlush: false,
      key: '',
      clickButton: '',
      clickCount: 0,
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
      target: {
        tag: 'select',
        text: '',
        ariaLabel: '成员',
        href: '',
        ordinal: 1,
        id: 'members',
        name: 'members',
        role: '',
        inputType: 'select-multiple',
        contentEditable: false,
        testId: '',
        testIdAttribute: '',
        cssPath: '#members',
        framePath: [],
      },
      provenance: {
        schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
        source: 'document-world',
        capturePhase: 'event-callback',
        browserTrusted: true,
        targetEvidence: 'synchronous',
        nativeInput: 'unverified',
      },
    };
    expect(parseRecorderEvent(JSON.stringify(payload))).toMatchObject({
      value: 'alpha',
      values: ['alpha', 'gamma'],
      target: { tag: 'select', inputType: 'select-multiple' },
    });
    const manyValues = Array.from({ length: 5_000 }, (_, index) => `选项-${index}`);
    expect(parseRecorderEvent(JSON.stringify({
      ...payload,
      value: manyValues[0],
      values: manyValues,
    }))).toMatchObject({ value: manyValues[0], values: manyValues });
    const longValue = '超长选项🚀'.repeat(20_000);
    expect(parseRecorderEvent(JSON.stringify({
      ...payload,
      value: longValue,
      values: [longValue],
    }))).toMatchObject({ value: longValue, values: [longValue] });
    expect(parseRecorderEvent(JSON.stringify({
      ...payload,
      target: { ...payload.target, inputType: 'select-one' },
    }))).toBeNull();
  });

  it('secret tier 不删除任一可回放字段', () => {
    const sentinel = 'S3NTINEL-private-90817';
    const event = parseRecorderEvent(JSON.stringify({
      ...base,
      type: 'input',
      tier: 'secret',
      value: sentinel,
      url: `https://example.com/${sentinel}?token=${sentinel}`,
      hint: sentinel,
      key: sentinel,
      target: {
        tag: sentinel,
        text: sentinel,
        ariaLabel: sentinel,
        href: `/${sentinel}?token=${sentinel}`,
        ordinal: 1,
        id: sentinel,
        name: sentinel,
        role: sentinel,
        inputType: sentinel,
        testId: sentinel,
        testIdAttribute: 'data-testid',
        cssPath: `#${sentinel}`,
        framePath: [`#${sentinel}`],
      },
    }));
    expect(event).not.toBeNull();
    expect(JSON.stringify(event)).toContain(sentinel);
    expect(event).toMatchObject({
      url: `https://example.com/${sentinel}?token=${sentinel}`,
      hint: sentinel,
      value: sentinel,
      key: sentinel,
      target: {
        text: sentinel,
        ariaLabel: sentinel,
        href: `/${sentinel}?token=${sentinel}`,
        id: sentinel,
        name: sentinel,
        testId: sentinel,
        cssPath: `#${sentinel}`,
        framePath: [`#${sentinel}`],
      },
    });
  });

  it('普通 href 原样保留签名 query，保证 SSO 与签名导航可复现', () => {
    const event = parseRecorderEvent(JSON.stringify({
      ...base,
      target: {
        tag: 'a', text: '详情', ariaLabel: '', ordinal: 1,
        href: '/ticket/detail?id=GD-1&token=abc123SECRET&sig=deadbeef&page=2',
      },
    }));
    const href = event?.target?.href ?? '';
    expect(href).toBe(
      '/ticket/detail?id=GD-1&token=abc123SECRET&sig=deadbeef&page=2',
    );
  });

  it('hash 路由原样保留，不做 parse/reencode', () => {
    const event = parseRecorderEvent(JSON.stringify({
      ...base,
      target: {
        tag: 'a', text: 'x', ariaLabel: '', ordinal: 1,
        href: '/app#/detail?ticket=1&session=a1b2c3d4e5f6g7h8i9j0',
      },
    }));
    expect(event?.target?.href).toBe(
      '/app#/detail?ticket=1&session=a1b2c3d4e5f6g7h8i9j0',
    );
  });

  it('元素身份缺失或非法时置空而不是编造', () => {
    expect(parseRecorderEvent(JSON.stringify(base))?.target).toBeNull();
    expect(parseRecorderEvent(JSON.stringify({ ...base, target: 'oops' }))?.target).toBeNull();
    const bad = parseRecorderEvent(JSON.stringify({
      ...base,
      target: { tag: 'a', text: 'x', href: 'y', ordinal: -3 },
    }));
    expect(bad?.target?.ordinal).toBe(0);
    expect(bad?.target?.ariaLabel).toBe('');
  });

  it('URL 与文本字段完整保留，滚动量只做数值取整', () => {
    const url = `https://example.com/${'u'.repeat(100_000)}`;
    const hint = 'h'.repeat(100_000);
    const event = parseRecorderEvent(
      JSON.stringify({
        ...base,
        type: 'scroll',
        url,
        hint,
        scrollX: 12.7,
        scrollY: Number.NaN,
      }),
    );
    expect(event?.url).toBe(url);
    expect(event?.hint).toBe(hint);
    expect(event?.scrollX).toBe(12);
    expect(event?.scrollY).toBe(0);
  });
});
