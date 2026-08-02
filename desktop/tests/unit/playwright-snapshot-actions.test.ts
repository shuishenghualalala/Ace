import { Window } from 'happy-dom';
import { describe, expect, it, vi } from 'vitest';

import {
  ariaIdentityForLocator,
  captureSnapshot,
  captureSnapshotForFind,
  fingerprintRef,
} from '../../src/main/browser/playwright-snapshot';
import {
  assertState,
  click,
  registerOverlayHandler,
  fill,
  locateBySelector,
} from '../../src/main/browser/playwright-actions';

import type { ActionContext } from '../../src/main/browser/playwright-actions';
import type {
  FingerprintResult,
  RefRecord,
} from '../../src/main/browser/playwright-snapshot';
import type { Locator, Page } from '../../src/main/browser/playwright-compat';

function materialState(
  material: string,
  overrides: Partial<Omit<FingerprintResult, 'security'>> = {},
): Record<string, unknown> {
  const tag = overrides.tag ?? 'button';
  const inputType = overrides.inputType ?? 'button';
  return {
    material,
    navigation: '',
    downloadNavigation: '',
    action: '',
    actionKind: 'activate',
    accessibleRole: 'button',
    accessibleName: 'Submit',
    documentBaseURI: 'https://example.test/',
    documentURL: 'https://example.test/',
    tag,
    inputType,
    contentEditable: overrides.contentEditable ?? false,
    fieldProbe: {
      type: inputType,
      autocomplete: '',
      name: '',
      id: '',
      placeholder: '',
      ariaLabel: '',
      labelText: '',
    },
    complete: true,
    ...overrides,
  };
}

function attestedMaterial(
  material: string,
  tag = 'button',
  inputType = 'button',
  tier = 'plain',
  contentEditable = false,
): string {
  return [
    material,
    `attested-tag\0${tag}`,
    `attested-input-type\0${inputType}`,
    `attested-content-editable\0${contentEditable}`,
    `attested-field-tier\0${tier}`,
  ].join('\n');
}

function snapshotPage(
  raw: string | (() => string),
  evaluate: (selector: string, callback: unknown) => unknown = (selector) =>
    materialState(`material:${selector}`),
): {
  page: Page;
  selectors: string[];
  ariaOptions: Array<Record<string, unknown>>;
} {
  const selectors: string[] = [];
  const ariaOptions: Array<Record<string, unknown>> = [];
  const page = {
    ariaSnapshot: async (options: Record<string, unknown>) => {
      ariaOptions.push(options);
      return typeof raw === 'function' ? raw() : raw;
    },
    locator: (selector: string) => {
      selectors.push(selector);
      return {
        evaluate: async (callback: unknown) => evaluate(selector, callback),
      };
    },
    url: () => 'https://example.test/',
    title: async () => 'Example',
  } as unknown as Page;
  return { page, selectors, ariaOptions };
}

