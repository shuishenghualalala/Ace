import { EventEmitter } from 'node:events';

import { describe, expect, it, vi } from 'vitest';

import {
  ActionError,
  click,
  drop,
  drag,
  fill,
  fillForm,
  goBack,
  goForward,
  hover,
  keyDown,
  keyUp,
  locateBySelector,
  mouseClick,
  mouseDown,
  mouseDrag,
  mouseMove,
  mouseUp,
  mouseWheel,
  pointerGesture,
  press,
  reload,
  resize,
  selectOption,
  setChecked,
  upload,
  uploadFileChooser,
  waitFor,
  withActionCompletion,
} from '../../src/main/browser/playwright-actions';

import type { ActionContext } from '../../src/main/browser/playwright-actions';
import type {
  CDPSession,
  FileChooser,
  Locator,
  Page,
} from '../../src/main/browser/playwright-compat';
import type { RefRecord } from '../../src/main/browser/playwright-snapshot';

function record(selector: string, playwrightRef: string): RefRecord {
  return {
    selector,
    playwrightRef,
    role: 'button',
    name: selector,
    securityKey: '',
    security: '',
    navigation: '',
    downloadNavigation: '',
    action: '',
    actionKind: 'activate',
    semanticRole: 'button',
    semanticName: selector,
    documentBaseURI: 'https://example.test/',
    documentURL: 'https://example.test/',
    tag: 'button',
    inputType: 'button',
    contentEditable: false,
    fieldTier: 'plain',
  };
}

function actionFixture(): {
  ctx: ActionContext;
  start: Locator;
  end: Locator;
  page: Page;
  cdp: CDPSession;
  cdpSend: ReturnType<typeof vi.fn>;
  cdpDetach: ReturnType<typeof vi.fn>;
} {
  const start = {
    _selector: '#start',
    normalize: vi.fn(async () => start),
    count: vi.fn(async () => 1),
    click: vi.fn(async () => undefined),
    hover: vi.fn(async () => undefined),
    dragTo: vi.fn(async () => undefined),
    waitFor: vi.fn(async () => undefined),
    isEditable: vi.fn(async () => true),
    fill: vi.fn(async () => undefined),
    pressSequentially: vi.fn(async () => undefined),
    press: vi.fn(async () => undefined),
    selectOption: vi.fn(async (values: string[]) => values),
    setChecked: vi.fn(async () => undefined),
    drop: vi.fn(async () => undefined),
    evaluate: vi.fn(async () => undefined),
    setInputFiles: vi.fn(async () => undefined),
    scrollIntoViewIfNeeded: vi.fn(async () => undefined),
    boundingBox: vi.fn(async () => ({
      x: 100.5,
      y: 200.25,
      width: 640,
      height: 320,
    })),
  } as unknown as Locator;
  const end = {
    _selector: '#end',
    normalize: vi.fn(async () => end),
    count: vi.fn(async () => 1),
    hover: vi.fn(async () => undefined),
  } as unknown as Locator;
  const byText = new Map<string, ReturnType<typeof vi.fn>>();
  const cdpSend = vi.fn(async () => ({}));
  const cdpDetach = vi.fn(async () => undefined);
  const cdp = {
    send: cdpSend,
    detach: cdpDetach,
  } as unknown as CDPSession;
  const context = {
    newCDPSession: vi.fn(async () => cdp),
  };
  const page = {
    url: vi.fn(() => 'https://example.test/'),
    locator: vi.fn((selector: string) => {
      if (selector === 'aria-ref=e1' || selector === '#start') return start;
      if (selector === 'aria-ref=e2' || selector === '#end') return end;
      throw new Error(`unexpected selector ${selector}`);
    }),
    keyboard: {
      press: vi.fn(async () => undefined),
      down: vi.fn(async () => undefined),
      up: vi.fn(async () => undefined),
    },
    mouse: {
      move: vi.fn(async () => undefined),
      down: vi.fn(async () => undefined),
      up: vi.fn(async () => undefined),
      wheel: vi.fn(async () => undefined),
      click: vi.fn(async () => undefined),
    },
    setViewportSize: vi.fn(async () => undefined),
    goBack: vi.fn(async () => ({})),
    goForward: vi.fn(async () => ({})),
    reload: vi.fn(async () => null),
    waitForTimeout: vi.fn(async () => undefined),
    context: vi.fn(() => context),
    getByText: vi.fn((text: string) => {
      let waiter = byText.get(text);
      if (!waiter) {
        waiter = vi.fn(async () => undefined);
        byText.set(text, waiter);
      }
      return {
        first: () => ({ waitFor: waiter }),
      };
    }),
  } as unknown as Page;
  return {
    ctx: {
      page,
      refs: new Map([
        ['@e1', record('#start', 'e1')],
        ['@e2', record('#end', 'e2')],
      ]),
      hash: (value) => value,
      timeoutMs: 12_345,
    },
    start,
    end,
    page,
    cdp,
    cdpSend,
    cdpDetach,
  };
}