describe('playwright snapshot hardening', () => {
  it('find uses one raw aria snapshot for snippets and the complete ref table', async () => {
    const raw = [
      '- document "Example" [ref=e1]:',
      '  - navigation "Primary" [ref=e2]:',
      '    - link "Home" [ref=e3]',
      '  - main "Content" [ref=e4]:',
      '    - heading "Before" [ref=e5]',
      '    - paragraph: one',
      '    - paragraph: two',
      '    - button "TARGET one" [ref=e6]',
      '    - paragraph: three',
      '    - paragraph: four',
      '    - paragraph: five',
      '    - paragraph: six',
      '    - paragraph: seven',
      '    - paragraph: eight',
      '    - paragraph: nine',
      '    - button "target two" [ref=e7]',
      '    - paragraph: after',
    ].join('\n');
    const { page, ariaOptions } = snapshotPage(raw);

    const found = await captureSnapshotForFind(
      page,
      {
        full: true,
        hash: (value) => `hash:${value}`,
        timeoutMs: 1_000,
      },
      { text: 'TaRgEt' },
    );

    expect(ariaOptions).toHaveLength(1);
    expect(found.refs.size).toBe(7);
    expect(found.text).toContain('Found 2 matches for "TaRgEt":');
    expect(found.text).toContain('button "TARGET one" [ref=@e6]');
    expect(found.text).toContain('button "target two" [ref=@e7]');
    expect(found.text).toContain('\n\n----\n\n');
    // Separate snippets retain the upstream ancestor path, but a native ref is
    // exposed exactly once even when the same ancestor line is repeated.
    expect(found.text.match(/\[ref=@e1\]/g)).toHaveLength(1);
    expect(found.text.match(/\[ref=@e4\]/g)).toHaveLength(1);
    expect(found.text.match(/document "Example"/g)).toHaveLength(2);
    expect(found.text.match(/main "Content"/g)).toHaveLength(2);
    // Removing a duplicate structural ref must not glue the accessible name to
    // the trailing YAML colon in the second ancestor copy.
    expect(found.text).toContain('- document "Example" :');
    expect(found.text).toContain('  - main "Content" :');
  });

  it('find mirrors upstream regex flags, lastIndex reset and overlapping windows', async () => {
    const raw = [
      '- list:',
      '  - listitem "ERROR one" [ref=e1]',
      '  - paragraph: context a',
      '  - paragraph: context b',
      '  - listitem "error two" [ref=e2]',
      '  - paragraph: tail',
    ].join('\n');
    const { page, ariaOptions } = snapshotPage(raw);

    const found = await captureSnapshotForFind(
      page,
      {
        full: false,
        hash: (value) => `hash:${value}`,
        timeoutMs: 1_000,
      },
      { regex: '/error/gi' },
    );

    expect(ariaOptions).toHaveLength(1);
    expect(found.text).toContain('Found 2 matches for /error/i:');
    // The two ±3-line windows overlap and therefore form one snippet.
    expect(found.text).not.toContain('\n\n----\n\n');
    expect(found.text).toContain('[ref=@e1]');
    expect(found.text).toContain('[ref=@e2]');
  });

  it('find validates before capture and no-match still returns the full same-snapshot refs', async () => {
    const { page, ariaOptions } = snapshotPage(
      '- button "Continue" [ref=e1]\n- textbox "Keyword" [ref=e2]',
    );
    const options = {
      full: false,
      hash: (value: string) => `hash:${value}`,
      timeoutMs: 1_000,
    };

    await expect(
      captureSnapshotForFind(page, options, { regex: '[' }),
    ).rejects.toThrow();
    await expect(
      captureSnapshotForFind(
        page,
        options,
        { text: 'x', regex: 'x' } as never,
      ),
    ).rejects.toThrow('Provide only one');
    expect(ariaOptions).toHaveLength(0);

    const missing = await captureSnapshotForFind(
      page,
      options,
      { text: 'does-not-exist' },
    );
    expect(ariaOptions).toHaveLength(1);
    expect(missing.text).toBe('No matches found for "does-not-exist".');
    expect(missing.refs.size).toBe(2);
    expect(missing.refKeys).toEqual({});
  });

  it('compact 保留全部上下文与全部动作行', async () => {
    const context = [
      '- heading "订单详情"',
      '- paragraph: 请核对收件地址',
      '- table:',
      '  - row "商品 数量"',
      '    - cell "机械键盘"',
      '    - cell "2"',
      '- alert: 地址不能为空',
    ];
    const actions = Array.from(
      { length: 1_201 },
      (_, index) => `- button "${index === 0 ? '提交' : `动作 ${index + 1}`}" [ref=e${index + 1}]`,
    );
    const raw = [...context, ...actions].join('\n');
    const { page } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: false,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });

    expect(snapshot.text).toContain('heading "订单详情"');
    expect(snapshot.text).toContain('paragraph: 请核对收件地址');
    expect(snapshot.text).toContain('table:');
    expect(snapshot.text).toContain('cell "机械键盘"');
    expect(snapshot.text).toContain('alert: 地址不能为空');
    expect(snapshot.text).toContain('button "提交" [ref=@e1]');
    expect(snapshot.text).toContain('动作 1201" [ref=@e1201]');
    expect(snapshot.refs.size).toBe(1_201);
    expect(snapshot.truncated).toBe('');
  });

  it('只登记 node key 的结构 ref，并转义 name/text 中的伪 ref', async () => {
    const raw = [
      '- button "查看 [ref=e99]" [ref=e1]',
      '- text: [ref=e77]',
      '- \'button "A: [ref=e88]" [ref=e4]\'',
      '- button "重复属性" [ref=e2] [ref=e3]',
    ].join('\n');
    const { page, selectors } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });

    expect(snapshot.refs.size).toBe(2);
    expect(snapshot.text).toContain('- button "查看 [page-ref=e99]" [ref=@e1]');
    expect(snapshot.text).toContain('- text: [page-ref=e77]');
    expect(snapshot.text).toContain('button "A: [page-ref=e88]" [ref=@e2]');
    expect(snapshot.text).toContain('[page-ref=e2] [page-ref=e3]');
    expect(snapshot.text).not.toMatch(/\[ref=(?!@e\d+\])/);
    expect(selectors).toContain('aria-ref=e1');
    expect(selectors).toContain('aria-ref=e4');
    expect(selectors).not.toContain('aria-ref=e99');
    expect(selectors).not.toContain('aria-ref=e77');
  });

  it('完整保留任意长度的可访问身份', async () => {
    const oversizedName = 'N'.repeat(4_097);
    const raw = `- button ${JSON.stringify(oversizedName)} [ref=e1]`;
    const { page, selectors } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });

    expect(snapshot.refs.size).toBe(1);
    expect(snapshot.refs.get('@e1')?.name).toBe(oversizedName);
    expect(snapshot.text).toContain('[ref=@e1]');
    expect(snapshot.truncated).toBe('');
    expect(selectors).toContain('aria-ref=e1');
  });

  it('为全部 ref 尝试丰富元数据，单个探针失败也保留 Playwright ref', async () => {
    const refCount = 252;
    const raw = Array.from(
      { length: refCount },
      (_, index) => `- button "B${index + 1}" [ref=e${index + 1}]`,
    ).join('\n');
    const { page, selectors } = snapshotPage(raw, (selector) => {
      if (selector === 'aria-ref=e2') throw new Error('detached');
      return materialState(`material:${selector}`);
    });

    const snapshot = await captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });

    expect(snapshot.refs.size).toBe(refCount);
    expect(snapshot.refs.has('@e2')).toBe(true);
    expect(snapshot.text).toContain('[ref=@e2]');
    expect(snapshot.text).toContain('[ref=@e251]');
    expect(snapshot.text).toContain('[ref=@e252]');
    expect(snapshot.truncated).toBe('');
    expect(selectors.filter((selector) => selector.startsWith('aria-ref='))).toHaveLength(
      refCount,
    );
    expect(snapshot.refs.get('@e2')?.security).toContain('functional-ref');
    expect(snapshot.refs.get('@e251')?.security).toContain('material:aria-ref=e251');
  });

  it('完整暴露超过旧 1000 项上限的所有可执行 ref', async () => {
    const refCount = 1_200;
    const raw = Array.from(
      { length: refCount },
      (_, index) => `- button "R${index + 1}" [ref=e${index + 1}]`,
    ).join('\n');
    const { page, selectors } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });

    expect(snapshot.refs.size).toBe(refCount);
    expect(snapshot.refs.has(`@e${refCount}`)).toBe(true);
    expect(snapshot.text).toContain(`[ref=@e${refCount}]`);
    expect(snapshot.truncated).toBe('');
    // Production snapshots remain ref-only by default and do not trigger
    // supplementary Runtime/DOM probes.
    expect(selectors.filter((selector) => selector.startsWith('aria-ref='))).toHaveLength(0);
  });

  it('300+ 长列表全部可执行且全部获得丰富元数据，不受 viewport 影响', async () => {
    let scrollY = 0;
    const raw = () => Array.from(
      { length: 320 },
      (_, index) => {
        const row = index + 1;
        const y = index * 32 - scrollY;
        return `- button "Row ${row}" [ref=e${row}] [box=10,${y},180,24]`;
      },
    ).join('\n');
    const { page, ariaOptions, selectors } = snapshotPage(raw);
    const capture = () => captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });

    const before = await capture();
    const beforeRefs = new Set([...before.refs.values()].map((record) => record.playwrightRef));
    const beforeRich = new Set(
      selectors
        .filter((selector) => selector.startsWith('aria-ref='))
        .map((selector) => selector.slice('aria-ref='.length)),
    );
    expect(before.refs.size).toBe(320);
    expect(beforeRefs.has('e1')).toBe(true);
    expect(beforeRefs.has('e320')).toBe(true);
    expect(beforeRich.has('e1')).toBe(true);
    expect(beforeRich.has('e320')).toBe(true);
    expect(before.text).not.toContain('[box=');

    const selectorCursor = selectors.length;
    scrollY = 319 * 32;
    const after = await capture();
    const afterRefs = new Set([...after.refs.values()].map((record) => record.playwrightRef));
    const afterRich = new Set(
      selectors.slice(selectorCursor)
        .filter((selector) => selector.startsWith('aria-ref='))
        .map((selector) => selector.slice('aria-ref='.length)),
    );
    expect(after.refs.size).toBe(320);
    expect(afterRefs.has('e1')).toBe(true);
    expect(afterRefs.has('e320')).toBe(true);
    expect(afterRich.has('e1')).toBe(true);
    expect(afterRich.has('e320')).toBe(true);
    expect(after.text).toContain('Row 320');
    expect(after.text).not.toContain('[box=');
    expect(ariaOptions).toEqual([
      expect.objectContaining({ mode: 'ai', boxes: false }),
      expect.objectContaining({ mode: 'ai', boxes: false }),
    ]);
  });

  it('万级长列表完整保留文档首尾的全部元素', async () => {
    const rows = 12_000;
    const raw = Array.from(
      { length: rows },
      (_, index) => {
        const row = index + 1;
        const y = row === rows ? 12 : 100_000 + index * 30;
        return `- button "Deep row ${row}" [ref=e${row}] [box=10,${y},180,24]`;
      },
    ).join('\n');
    const { page } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });
    const exposed = new Set([...snapshot.refs.values()].map((record) => record.playwrightRef));

    expect(snapshot.refs.size).toBe(rows);
    expect(exposed.has('e1')).toBe(true);
    expect(exposed.has(`e${rows}`)).toBe(true);
    expect(snapshot.text).toContain(`Deep row ${rows}`);
    expect(snapshot.truncated).toBe('');
  });

  it('只清理 node key 末尾的结构 box，不让 name/text 里的伪 box 破坏 refs', async () => {
    const fakeVisibleBox = '[box=0,0,100,20]';
    const raw = [
      `- text: ${JSON.stringify(`页面正文 ${fakeVisibleBox}`)}`,
      ...Array.from({ length: 260 }, (_, index) => {
        const row = index + 1;
        return `- button ${JSON.stringify(`Trap ${row} ${fakeVisibleBox}`)} `
          + `[ref=e${row}] [box=0,${1_000_000 + index * 20},100,20]`;
      }),
      ...Array.from({ length: 60 }, (_, index) => {
        const row = index + 261;
        return `- button "Visible ${row}" [ref=e${row}] [box=0,${index * 20},100,18]`;
      }),
      // This order cannot be emitted by pinned Playwright: box is always last.
      `- button "Wrong order ${fakeVisibleBox}" [box=0,0,100,20] [ref=e321]`,
    ].join('\n');
    const { page } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });
    const exposed = new Set([...snapshot.refs.values()].map((record) => record.playwrightRef));

    expect(snapshot.refs.size).toBe(321);
    expect(exposed.has('e1')).toBe(true);
    expect(exposed.has('e250')).toBe(true);
    expect(exposed.has('e321')).toBe(true);
    expect(snapshot.text).not.toContain('[box=');
    expect(snapshot.text).toContain('[page-box=0,0,100,20]');
    expect(snapshot.truncated).toBe('');
  });

  it('完整保留带 box、无 box 及 iframe 子树里的全部 refs', async () => {
    const raw = [
      ...Array.from(
        { length: 260 },
        (_, index) =>
          `- button "Far ${index + 1}" [ref=e${index + 1}] `
          + `[box=0,${100_000 + index * 30},100,20]`,
      ),
      '- iframe [ref=e261] [box=10,10,400,300]:',
      '  - button "Visible frame child" [ref=f1e1] [box=20,20,120,24]',
      '- iframe [ref=e262]:',
      '  - button "Untrusted local box" [ref=f2e1] [box=20,20,120,24]',
      ...Array.from(
        { length: 30 },
        (_, index) => `- button "No box ${index + 1}" [ref=e${index + 263}]`,
      ),
    ].join('\n');
    const { page } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });
    const exposed = new Set([...snapshot.refs.values()].map((record) => record.playwrightRef));

    expect(snapshot.refs.size).toBe(294);
    expect(exposed.has('e261')).toBe(true);
    expect(exposed.has('f1e1')).toBe(true);
    expect(exposed.has('e262')).toBe(true);
    expect(exposed.has('f2e1')).toBe(true);
    expect(exposed.has('e260')).toBe(true);
    expect(snapshot.text).not.toContain('[box=');
  });

  it('完整保留超长正文和全部可执行行且不产生 orphan ref', async () => {
    const raw = [
      `- text: ${JSON.stringify('x'.repeat(35_000))}`,
      ...Array.from({ length: 310 }, (_, index) => {
        const row = index + 1;
        const y = row === 310 ? 10 : 1_000_000 + index * 20;
        return `- button "Row ${row}" [ref=e${row}] [box=0,${y},100,20]`;
      }),
    ].join('\n');
    const { page } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });
    const exposed = new Set([...snapshot.refs.values()].map((record) => record.playwrightRef));

    expect(snapshot.text.length).toBeGreaterThan(35_000);
    expect(snapshot.text).toContain('x'.repeat(35_000));
    expect(snapshot.text).not.toContain('[box=');
    expect(exposed.has('e310')).toBe(true);
    for (const nativeRef of snapshot.refs.keys()) {
      expect(snapshot.text).toContain(`[ref=${nativeRef}]`);
    }
    expect(snapshot.truncated).toBe('');
  });

  it('完整保留超过旧 raw 上限的 aria YAML', async () => {
    const raw = `- text: ${'x'.repeat(8 * 1024 * 1024 + 32)}`;
    const { page } = snapshotPage(raw);

    const snapshot = await captureSnapshot(page, {
      full: true,
      hash: (value) => `hash:${value}`,
      timeoutMs: 1_000,
    });

    expect(snapshot.refs.size).toBe(0);
    expect(snapshot.text).toBe(raw);
    expect(snapshot.truncated).toBe('');
  });

  it('目标语义复核不截断长名称或大型可访问子树', async () => {
    const longName = '语义'.repeat(4_500);
    const text = [
      `- button ${JSON.stringify(longName)} [ref=e1]`,
      ...Array.from(
        { length: 300 },
        (_, index) => `  - text: 子节点 ${index + 1}`,
      ),
    ].join('\n');
    const locator = {
      ariaSnapshot: async () => text,
    } as unknown as Locator;

    await expect(ariaIdentityForLocator(locator, 1_000)).resolves.toEqual({
      role: 'button',
      name: longName,
    });
  });

  it('指纹完整包含长属性、全部 labels 与全部 select options', async () => {
    const window = new Window({ url: 'https://example.test/large-form' });
    const select = window.document.createElement('select');
    const longPlaceholder = 'placeholder-'.repeat(500);
    select.setAttribute('placeholder', longPlaceholder);
    const labelIds: string[] = [];
    for (let index = 0; index < 40; index += 1) {
      const label = window.document.createElement('span');
      label.id = `label-${index}`;
      label.textContent = `Label ${index}`;
      labelIds.push(label.id);
      window.document.body.append(label);
    }
    select.setAttribute('aria-labelledby', labelIds.join(' '));
    for (let index = 0; index < 300; index += 1) {
      const option = window.document.createElement('option');
      option.value = `value-${index}`;
      option.textContent = `Option ${index}`;
      select.append(option);
    }
    window.document.body.append(select);
    const page = {
      locator: () => ({
        evaluate: async (callback: unknown) =>
          (callback as (element: Element) => unknown)(select as unknown as Element),
      }),
    } as unknown as Page;

    const result = await fingerprintRef(page, '#large', (value) => value, 1_000);

    expect(result.complete).toBe(true);
    expect(result.accessibleName).toContain('Label 39');
    expect(result.security).toContain(longPlaceholder);
    expect(result.security).toContain('option\u0000299\u0000value-299\u0000Option 299');
  });

  it('把 accessible 语义、baseURI、解析后 form 目标和动作类别绑定进指纹', async () => {
    const window = new Window({ url: 'https://example.test/app/page' });
    window.document.write(`
      <base href="https://example.test/v1/">
      <form action="../approve">
        <button id="approve" aria-label="Approve request">Go</button>
      </form>
    `);
    const button = window.document.querySelector('#approve');
    if (!button) throw new Error('fixture missing');
    const raw = '- button "Approve request" [ref=e1]';
    const page = {
      ariaSnapshot: async () => raw,
      locator: (selector: string) => {
        return {
          evaluate: async (callback: unknown) =>
            (callback as (element: Element) => unknown)(button as unknown as Element),
        };
      },
      url: () => window.location.href,
      title: async () => 'Fixture',
    } as unknown as Page;

    const snapshot = await captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash: (value) => value,
      timeoutMs: 1_000,
    });
    const record = snapshot.refs.get('@e1');
    expect(record).toBeDefined();
    expect(record?.security).toContain('accessible-role\u0000button');
    expect(record?.security).toContain('accessible-name\u0000Approve request');
    expect(record?.security).toContain('document-url\u0000https://example.test/app/page');
    expect(record?.security).toContain('document-base\u0000https://example.test/v1/');
    expect(record?.documentURL).toBe('https://example.test/app/page');
    expect(record?.downloadNavigation).toBe('https://example.test/approve');
    expect(record?.action).toBe('submit');
    expect(record?.actionKind).toBe('submit');
    expect(record?.tag).toBe('button');
    expect(record?.inputType).toBe('submit');
    expect(record?.fieldTier).toBe('plain');
    expect(record?.security).toContain('attested-field-tier\u0000plain');

    const before = await fingerprintRef(page, '#approve', (value) => value, 1_000);
    button.setAttribute('aria-label', 'Delete account');
    const afterName = await fingerprintRef(page, '#approve', (value) => value, 1_000);
    expect(afterName.security).not.toBe(before.security);
    window.document.querySelector('base')?.setAttribute('href', 'https://evil.example/root/');
    const afterBase = await fingerprintRef(page, '#approve', (value) => value, 1_000);
    expect(afterBase.security).not.toBe(afterName.security);
    expect(afterBase.documentURL).toBe('https://example.test/app/page');
    expect(afterBase.downloadNavigation).toBe('https://evil.example/approve');
  });
});

function actionRecord(security: string): RefRecord {
  return {
    selector: '#target',
    playwrightRef: '',
    role: 'button',
    name: 'Submit',
    securityKey: 'button\u0000Submit\u00001',
    security,
    navigation: '',
    downloadNavigation: '',
    action: '',
    actionKind: 'activate',
    semanticRole: 'button',
    semanticName: 'Submit',
    documentBaseURI: 'https://example.test/',
    documentURL: 'https://example.test/',
    tag: 'button',
    inputType: 'button',
    contentEditable: false,
    fieldTier: 'plain',
  };
}

function actionFixture(options: {
  onTrial?: () => void;
  onClick?: () => Promise<void>;
  onFill?: () => Promise<void>;
  onIsEnabled?: () => Promise<boolean>;
  onWaitFor?: () => Promise<void>;
  target?: 'button' | 'input';
} = {}): {
  ctx: ActionContext;
  state: { material: string };
  locator: Locator;
  calls: string[];
} {
  const state = { material: 'safe' };
  const calls: string[] = [];
  const inputTarget = options.target === 'input';
  const role = inputTarget ? 'textbox' : 'button';
  const name = inputTarget ? 'Keyword' : 'Submit';
  const tag = inputTarget ? 'input' : 'button';
  const inputType = inputTarget ? 'text' : 'button';
  const actionKind = inputTarget ? 'input' : 'activate';
  const locator = {
    _selector: '#target',
    normalize: vi.fn(async () => {
      calls.push('normalize');
      return locator;
    }),
    evaluate: vi.fn(async () => {
      calls.push('fingerprint');
      return materialState(state.material, {
        actionKind,
        accessibleRole: role,
        accessibleName: name,
        tag,
        inputType,
      });
    }),
    ariaSnapshot: vi.fn(async () => {
      calls.push('aria');
      return `- ${role} "${name}"`;
    }),
    click: vi.fn(async (clickOptions?: { trial?: boolean }) => {
      if (clickOptions?.trial) {
        calls.push('trial');
        options.onTrial?.();
        return;
      }
      calls.push('click');
      await options.onClick?.();
    }),
    waitFor: vi.fn(async () => {
      calls.push('wait-visible');
      if (options.onWaitFor) await options.onWaitFor();
    }),
    isEditable: vi.fn(async () => true),
    isEnabled: vi.fn(async () => {
      calls.push('is-enabled');
      if (options.onIsEnabled) return await options.onIsEnabled();
      return true;
    }),
    isDisabled: vi.fn(async () => false),
    isChecked: vi.fn(async () => {
      calls.push('is-checked');
      return true;
    }),
    fill: vi.fn(async () => {
      calls.push('fill');
      await options.onFill?.();
    }),
    press: vi.fn(async () => {
      calls.push('press');
    }),
    count: vi.fn(async () => {
      calls.push('count');
      return 1;
    }),
    textContent: vi.fn(async () => 'Submit'),
    getAttribute: vi.fn(async () => null),
  } as unknown as Locator;
  const hash = (value: string): string => `hash:${value}`;
  const page = {
    locator: () => locator,
    keyboard: { press: async () => undefined },
    mouse: { wheel: async () => undefined },
  } as unknown as Page;
  const ctx: ActionContext = {
    page,
    refs: new Map([['@e1', {
      ...actionRecord(hash(attestedMaterial(state.material, tag, inputType))),
      role,
      name,
      actionKind,
      semanticRole: role,
      semanticName: name,
      tag,
      inputType,
    }]]),
    hash,
    timeoutMs: 10_000,
  };
  return { ctx, state, locator, calls };
}