describe('Playwright MCP action parity', () => {
  it('forwards caller-selected click count, delay, and coordinates without product caps', async () => {
    const { ctx, start } = actionFixture();

    await click(ctx, '@e1', {
      button: 'right',
      clickCount: 4,
      modifiers: ['ControlOrMeta', 'Shift'],
      delayMs: 5_001,
      position: { x: 1_000_127.5, y: 42.25 },
    });

    expect(start.click).toHaveBeenCalledOnce();
    expect(start.click).toHaveBeenCalledWith({
      timeout: 12_345,
      button: 'right',
      clickCount: 4,
      modifiers: ['ControlOrMeta', 'Shift'],
      delay: 5_001,
      position: { x: 1_000_127.5, y: 42.25 },
    });
  });

  it('does not dispatch a mutation after the shared command deadline has expired', async () => {
    const { ctx, start } = actionFixture();
    ctx.deadlineAt = Date.now() - 1;

    await expect(click(ctx, '@e1')).rejects.toMatchObject({
      code: 'command_timeout',
      phase: 'pre_dispatch',
      uncertain: false,
      partial: false,
    });
    expect(start.click).not.toHaveBeenCalled();
  });

  it('treats a page that closes after dispatch as a completed action', async () => {
    const { ctx, start, page } = actionFixture();
    Object.assign(page, {
      isClosed: vi.fn(() => true),
    });
    vi.mocked(page.waitForTimeout).mockRejectedValue(
      new Error('page.waitForTimeout: Target page, context or browser has been closed'),
    );

    await expect(click(ctx, '@e1')).resolves.toBeUndefined();
    expect(start.click).toHaveBeenCalledOnce();
  });

  it('observes a navigation request from action start and waits for load before returning', async () => {
    const { ctx, start, page } = actionFixture();
    let requestListener: ((request: unknown) => void) | undefined;
    const waitForLoadState = vi.fn(async () => undefined);
    Object.assign(page as unknown as Record<string, unknown>, {
      on: vi.fn((event: string, listener: (request: unknown) => void) => {
        if (event === 'request') requestListener = listener;
      }),
      off: vi.fn((event: string, listener: (request: unknown) => void) => {
        if (event === 'request' && requestListener === listener) requestListener = undefined;
      }),
      mainFrame: vi.fn(() => ({ waitForLoadState })),
    });
    vi.mocked(start.click).mockImplementation(async () => {
      requestListener?.({
        isNavigationRequest: () => true,
        resourceType: () => 'document',
        response: async () => null,
      });
    });

    await click(ctx, '@e1');

    expect(page.waitForTimeout).toHaveBeenCalledWith(500);
    expect(waitForLoadState).toHaveBeenCalledWith('load', { timeout: 10_000 });
    expect(page.off).toHaveBeenCalledWith('request', expect.any(Function));
  });

  it('does not swallow a navigation observation timeout that exhausts the command deadline', async () => {
    const { ctx, start, page } = actionFixture();
    let currentTime = 10_000;
    const now = vi.spyOn(Date, 'now').mockImplementation(() => currentTime);
    try {
      ctx.deadlineAt = 10_600;
      let requestListener: ((request: unknown) => void) | undefined;
      const waitForLoadState = vi.fn(async (_state: string, options: { timeout: number }) => {
        currentTime += options.timeout;
        throw new Error(`Timeout ${options.timeout}ms exceeded`);
      });
      Object.assign(page as unknown as Record<string, unknown>, {
        on: vi.fn((event: string, listener: (request: unknown) => void) => {
          if (event === 'request') requestListener = listener;
        }),
        off: vi.fn(),
        mainFrame: vi.fn(() => ({ waitForLoadState })),
      });
      vi.mocked(page.waitForTimeout).mockImplementation(async (milliseconds) => {
        currentTime += milliseconds;
      });
      vi.mocked(start.click).mockImplementation(async () => {
        requestListener?.({
          isNavigationRequest: () => true,
          resourceType: () => 'document',
          response: async () => null,
        });
      });

      await expect(click(ctx, '@e1')).rejects.toMatchObject({
        code: 'command_timeout',
        phase: 'dispatching',
        uncertain: true,
        partial: true,
        completedCount: 1,
      });
      expect(waitForLoadState).toHaveBeenCalledWith('load', { timeout: 100 });
    } finally {
      now.mockRestore();
    }
  });

  it('returns immediately on an unplanned dialog while retaining the underlying action', async () => {
    const { ctx, start, page } = actionFixture();
    const events = new EventEmitter();
    Object.assign(page as unknown as Record<string, unknown>, {
      on: events.on.bind(events),
      once: events.once.bind(events),
      off: events.off.bind(events),
    });
    let settleAction!: () => void;
    const actionSettled = new Promise<void>((resolve) => {
      settleAction = resolve;
    });
    vi.mocked(start.click).mockImplementation(async () => {
      await actionSettled;
    });
    let retained: Promise<void> | undefined;
    ctx.onModalActionPending = (pending) => {
      retained = pending;
    };

    const result = click(ctx, '@e1').catch((error: unknown) => error);
    await Promise.resolve();
    await Promise.resolve();
    events.emit('dialog', {});

    await expect(result).resolves.toMatchObject({
      code: 'dialog_pending',
      phase: 'dispatching',
      uncertain: false,
      partial: false,
    });
    expect(retained).toBeDefined();

    settleAction();
    await expect(retained).resolves.toBeUndefined();
  });

  it('races a dialog opened during the completion settle window', async () => {
    const { ctx, start, page } = actionFixture();
    const events = new EventEmitter();
    Object.assign(page as unknown as Record<string, unknown>, {
      on: events.on.bind(events),
      off: events.off.bind(events),
    });
    let enterSettle!: () => void;
    const settleEntered = new Promise<void>((resolve) => {
      enterSettle = resolve;
    });
    let releaseSettle!: () => void;
    const settleGate = new Promise<void>((resolve) => {
      releaseSettle = resolve;
    });
    vi.mocked(page.waitForTimeout).mockImplementation(async (milliseconds) => {
      if (milliseconds === 500) {
        enterSettle();
        await settleGate;
      }
    });
    vi.mocked(start.click).mockResolvedValue(undefined);
    let retained: Promise<void> | undefined;
    ctx.onModalActionPending = (pending) => {
      retained = pending;
    };

    const result = click(ctx, '@e1').catch((error: unknown) => error);
    await settleEntered;
    events.emit('dialog', {});

    await expect(result).resolves.toMatchObject({ code: 'dialog_pending' });
    expect(retained).toBeDefined();
    releaseSettle();
    await expect(retained).resolves.toBeUndefined();
    expect(events.listenerCount('dialog')).toBe(0);
  });

  it('keeps request observation alive until a retained modal continuation fully settles', async () => {
    const { ctx, start, page } = actionFixture();
    const events = new EventEmitter();
    Object.assign(page as unknown as Record<string, unknown>, {
      on: events.on.bind(events),
      off: events.off.bind(events),
    });
    let releaseAction!: () => void;
    const actionGate = new Promise<void>((resolve) => {
      releaseAction = resolve;
    });
    let releaseResponse!: () => void;
    const responseGate = new Promise<void>((resolve) => {
      releaseResponse = resolve;
    });
    const finished = vi.fn(async () => {
      await responseGate;
    });
    vi.mocked(page.waitForTimeout).mockImplementation(async (milliseconds) => {
      if (milliseconds === 5_000) await new Promise<void>(() => undefined);
    });
    vi.mocked(start.click).mockImplementation(async () => {
      events.emit('request', {
        isNavigationRequest: () => false,
        resourceType: () => 'fetch',
        response: async () => ({ finished }),
      });
      events.emit('dialog', {});
      await actionGate;
    });
    let retained: Promise<void> | undefined;
    ctx.onModalActionPending = (pending) => {
      retained = pending;
    };

    await expect(click(ctx, '@e1')).rejects.toMatchObject({
      code: 'dialog_pending',
    });
    expect(events.listenerCount('request')).toBe(1);
    releaseAction();
    await vi.waitFor(() => {
      expect(finished).toHaveBeenCalledOnce();
    });
    let settled = false;
    void retained?.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    releaseResponse();
    await expect(retained).resolves.toBeUndefined();
    expect(events.listenerCount('request')).toBe(0);
  });

  it('waits for action-triggered fetch completion and one final settle window', async () => {
    const { ctx, start, page } = actionFixture();
    let requestListener: ((request: unknown) => void) | undefined;
    const finished = vi.fn(async () => undefined);
    const response = vi.fn(async () => ({ finished }));
    Object.assign(page as unknown as Record<string, unknown>, {
      on: vi.fn((event: string, listener: (request: unknown) => void) => {
        if (event === 'request') requestListener = listener;
      }),
      off: vi.fn(() => {
        requestListener = undefined;
      }),
    });
    vi.mocked(start.click).mockImplementation(async () => {
      requestListener?.({
        isNavigationRequest: () => false,
        resourceType: () => 'fetch',
        response,
      });
    });

    await click(ctx, '@e1');

    expect(response).toHaveBeenCalledOnce();
    expect(finished).toHaveBeenCalledOnce();
    expect(page.waitForTimeout).toHaveBeenNthCalledWith(1, 500);
    expect(page.waitForTimeout).toHaveBeenCalledWith(5_000);
    expect(page.waitForTimeout).toHaveBeenLastCalledWith(500);
  });

  it('reports command timeout when request settling consumes the absolute deadline', async () => {
    const { ctx, start, page } = actionFixture();
    let currentTime = 20_000;
    const now = vi.spyOn(Date, 'now').mockImplementation(() => currentTime);
    try {
      ctx.deadlineAt = 20_600;
      let requestListener: ((request: unknown) => void) | undefined;
      Object.assign(page as unknown as Record<string, unknown>, {
        on: vi.fn((event: string, listener: (request: unknown) => void) => {
          if (event === 'request') requestListener = listener;
        }),
        off: vi.fn(),
      });
      vi.mocked(page.waitForTimeout).mockImplementation(async (milliseconds) => {
        currentTime += milliseconds;
      });
      vi.mocked(start.click).mockImplementation(async () => {
        requestListener?.({
          isNavigationRequest: () => false,
          resourceType: () => 'fetch',
          response: async () => ({
            finished: () => new Promise<void>(() => undefined),
          }),
        });
      });

      await expect(click(ctx, '@e1')).rejects.toMatchObject({
        code: 'command_timeout',
        phase: 'dispatching',
        uncertain: true,
        partial: true,
        completedCount: 1,
      });
      expect(page.waitForTimeout).toHaveBeenNthCalledWith(1, 500);
      expect(page.waitForTimeout).toHaveBeenNthCalledWith(2, 100);
    } finally {
      now.mockRestore();
    }
  });

  it.each([
    { clickCount: 0 },
    { clickCount: 1.5 },
    { delayMs: -1 },
    { modifiers: ['Shift', 'Shift'] },
    { button: 'primary' },
    { position: { x: -1, y: 2 } },
    { position: { x: Number.NaN, y: 2 } },
    { position: { x: 1 } },
  ])('rejects invalid click options before resolving a ref: %o', async (options) => {
    const { ctx, page } = actionFixture();

    const error = await click(
      ctx,
      '@e1',
      options as Parameters<typeof click>[2],
    ).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ActionError);
    expect(error).toMatchObject({ code: 'invalid_input', uncertain: false });
    expect(page.locator).not.toHaveBeenCalled();
  });

  it('does not shrink the Host 15-second actionability budget', async () => {
    const { ctx, start } = actionFixture();
    ctx.timeoutMs = 15_000;

    await click(ctx, '@e1');

    expect(start.click).toHaveBeenCalledWith(expect.objectContaining({
      timeout: 15_000,
    }));
  });

  it('classifies an actionability timeout before native dispatch as not actionable', async () => {
    const { ctx, start } = actionFixture();
    vi.mocked(start.click).mockRejectedValueOnce(new Error(
      'locator.click: Timeout 12345ms exceeded.\n'
      + 'Call log:\n'
      + '  - attempting click action\n'
      + '  - waiting for element to be visible, enabled and stable\n'
      + '  - element is not visible',
    ));

    await expect(click(ctx, '@e1')).rejects.toMatchObject({
      code: 'element_not_actionable',
      phase: 'pre_dispatch',
      uncertain: false,
      partial: false,
    });
  });

  it('keeps the same timeout uncertain once the call log proves dispatch', async () => {
    const { ctx, start } = actionFixture();
    vi.mocked(start.click).mockRejectedValueOnce(new Error(
      'locator.click: Timeout 12345ms exceeded.\n'
      + 'Call log:\n'
      + '  - waiting for element to be visible, enabled and stable\n'
      + '  - performing click action',
    ));

    await expect(click(ctx, '@e1')).rejects.toMatchObject({
      code: 'input_uncertain',
      phase: 'dispatching',
      uncertain: true,
      partial: true,
    });
  });

  it('clicks the exact aria-ref when two elements share the same role and name', async () => {
    const sharedSemantic = {
      count: vi.fn(async () => 2),
      click: vi.fn(async () => undefined),
    } as unknown as Locator;
    const first = {
      count: vi.fn(async () => 1),
      normalize: vi.fn(async () => sharedSemantic),
      click: vi.fn(async () => undefined),
    } as unknown as Locator;
    const second = {
      count: vi.fn(async () => 1),
      normalize: vi.fn(async () => sharedSemantic),
      click: vi.fn(async () => undefined),
    } as unknown as Locator;
    const sameName = record('', 'e1');
    sameName.name = '详情';
    sameName.semanticName = '详情';
    const secondRecord = { ...sameName, playwrightRef: 'e2' };
    const page = {
      locator: vi.fn((selector: string) => {
        if (selector === 'aria-ref=e1') return first;
        if (selector === 'aria-ref=e2') return second;
        throw new Error(`unexpected selector ${selector}`);
      }),
    } as unknown as Page;
    const ctx: ActionContext = {
      page,
      refs: new Map([
        ['@e1', sameName],
        ['@e2', secondRecord],
      ]),
      hash: (value) => value,
      timeoutMs: 15_000,
    };

    await click(ctx, '@e2');

    expect(second.click).toHaveBeenCalledOnce();
    expect(first.click).not.toHaveBeenCalled();
    expect(sharedSemantic.click).not.toHaveBeenCalled();
    expect(first.normalize).not.toHaveBeenCalled();
    expect(second.normalize).not.toHaveBeenCalled();
  });

  it('delegates strict endpoint resolution and actionability to one Locator.dragTo', async () => {
    const { ctx, start, end } = actionFixture();

    await drag(ctx, '@e1', '@e2');

    expect(start.normalize).not.toHaveBeenCalled();
    expect(end.normalize).not.toHaveBeenCalled();
    expect(start.count).not.toHaveBeenCalled();
    expect(end.count).not.toHaveBeenCalled();
    expect(start.hover).not.toHaveBeenCalled();
    expect(end.hover).not.toHaveBeenCalled();
    expect(start.dragTo).toHaveBeenCalledWith(end, {
      timeout: 12_345,
    });
  });

  it('forwards fractional drag endpoint coordinates without rounding or dropping them', async () => {
    const { ctx, start, end } = actionFixture();

    await drag(ctx, '@e1', '@e2', {
      sourcePosition: { x: 12.25, y: 3.5 },
      targetPosition: { x: 40.75, y: 91.125 },
    });

    expect(start.dragTo).toHaveBeenCalledWith(end, {
      timeout: 12_345,
      sourcePosition: { x: 12.25, y: 3.5 },
      targetPosition: { x: 40.75, y: 91.125 },
    });
  });

  it('classifies drag failures after dispatch as uncertain and partial', async () => {
    const { ctx, start } = actionFixture();
    vi.mocked(start.dragTo).mockRejectedValue(new Error('lost after mousedown'));

    const error = await drag(ctx, '@e1', '@e2').catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      code: 'input_uncertain',
      phase: 'dispatching',
      uncertain: true,
      partial: true,
    });
  });

  it('uses pressSequentially for slowly type and can submit on the same locator', async () => {
    const { ctx, start } = actionFixture();

    await fill(ctx, '@e1', 'hello', { slowly: true, submit: true });

    expect(start.fill).not.toHaveBeenCalled();
    expect(start.pressSequentially).toHaveBeenCalledWith('hello', { timeout: 12_345 });
    expect(start.press).toHaveBeenCalledWith('Enter', {
      timeout: 12_345,
    });
  });

  it('passes an empty option value through to Playwright unchanged', async () => {
    const { ctx, start } = actionFixture();

    await expect(selectOption(ctx, '@e1', [''])).resolves.toEqual(['']);

    expect(start.selectOption).toHaveBeenCalledWith([''], {
      timeout: 12_345,
    });
  });

  it('matches upstream completion boundaries for ordinary non-navigating actions', async () => {
    const { ctx, start, page } = actionFixture();

    await hover(ctx, '@e1');
    await fill(ctx, '@e1', 'plain', { submit: false, slowly: false });
    await fillForm(ctx, [
      { type: 'textbox', ref: '@e1', value: 'batch' },
      {
        type: 'combobox',
        ref: '@e1',
        value: '',
        selectBy: 'value',
      },
      { type: 'checkbox', ref: '@e1', value: true },
    ]);
    await selectOption(ctx, '@e1', []);
    await setChecked(ctx, '@e1', false);
    await press(ctx, 'ControlOrMeta+A', undefined);
    await press(ctx, 'ArrowDown', '@e1');

    expect(page.waitForTimeout).not.toHaveBeenCalled();
    expect(start.hover).toHaveBeenCalledWith({ timeout: 12_345 });
    expect(start.fill).toHaveBeenCalledWith('plain', { timeout: 12_345 });
    expect(start.selectOption).toHaveBeenCalledWith([], { timeout: 12_345 });
    expect(start.setChecked).toHaveBeenCalledWith(false, { timeout: 12_345 });
  });

  it('keeps completion for click/drag/drop/slow/submit/Enter/evaluate boundaries', async () => {
    const { ctx, page } = actionFixture();

    await drag(ctx, '@e1', '@e2');
    await drop(ctx, '@e1', { data: {} });
    await mouseClick(ctx, 10, 20);
    await mouseDrag(ctx, 10, 20, 30, 40);
    await fill(ctx, '@e1', 'slow', { submit: false, slowly: true });
    await fill(ctx, '@e1', 'submit', { submit: true, slowly: false });
    await press(ctx, 'Enter', undefined);
    await press(ctx, 'Enter', '@e1');
    await withActionCompletion(ctx, async () => 42);

    const completionSettles = vi.mocked(page.waitForTimeout).mock.calls
      .filter(([milliseconds]) => milliseconds === 500);
    expect(completionSettles).toHaveLength(9);
  });

  it('forwards the complete official coordinate mouse and resize surface', async () => {
    const { ctx, page } = actionFixture();

    await mouseMove(ctx, -12.5, 1_000_000.25);
    await mouseDown(ctx, 'right');
    await mouseUp(ctx, 'middle');
    await mouseWheel(ctx, 0, 0);
    await mouseClick(ctx, 12.25, -4.5, {
      button: 'right',
      clickCount: 4,
      delayMs: 5_001.5,
    });
    await mouseDrag(ctx, -1, -2, 1_000_003, 4.25);
    await resize(ctx, 963.5, 707.25);

    expect(page.mouse.move).toHaveBeenNthCalledWith(1, -12.5, 1_000_000.25);
    expect(page.mouse.down).toHaveBeenNthCalledWith(1, { button: 'right' });
    expect(page.mouse.up).toHaveBeenNthCalledWith(1, { button: 'middle' });
    expect(page.mouse.wheel).toHaveBeenCalledWith(0, 0);
    expect(page.mouse.click).toHaveBeenCalledWith(12.25, -4.5, {
      button: 'right',
      clickCount: 4,
      delay: 5_001.5,
    });
    expect(page.mouse.move).toHaveBeenNthCalledWith(2, -1, -2);
    expect(page.mouse.down).toHaveBeenNthCalledWith(2);
    expect(page.mouse.move).toHaveBeenNthCalledWith(3, 1_000_003, 4.25);
    expect(page.mouse.up).toHaveBeenNthCalledWith(2);
    expect(page.setViewportSize).toHaveBeenCalledWith({
      width: 963.5,
      height: 707.25,
    });
  });

  it('replays a border-box-relative pointer trajectory with float timing and modifiers', async () => {
    const { ctx, start, page } = actionFixture();

    await pointerGesture(ctx, '@e1', {
      button: 'left',
      modifiers: ['Control', 'Shift'],
      start: { x: -1.25, y: 2.5 },
      points: [
        { x: 3.75, y: 4.125, elapsedMs: 5.5 },
        { x: 9, y: 8, elapsedMs: 12 },
      ],
    });

    expect(start.boundingBox).toHaveBeenCalledOnce();
    expect(page.keyboard.down).toHaveBeenNthCalledWith(1, 'Control');
    expect(page.keyboard.down).toHaveBeenNthCalledWith(2, 'Shift');
    expect(page.mouse.move).toHaveBeenNthCalledWith(1, 99.25, 202.75);
    expect(page.mouse.down).toHaveBeenCalledWith({ button: 'left' });
    expect(page.mouse.move).toHaveBeenNthCalledWith(2, 104.25, 204.375);
    expect(page.mouse.move).toHaveBeenNthCalledWith(3, 109.5, 208.25);
    expect(page.mouse.up).toHaveBeenCalledWith({ button: 'left' });
    expect(page.keyboard.up).toHaveBeenNthCalledWith(1, 'Shift');
    expect(page.keyboard.up).toHaveBeenNthCalledWith(2, 'Control');
    expect(page.waitForTimeout).toHaveBeenNthCalledWith(1, 5.5);
    expect(page.waitForTimeout).toHaveBeenNthCalledWith(2, 6.5);
    expect(page.waitForTimeout).toHaveBeenNthCalledWith(3, 500);
    expect(
      vi.mocked(page.keyboard.up).mock.invocationCallOrder.at(-1),
    ).toBeLessThan(
      vi.mocked(page.waitForTimeout).mock.invocationCallOrder.at(-1) ?? 0,
    );
  });

  it('best-effort releases the pointer button and modifiers after a move failure', async () => {
    const { ctx, page } = actionFixture();
    vi.mocked(page.mouse.move)
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('CDP move failed after dispatch'));

    await expect(pointerGesture(ctx, '@e1', {
      button: 'right',
      modifiers: ['Alt', 'Meta'],
      start: { x: 1, y: 2 },
      points: [{ x: -10.5, y: 20.25, elapsedMs: 3 }],
    })).rejects.toBeInstanceOf(ActionError);

    expect(page.mouse.up).toHaveBeenCalledWith({ button: 'right' });
    expect(page.keyboard.up).toHaveBeenNthCalledWith(1, 'Meta');
    expect(page.keyboard.up).toHaveBeenNthCalledWith(2, 'Alt');
  });

  it('replays pen samples through a public CDPSession without mouse fallback', async () => {
    const { ctx, page, cdpSend, cdpDetach } = actionFixture();

    await pointerGesture(ctx, '@e1', {
      pointerType: 'pen',
      button: 'right',
      modifiers: ['Alt'],
      start: {
        x: 1,
        y: 2,
        pressure: 0.25,
        tangentialPressure: -0.4,
        tiltX: 11,
        tiltY: -12,
        twist: 33,
        width: 8,
        height: 6,
      },
      points: [{
        x: 9,
        y: 10,
        elapsedMs: 4.5,
        pressure: 0.75,
        tangentialPressure: 0.2,
        tiltX: 21,
        tiltY: -22,
        twist: 44,
        width: 9,
        height: 7,
      }],
    });

    expect(page.context().newCDPSession).toHaveBeenCalledWith(page);
    expect(page.mouse.move).not.toHaveBeenCalled();
    expect(page.mouse.down).not.toHaveBeenCalled();
    expect(page.mouse.up).not.toHaveBeenCalled();
    expect(cdpSend).toHaveBeenNthCalledWith(
      1,
      'Input.dispatchMouseEvent',
      expect.objectContaining({
        type: 'mouseMoved',
        x: 101.5,
        y: 202.25,
        modifiers: 1,
        button: 'none',
        buttons: 0,
        pointerType: 'pen',
        force: 0.25,
        tangentialPressure: -0.4,
        tiltX: 11,
        tiltY: -12,
        twist: 33,
      }),
    );
    expect(cdpSend).toHaveBeenNthCalledWith(
      2,
      'Input.dispatchMouseEvent',
      expect.objectContaining({
        type: 'mousePressed',
        button: 'right',
        buttons: 2,
        pointerType: 'pen',
        force: 0.25,
      }),
    );
    expect(cdpSend).toHaveBeenNthCalledWith(
      3,
      'Input.dispatchMouseEvent',
      expect.objectContaining({
        type: 'mouseMoved',
        x: 109.5,
        y: 210.25,
        button: 'none',
        buttons: 2,
        pointerType: 'pen',
        force: 0.75,
        tiltX: 21,
        tiltY: -22,
        twist: 44,
      }),
    );
    expect(cdpSend).toHaveBeenNthCalledWith(
      4,
      'Input.dispatchMouseEvent',
      expect.objectContaining({
        type: 'mouseReleased',
        x: 109.5,
        y: 210.25,
        button: 'right',
        buttons: 0,
        pointerType: 'pen',
        force: 0,
      }),
    );
    // CDP has no pen contact width/height input fields; they remain preserved
    // in the trace but must not be invented as unrelated protocol fields.
    expect(cdpSend.mock.calls.some((call) => (
      'width' in (call[1] as Record<string, unknown>)
      || 'height' in (call[1] as Record<string, unknown>)
    ))).toBe(false);
    expect(cdpDetach).toHaveBeenCalledOnce();
  });

  it('replays one primary touch with contact geometry and a clean touchEnd', async () => {
    const { ctx, page, cdpSend, cdpDetach } = actionFixture();

    await pointerGesture(ctx, '@e1', {
      pointerType: 'touch',
      button: 'left',
      modifiers: ['Control', 'Shift'],
      start: {
        x: 3,
        y: 4,
        pressure: 0.6,
        tangentialPressure: -0.25,
        tiltX: 5,
        tiltY: -6,
        twist: 17,
        width: 12,
        height: 10,
      },
      points: [
        {
          x: 13,
          y: 14,
          elapsedMs: 3,
          pressure: 0.8,
          width: 14,
          height: 8,
        },
        {
          x: 20,
          y: 22,
          elapsedMs: 8,
          pressure: 0,
          width: 16,
          height: 6,
        },
      ],
    });

    expect(page.mouse.move).not.toHaveBeenCalled();
    expect(cdpSend).toHaveBeenNthCalledWith(
      1,
      'Input.dispatchTouchEvent',
      {
        type: 'touchStart',
        modifiers: 10,
        touchPoints: [{
          x: 103.5,
          y: 204.25,
          id: 1,
          force: 0.6,
          radiusX: 6,
          radiusY: 5,
          tangentialPressure: -0.25,
          tiltX: 5,
          tiltY: -6,
          twist: 17,
        }],
      },
    );
    expect(cdpSend).toHaveBeenNthCalledWith(
      2,
      'Input.dispatchTouchEvent',
      expect.objectContaining({
        type: 'touchMove',
        modifiers: 10,
        touchPoints: [expect.objectContaining({
          x: 113.5,
          y: 214.25,
          id: 1,
          force: 0.8,
          radiusX: 7,
          radiusY: 4,
        })],
      }),
    );
    expect(cdpSend).toHaveBeenNthCalledWith(
      3,
      'Input.dispatchTouchEvent',
      expect.objectContaining({
        type: 'touchMove',
        touchPoints: [expect.objectContaining({
          x: 120.5,
          y: 222.25,
          id: 1,
          // pointerup samples record pressure=0; the active endpoint move must
          // retain contact until the following touchEnd releases it.
          force: 0.8,
          radiusX: 8,
          radiusY: 3,
        })],
      }),
    );
    expect(cdpSend).toHaveBeenNthCalledWith(
      4,
      'Input.dispatchTouchEvent',
      { type: 'touchEnd', modifiers: 10, touchPoints: [] },
    );
    expect(cdpSend.mock.calls.some((call) => call[1]?.type === 'touchCancel'))
      .toBe(false);
    expect(cdpDetach).toHaveBeenCalledOnce();
  });

  it('cancels an uncertain touch and detaches its CDPSession after failure', async () => {
    const { ctx, page, cdpSend, cdpDetach } = actionFixture();
    cdpSend.mockImplementation(async (
      _method: string,
      params: Record<string, unknown>,
    ) => {
      if (params.type === 'touchMove') {
        throw new Error('touch move failed after dispatch');
      }
      return {};
    });

    await expect(pointerGesture(ctx, '@e1', {
      pointerType: 'touch',
      button: 'left',
      modifiers: ['Meta'],
      start: { x: 1, y: 2 },
      points: [{ x: 3, y: 4, elapsedMs: 1 }],
    })).rejects.toBeInstanceOf(ActionError);

    expect(cdpSend).toHaveBeenLastCalledWith(
      'Input.dispatchTouchEvent',
      { type: 'touchCancel', touchPoints: [] },
    );
    expect(page.keyboard.up).toHaveBeenCalledWith('Meta');
    expect(cdpDetach).toHaveBeenCalledOnce();
  });

  it('lets a retained dialog operation own its pen cleanup until it settles', async () => {
    const { ctx, page, cdpSend, cdpDetach } = actionFixture();
    const emitter = new EventEmitter();
    Object.assign(page as object, {
      on: emitter.on.bind(emitter),
      off: emitter.off.bind(emitter),
    });
    let releasePressed!: () => void;
    const pressedGate = new Promise<void>((resolve) => {
      releasePressed = resolve;
    });
    cdpSend.mockImplementation(async (
      _method: string,
      params: Record<string, unknown>,
    ) => {
      if (params.type === 'mousePressed') {
        emitter.emit('dialog', {});
        await pressedGate;
      }
      return {};
    });
    let retained: Promise<void> | undefined;
    ctx.onModalActionPending = (pending) => {
      retained = pending;
    };

    await expect(pointerGesture(ctx, '@e1', {
      pointerType: 'pen',
      button: 'left',
      modifiers: ['Alt'],
      start: { x: 1, y: 2, pressure: 0.3 },
      points: [{ x: 3, y: 4, elapsedMs: 1, pressure: 0 }],
    })).rejects.toMatchObject({ code: 'dialog_pending' });

    expect(retained).toBeDefined();
    expect(cdpDetach).not.toHaveBeenCalled();
    expect(page.keyboard.up).not.toHaveBeenCalled();
    releasePressed();
    await retained;
    expect(cdpSend.mock.calls.some((call) => call[1]?.type === 'mouseReleased'))
      .toBe(true);
    expect(page.keyboard.up).toHaveBeenCalledWith('Alt');
    expect(cdpDetach).toHaveBeenCalledOnce();
  });

  it('preserves file/data/combined/explicit-empty drop payloads exactly', async () => {
    const { ctx, start } = actionFixture();

    await drop(ctx, '@e1', { files: ['/tmp/one.txt'] });
    await drop(ctx, '@e1', {
      files: ['/tmp/one.txt', '/tmp/two.txt'],
      data: { 'text/plain': 'hello', 'text/uri-list': '--literal-value' },
    });
    await drop(ctx, '@e1', { data: {} });

    expect(start.drop).toHaveBeenNthCalledWith(
      1,
      { files: '/tmp/one.txt' },
      { timeout: 12_345 },
    );
    expect(start.drop).toHaveBeenNthCalledWith(
      2,
      {
        files: ['/tmp/one.txt', '/tmp/two.txt'],
        data: { 'text/plain': 'hello', 'text/uri-list': '--literal-value' },
      },
      { timeout: 12_345 },
    );
    expect(start.drop).toHaveBeenCalledTimes(2);
    expect(start.evaluate).toHaveBeenCalledWith(
      expect.any(Function),
      undefined,
      { timeout: 12_345 },
    );
  });

  it('supports page-level press plus explicit keydown/keyup without a ref', async () => {
    const { ctx, page } = actionFixture();

    await press(ctx, 'Enter', undefined);
    await press(ctx, 'ControlOrMeta+A', undefined);
    await keyDown(ctx, 'Shift');
    await keyUp(ctx, 'Shift');

    expect(page.keyboard.press).toHaveBeenNthCalledWith(1, 'Enter');
    expect(page.keyboard.press).toHaveBeenNthCalledWith(2, 'ControlOrMeta+A');
    expect(page.keyboard.down).toHaveBeenCalledWith('Shift');
    expect(page.keyboard.up).toHaveBeenCalledWith('Shift');
  });

  it('delegates upload validation directly to Locator.setInputFiles', async () => {
    const { ctx, start } = actionFixture();

    await upload(ctx, '@e1', ['/tmp/a.txt']);

    expect(start.setInputFiles).toHaveBeenCalledWith(['/tmp/a.txt'], {
      timeout: 12_345,
    });
  });

  it('uses an empty direct upload list to clear the exact file input', async () => {
    const { ctx, start } = actionFixture();

    await upload(ctx, '@e1', []);

    expect(start.setInputFiles).toHaveBeenCalledWith([], {
      timeout: 12_345,
    });
  });

  it('completes or cancels a pending browser-native FileChooser without a ref', async () => {
    const { ctx } = actionFixture();
    const chooser = {
      setFiles: vi.fn(async () => undefined),
      isMultiple: vi.fn(() => true),
    } as unknown as FileChooser;

    await uploadFileChooser(ctx, chooser, ['/tmp/a.txt', '/tmp/b.txt']);
    expect(chooser.setFiles).toHaveBeenCalledWith(
      ['/tmp/a.txt', '/tmp/b.txt'],
      { timeout: 12_345 },
    );

    vi.mocked(chooser.setFiles).mockClear();
    await uploadFileChooser(ctx, chooser, undefined);
    expect(chooser.setFiles).not.toHaveBeenCalled();
  });

  it('classifies local upload payload failures as deterministic pre-dispatch input errors', async () => {
    const { ctx, start } = actionFixture();
    vi.mocked(start.setInputFiles).mockRejectedValueOnce(
      new Error("ENOENT: no such file or directory, stat '/tmp/missing.txt'"),
    );

    await expect(upload(ctx, '@e1', ['/tmp/missing.txt'])).rejects.toMatchObject({
      code: 'invalid_upload',
      phase: 'pre_dispatch',
      uncertain: false,
      partial: false,
    });
  });

  it('uses Playwright history/reload APIs with bounded navigation waits', async () => {
    const { ctx, page } = actionFixture();

    await goBack(ctx);
    await goForward(ctx);
    await reload(ctx);

    expect(page.goBack).toHaveBeenCalledWith({ waitUntil: 'commit', timeout: 12_345 });
    expect(page.goForward).toHaveBeenCalledWith({ waitUntil: 'commit', timeout: 12_345 });
    expect(page.reload).toHaveBeenCalledWith({ timeout: 12_345 });
  });

  it.each([
    ['back', goBack, 'goBack'],
    ['forward', goForward, 'goForward'],
  ] as const)('reports no_history when Playwright returns null for %s', async (
    _name,
    action,
    method,
  ) => {
    const { ctx, page } = actionFixture();
    vi.mocked(page[method]).mockResolvedValue(null);

    const error = await action(ctx).catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      code: 'no_history',
      phase: 'pre_dispatch',
      uncertain: false,
      partial: false,
    });
  });

  it('waits for bounded time, disappearing text, then visible text', async () => {
    const { ctx, page } = actionFixture();
    const calls: string[] = [];
    vi.mocked(page.waitForTimeout).mockImplementation(async () => {
      calls.push('time');
    });
    vi.mocked(page.getByText).mockImplementation((text: string) => ({
      first: () => ({
        waitFor: async (options: { state: string }) => {
          calls.push(`${text}:${options.state}`);
        },
      }),
    }) as ReturnType<Page['getByText']>);

    await waitFor(ctx, {
      timeSeconds: 0.25,
      textGone: 'Loading',
      text: 'Ready',
    });

    expect(page.waitForTimeout).toHaveBeenCalledWith(250);
    expect(calls).toEqual(['time', 'Loading:hidden', 'Ready:visible']);
  });

  it('truncates an over-budget time wait and does not dispatch later text waits', async () => {
    const { ctx, page } = actionFixture();
    const now = vi.spyOn(Date, 'now').mockReturnValue(10_000);
    try {
      ctx.deadlineAt = 10_125;

      await expect(waitFor(ctx, {
        timeSeconds: 30,
        textGone: 'Loading',
        text: 'Ready',
      })).rejects.toMatchObject({
        code: 'command_timeout',
        phase: 'pre_dispatch',
        uncertain: false,
        partial: false,
      });

      expect(page.waitForTimeout).toHaveBeenCalledWith(125);
      expect(page.getByText).not.toHaveBeenCalled();
    } finally {
      now.mockRestore();
    }
  });

  it('reports text wait timeout as a non-mutating wait_timeout', async () => {
    const { ctx, page } = actionFixture();
    vi.mocked(page.getByText).mockReturnValue({
      first: () => ({
        waitFor: async () => {
          throw new Error('Timeout 12345ms exceeded while waiting for locator');
        },
      }),
    } as ReturnType<Page['getByText']>);

    const error = await waitFor(ctx, { text: 'Never' })
      .catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      code: 'wait_timeout',
      phase: 'pre_dispatch',
      uncertain: false,
      partial: false,
    });
  });

  it('keeps a strict unique locate executable when optional metadata probing fails', async () => {
    const { ctx, start } = actionFixture();
    // This minimal locator intentionally has no evaluate/ariaSnapshot methods,
    // so rich fingerprint collection fails after count() proved uniqueness.
    const located = await locateBySelector(
      ctx,
      '@s1',
      '#start',
      (value) => `hash:${value}`,
    );

    expect(start.count).toHaveBeenCalledOnce();
    expect(located).toMatchObject({
      selector: '#start',
      role: 'generic',
      name: '',
      actionKind: 'activate',
      fieldTier: 'plain',
      documentURL: '',
      securityKey: '@s1',
      security: '',
    });
    expect(ctx.refs.get('@s1')).toBe(located);
  });
});