describe('playwright action execution semantics', () => {
  it('普通 DOM/可访问语义变化不再覆盖 Playwright 唯一定位与 actionability', async () => {
    const fixture = actionFixture({
      onTrial: () => {
        fixture.state.material = 'changed';
      },
    });

    await click(fixture.ctx, '@e1');
    expect(fixture.calls).toEqual(['click']);
  });

  it('遮挡处理器用 selector 注册并取 first()，不用会失效的 ref', async () => {
    // 处理器要跨越整场回放存活，而 ref 表每次快照整张替换——用 ref 必然失效。
    // first() 是因为多个弹窗排队时关闭按钮会有同名兄弟，strict 模式下多匹配
    // 会抛，而抛在处理器里会让**触发它的那个动作**失败。
    const fixture = actionFixture();
    const first = vi.fn(() => fixture.locator);
    const locate = vi.fn(() => ({ first }) as unknown as Locator);
    const handlers: Array<(locator: Locator) => Promise<unknown>> = [];
    const addLocatorHandler = vi.fn(
      async (_locator: Locator, handler: (locator: Locator) => Promise<unknown>) => {
        handlers.push(handler);
      },
    );
    (fixture.ctx.page as unknown as Record<string, unknown>).locator = locate;
    (fixture.ctx.page as unknown as Record<string, unknown>).addLocatorHandler =
      addLocatorHandler;

    await registerOverlayHandler(fixture.ctx, '#announce-close');

    expect(locate).toHaveBeenCalledWith('#announce-close');
    expect(first).toHaveBeenCalled();
    expect(addLocatorHandler).toHaveBeenCalledTimes(1);

    // 处理器内部只点击一次
    await handlers[0](fixture.locator);
    expect(fixture.locator.click).toHaveBeenCalled();
  });

  it('遮挡已自行消失时处理器不报错', async () => {
    // 动画结束、倒计时自动关闭都会让遮挡在我们点它之前消失。那不是错误，
    // 处理器里的失败也绝不能冒泡成触发动作的失败。
    const fixture = actionFixture({
      onClick: async () => {
        throw new Error('element is not attached to the DOM');
      },
    });
    const handlers: Array<(locator: Locator) => Promise<unknown>> = [];
    (fixture.ctx.page as unknown as Record<string, unknown>).locator = () => ({
      first: () => fixture.locator,
    });
    (fixture.ctx.page as unknown as Record<string, unknown>).addLocatorHandler =
      async (_l: Locator, handler: (locator: Locator) => Promise<unknown>) => {
        handlers.push(handler);
      };

    await registerOverlayHandler(fixture.ctx, '#gone');
    await expect(handlers[0](fixture.locator)).resolves.toBeUndefined();
  });

  it('空 selector 在碰页面之前就被拒', async () => {
    const fixture = actionFixture();
    const addLocatorHandler = vi.fn();
    (fixture.ctx.page as unknown as Record<string, unknown>).addLocatorHandler =
      addLocatorHandler;
    const rejected = await registerOverlayHandler(fixture.ctx, '')
      .catch((error) => error);
    expect(rejected).toMatchObject({ code: 'invalid_overlay' });
    expect(addLocatorHandler).not.toHaveBeenCalled();
  });

  it('visible/hidden 断言走 Playwright 原生 waitFor，天然带重试', async () => {
    // 用 isVisible() 那种一次性快照做断言，等于要求每个断言前面手工塞一个
    // wait——忘了塞的那一次会随机失败，而随机失败的断言比没有断言更糟。
    const fixture = actionFixture();
    await assertState(fixture.ctx, '@e1', 'visible');
    expect(fixture.calls).toContain('wait-visible');
    expect(fixture.locator.waitFor).toHaveBeenCalledWith(
      expect.objectContaining({ state: 'visible' }),
    );
  });

  it('断言不成立是独立的失败类别，不混进 stale_ref 或超时', async () => {
    // 混进 stale_ref，模型会去重新观察；混进 command_timeout，模型会去重试。
    // 而真正该做的是停下来报告"这一页不是预期的那一页"。
    const fixture = actionFixture({
      onWaitFor: async () => {
        throw new Error('Timeout 5000ms exceeded.\nwaiting for locator');
      },
    });
    const rejected = await assertState(fixture.ctx, '@e1', 'visible')
      .catch((error) => error);
    expect(rejected).toMatchObject({ code: 'assertion_failed' });
    expect(String(rejected.message)).toContain('断言不成立');
    expect(String(rejected.message)).toContain('visible');
  });

  it('enabled/checked 这类没有 waitFor 对应项的状态自己复刻 expect 的重试', async () => {
    let attempts = 0;
    const fixture = actionFixture({
      onIsEnabled: async () => {
        attempts += 1;
        // 前两轮还没就绪，第三轮成立——断言必须能等到它。
        return attempts >= 3;
      },
    });
    await assertState(fixture.ctx, '@e1', 'enabled');
    expect(attempts).toBeGreaterThanOrEqual(3);
  });

  it('状态一直不成立时断言必须在预算内退出，不能无限轮询', async () => {
    // 这是实测踩到的 bug：`dispatchTimeout` 在没有 ctx.deadlineAt 时返回的是
    // **常量** ctx.timeoutMs。原实现每轮拿它当"剩余预算"比较，条件永远不成立，
    // 于是一个永远不就绪的元素会把 Electron 主进程卡死。
    //
    // 这条用例刻意不设 deadlineAt，且让状态永远不成立。
    const fixture = actionFixture({ onIsEnabled: async () => false });
    fixture.ctx.timeoutMs = 300;
    delete (fixture.ctx as unknown as Record<string, unknown>).deadlineAt;

    const started = Date.now();
    const rejected = await assertState(fixture.ctx, '@e1', 'enabled')
      .catch((error) => error);
    const elapsed = Date.now() - started;

    expect(rejected).toMatchObject({ code: 'assertion_failed' });
    // 在预算量级内结束，而不是跑到 vitest 超时
    expect(elapsed).toBeLessThan(3_000);
  });

  it('未知断言状态在派发前就被拒', async () => {
    const fixture = actionFixture();
    const rejected = await assertState(fixture.ctx, '@e1', 'exists')
      .catch((error) => error);
    expect(rejected).toMatchObject({ code: 'invalid_assert' });
    // 一次都没碰过页面
    expect(fixture.calls).not.toContain('wait-visible');
  });

  it('mutation 调用后的异常标成 uncertain/partial，不伪报 stale_ref', async () => {
    const fixture = actionFixture({
      onClick: async () => {
        throw new Error('Timeout 2000ms exceeded after mouseDown');
      },
    });

    const rejected = await click(fixture.ctx, '@e1').catch((error) => error);
    expect(rejected).toMatchObject({
      code: 'input_uncertain',
      phase: 'dispatching',
      uncertain: true,
      partial: true,
    });
    expect(String(rejected.message)).toContain('可能已部分执行');
  });

  it('fill 后页面重渲染不触发额外指纹拒绝，继续在 exact locator 上提交', async () => {
    const fixture = actionFixture({
      target: 'input',
      onFill: async () => {
        fixture.state.material = 'form-target-changed';
      },
    });

    await fill(fixture.ctx, '@e1', 'hello', { submit: true });
    expect(fixture.calls).toEqual([
      'fill',
      'press',
    ]);
  });

  it('locate 只做 exact selector strict count，不制造安全指纹', async () => {
    const fixture = actionFixture();
    const state = materialState('located', {
      navigation: 'https://example.test/next',
      downloadNavigation: 'https://example.test/next',
      actionKind: 'navigate',
      accessibleRole: 'link',
      accessibleName: 'Next step',
      documentBaseURI: 'https://example.test/',
      documentURL: 'https://frame.example.test/',
      tag: 'a',
      inputType: '',
      contentEditable: false,
    });
    state.fieldProbe = {
      type: '',
      autocomplete: '',
      name: '',
      id: '',
      placeholder: '',
      ariaLabel: '',
      labelText: '',
    };
    vi.mocked(fixture.locator.evaluate).mockResolvedValue(state);
    vi.mocked(fixture.locator.ariaSnapshot).mockResolvedValue('- link "Next step"');
    const ctx = { ...fixture.ctx, refs: new Map<string, RefRecord>() };

    const record = await locateBySelector(
      ctx,
      '@s1',
      'internal:role=link[name="Next step"i]',
      ctx.hash,
    );

    expect(record).toMatchObject({
      role: 'generic',
      name: '',
      navigation: '',
      actionKind: 'activate',
      semanticRole: 'generic',
      documentBaseURI: '',
      documentURL: '',
      tag: '',
      inputType: '',
      contentEditable: false,
      fieldTier: 'plain',
    });
    expect(record.security).toBe('');
    expect(record.securityKey).toBe('@s1');
    expect(ctx.refs.get('@s1')).toBe(record);
    expect(fixture.locator.evaluate).not.toHaveBeenCalled();
    expect(fixture.locator.ariaSnapshot).not.toHaveBeenCalled();
  });
});
