/**
 * Playwright-compatible execution of `browser_run_code_unsafe`.
 *
 * This deliberately runs in the Electron main process: the public Playwright
 * Page object cannot be cloned into a renderer.  The VM context exposes only
 * `page` and the private completion primitive used by upstream Playwright MCP;
 * CommonJS/Node globals are not added to the context.
 *
 * Node's default unhandled-rejection mode can terminate the Host. A single
 * process listener uses AsyncLocalStorage to attribute a rejection to the
 * snippet or retained callback that created its async resource. Rejections
 * observed while a snippet is active fail only that snippet; later callback
 * failures remain isolated to their originating context so the browser Host
 * stays available for subsequent commands.
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import { createContext, Script } from 'node:vm';

import type { Page } from './playwright-compat';

type CompletionBoundary = <T>(action: () => Promise<T>) => Promise<T>;

interface RunCodeRejectionContext {
  onUnhandledRejection: (reason: unknown) => void;
}

const rejectionContext = new AsyncLocalStorage<RunCodeRejectionContext>();

process.prependListener('unhandledRejection', (reason: unknown) => {
  // AsyncLocalStorage follows the async resource that created the rejected
  // promise, including promises/timers created inside a node:vm Context.
  // Therefore a route callback installed by run A can never reject run B just
  // because both snippets happen to be active at the same time.
  rejectionContext.getStore()?.onUnhandledRejection(reason);
});

class Deferred<T> {
  readonly promise: Promise<T>;
  private settled = false;
  private readonly resolvePromise: (value: T | PromiseLike<T>) => void;
  private readonly rejectPromise: (reason?: unknown) => void;

  constructor() {
    let resolvePromise!: (value: T | PromiseLike<T>) => void;
    let rejectPromise!: (reason?: unknown) => void;
    this.promise = new Promise<T>((resolve, reject) => {
      resolvePromise = resolve;
      rejectPromise = reject;
    });
    this.resolvePromise = resolvePromise;
    this.rejectPromise = rejectPromise;
  }

  resolve(value: T): void {
    if (this.settled) return;
    this.settled = true;
    this.resolvePromise(value);
  }

  reject(reason: unknown): void {
    if (this.settled) return;
    this.settled = true;
    this.rejectPromise(normalizedError(reason));
  }

  isDone(): boolean {
    return this.settled;
  }
}

function normalizedError(reason: unknown): Error {
  if (reason instanceof Error) return reason;
  if (
    reason
    && typeof reason === 'object'
    && typeof (reason as { message?: unknown }).message === 'string'
  ) {
    const error = new Error((reason as { message: string }).message);
    const stack = (reason as { stack?: unknown }).stack;
    if (typeof stack === 'string') error.stack = stack;
    return error;
  }
  return new Error(String(reason));
}

export class RunCodeTimeoutError extends Error {
  constructor() {
    super('Playwright 代码超过命令截止时间');
    this.name = 'RunCodeTimeoutError';
  }
}

class RunCodeRevokedError extends Error {
  constructor() {
    super('Playwright 代码已结束，不能继续调用先前保留的浏览器对象');
    this.name = 'RunCodeRevokedError';
  }
}

export interface RunCodeOptions {
  /** Absolute wall-clock deadline shared with the enclosing Host command. */
  deadlineAt: number;
  /** Used only for VM syntax/runtime stack locations. */
  filename?: string;
  /** Playwright MCP's request/navigation completion boundary. */
  withCompletion: CompletionBoundary;
  /**
   * Deterministically destroys renderer work that was already dispatched when
   * the deadline fired. BrowserHost implements this by replacing the current
   * document, which cancels evaluate timers and Locator actionability tasks
   * before the timeout is returned to Python.
   */
  onTimeout?: () => Promise<void>;
}

interface Registration {
  target: object;
  kind: 'event' | 'route' | 'websocket-route';
  key: unknown;
  original: (...args: unknown[]) => unknown;
  wrapped: (...args: unknown[]) => unknown;
  cleanup: () => void | Promise<void>;
  installed: boolean;
  removed: boolean;
}

interface SettledOperation {
  ok: boolean;
  value?: unknown;
  error?: unknown;
}

const NON_DISPATCHING_METHODS = new Set([
  'and',
  'browser',
  'context',
  'filter',
  'first',
  'frame',
  'frameLocator',
  'frames',
  'getByAltText',
  'getByLabel',
  'getByPlaceholder',
  'getByRole',
  'getByTestId',
  'getByText',
  'getByTitle',
  'isClosed',
  'last',
  'locator',
  'mainFrame',
  'nth',
  'or',
  'owner',
  'page',
  'pages',
  'waitForTimeout',
  'url',
]);

const EVENT_ADD_METHODS = new Set([
  'addListener',
  'on',
  'once',
  'prependListener',
  'prependOnceListener',
]);

const EVENT_REMOVE_METHODS = new Set([
  'off',
  'removeListener',
]);

const ROUTE_ADD_METHODS = new Set([
  'route',
]);

const ROUTE_REMOVE_METHODS = new Set([
  'unroute',
]);

const WEBSOCKET_ROUTE_ADD_METHODS = new Set([
  'routeWebSocket',
]);

const LOCATOR_TIMEOUT_OPTION_INDEX = new Map<string, number>([
  ['blur', 0],
  ['check', 0],
  ['click', 0],
  ['dblclick', 0],
  ['dragTo', 1],
  ['fill', 1],
  ['focus', 0],
  ['hover', 0],
  ['innerHTML', 0],
  ['innerText', 0],
  ['inputValue', 0],
  ['press', 1],
  ['pressSequentially', 1],
  ['screenshot', 0],
  ['scrollIntoViewIfNeeded', 0],
  ['selectOption', 1],
  ['selectText', 0],
  ['setChecked', 1],
  ['setInputFiles', 1],
  ['tap', 0],
  ['textContent', 0],
  ['type', 1],
  ['uncheck', 0],
  ['waitFor', 0],
]);

const PAGE_OR_FRAME_TIMEOUT_OPTION_INDEX = new Map<string, number>([
  ['check', 1],
  ['click', 1],
  ['dblclick', 1],
  ['dispatchEvent', 2],
  ['fill', 2],
  ['focus', 1],
  ['goto', 1],
  ['hover', 1],
  ['innerHTML', 1],
  ['innerText', 1],
  ['inputValue', 1],
  ['press', 2],
  ['reload', 0],
  ['selectOption', 2],
  ['setChecked', 2],
  ['setInputFiles', 2],
  ['tap', 1],
  ['textContent', 1],
  ['type', 2],
  ['uncheck', 1],
  ['waitForFunction', 2],
  ['waitForLoadState', 1],
  ['waitForSelector', 1],
  ['waitForURL', 1],
]);

function isProxyCandidate(value: unknown): value is object {
  if (!value || (typeof value !== 'object' && typeof value !== 'function')) {
    return false;
  }
  if (
    Buffer.isBuffer(value)
    || value instanceof ArrayBuffer
    || ArrayBuffer.isView(value)
    || value instanceof Date
    || value instanceof RegExp
  ) {
    return false;
  }
  if (Array.isArray(value)) return true;
  const record = value as Record<PropertyKey, unknown>;
  if (
    typeof record.on === 'function'
    || typeof record.locator === 'function'
    || typeof record.evaluate === 'function'
    || typeof record.click === 'function'
    || typeof record.close === 'function'
    || typeof record.newPage === 'function'
    || typeof record.fulfill === 'function'
  ) {
    return true;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype !== null && prototype !== Object.prototype;
}

/**
 * Deep, identity-stable façade over Playwright client objects.
 *
 * It is not an action policy: every public method remains available while the
 * run is live, and successful long-lived callbacks stay live. Revocation only
 * happens when the snippet fails or times out, so a retained Page/Locator/
 * Context cannot dispatch work after its command has already returned.
 */
class RevocablePlaywrightFacade {
  private readonly proxies = new WeakMap<object, object>();
  private readonly originals = new WeakMap<object, object>();
  private readonly methods = new WeakMap<object, Map<PropertyKey, (...args: unknown[]) => unknown>>();
  private readonly registrations: Registration[] = [];
  private readonly inFlight = new Set<Promise<SettledOperation>>();
  private revoked = false;
  private dispatched = false;

  constructor(
    private readonly runContext: RunCodeRejectionContext,
    private readonly deadlineAt: number,
  ) {}

  root<T extends object>(value: T): T {
    return this.wrapObject(value, true) as T;
  }

  hasDispatchedWork(): boolean {
    return this.dispatched;
  }

  revoke(): void {
    this.revoked = true;
  }

  private assertUsable(): void {
    if (this.revoked) throw new RunCodeRevokedError();
  }

  private unwrap(value: unknown): unknown {
    if (!value || (typeof value !== 'object' && typeof value !== 'function')) {
      return value;
    }
    return this.originals.get(value as object) ?? value;
  }

  private wrapCallback(
    callback: (...args: unknown[]) => unknown,
    revokedFallback?: (...args: unknown[]) => unknown,
  ): (...args: unknown[]) => unknown {
    const runContext = this.runContext;
    const isRevoked = (): boolean => this.revoked;
    const assertUsable = (): void => this.assertUsable();
    const wrapValue = (value: unknown): unknown => this.wrapValue(value);
    return function(this: unknown, ...args: unknown[]): unknown {
      // Playwright currently has no public unrouteWebSocket(). A handler from
      // a failed/timed-out snippet therefore cannot be physically removed.
      // Keep that unavoidable client registration inert by restoring the
      // official default behavior: connect the intercepted socket to its real
      // server. Never hand a revoked façade back to the stale user callback.
      if (isRevoked() && revokedFallback) {
        return Reflect.apply(revokedFallback, this, args);
      }
      return rejectionContext.run(
        runContext,
        () => {
          assertUsable();
          return Reflect.apply(
            callback,
            wrapValue(this),
            args.map(wrapValue),
          );
        },
      );
    };
  }

  private matchingRegistration(
    target: object,
    kind: Registration['kind'],
    key: unknown,
    original: unknown,
  ): Registration | undefined {
    return [...this.registrations].reverse().find((registration) => (
      registration.target === target
      && registration.kind === kind
      && registration.key === key
      && registration.original === original
      && !registration.removed
    ));
  }

  private prepareArguments(
    target: object,
    method: string,
    input: unknown[],
  ): {
    args: unknown[];
    registration?: Registration;
  } {
    const args = input.map((value) => this.unwrap(value));
    if (
      (
        EVENT_ADD_METHODS.has(method)
        || ROUTE_ADD_METHODS.has(method)
        || WEBSOCKET_ROUTE_ADD_METHODS.has(method)
      )
      && typeof input[1] === 'function'
    ) {
      const kind: Registration['kind'] = EVENT_ADD_METHODS.has(method)
        ? 'event'
        : WEBSOCKET_ROUTE_ADD_METHODS.has(method)
          ? 'websocket-route'
          : 'route';
      const original = input[1] as (...args: unknown[]) => unknown;
      const wrapped = this.wrapCallback(
        original,
        kind === 'websocket-route'
          ? (route: unknown): unknown => {
              if (!route || (typeof route !== 'object' && typeof route !== 'function')) {
                return undefined;
              }
              const connectToServer = (
                route as { connectToServer?: unknown }
              ).connectToServer;
              if (typeof connectToServer !== 'function') return undefined;
              return Reflect.apply(connectToServer, route, []);
            }
          : undefined,
      );
      args[1] = wrapped;
      const cleanup = kind === 'event'
        ? () => {
            const off = (target as { off?: unknown }).off;
            if (typeof off === 'function') {
              Reflect.apply(off, target, [input[0], wrapped]);
            }
          }
        : kind === 'route'
          ? async () => {
            const unroute = (target as { unroute?: unknown }).unroute;
            if (typeof unroute === 'function') {
              await Reflect.apply(unroute, target, [input[0], wrapped]);
            }
          }
          : () => undefined;
      return {
        args,
        registration: {
          target,
          kind,
          key: input[0],
          original,
          wrapped,
          cleanup,
          installed: false,
          removed: false,
        },
      };
    }
    if (
      (EVENT_REMOVE_METHODS.has(method) || ROUTE_REMOVE_METHODS.has(method))
      && typeof input[1] === 'function'
    ) {
      const kind = EVENT_REMOVE_METHODS.has(method) ? 'event' : 'route';
      const registration = this.matchingRegistration(
        target,
        kind,
        input[0],
        input[1],
      );
      if (registration) {
        args[1] = registration.wrapped;
        registration.removed = true;
      }
    }
    return { args };
  }

  private bindPlaywrightTimeout(
    target: object,
    method: string,
    args: unknown[],
  ): {
    args: unknown[];
    deadlineBound: boolean;
  } {
    const isLocator = typeof (target as { count?: unknown }).count === 'function';
    const isPageOrFrame = (
      typeof (target as { locator?: unknown }).locator === 'function'
      && (
        typeof (target as { context?: unknown }).context === 'function'
        || typeof (target as { page?: unknown }).page === 'function'
      )
    );
    const optionIndex = isLocator
      ? LOCATOR_TIMEOUT_OPTION_INDEX.get(method)
      : isPageOrFrame
        ? PAGE_OR_FRAME_TIMEOUT_OPTION_INDEX.get(method)
        : undefined;
    if (optionIndex === undefined) {
      return { args, deadlineBound: false };
    }
    // Fire the Playwright action timeout just before the outer command timer.
    // This cancels Locator actionability retry loops instead of letting them
    // survive a recovery navigation and click a newly loaded document.
    const remaining = Math.max(1, Math.floor(this.deadlineAt - Date.now()) - 25);
    const current = args[optionIndex];
    const currentOptions = (
      current && typeof current === 'object' && !Array.isArray(current)
        ? current as Record<string, unknown>
        : {}
    );
    const requested = currentOptions.timeout;
    const timeout = (
      typeof requested === 'number'
      && Number.isFinite(requested)
      && requested > 0
    )
      ? Math.min(requested, remaining)
      : remaining;
    const bounded = [...args];
    while (bounded.length < optionIndex) bounded.push(undefined);
    bounded[optionIndex] = { ...currentOptions, timeout };
    return { args: bounded, deadlineBound: true };
  }

  private async installRegistration(registration: Registration): Promise<void> {
    registration.installed = true;
    this.registrations.push(registration);
    if (this.revoked && !registration.removed) {
      registration.removed = true;
      await Promise.resolve(registration.cleanup()).catch(() => undefined);
    }
  }

  private async disposeLateResult(
    method: string,
    result: unknown,
  ): Promise<void> {
    // Context.newPage may have crossed the deadline while Chromium was
    // creating the target. Never publish that late target into later commands.
    if (method !== 'newPage' || !result || typeof result !== 'object') return;
    const close = (result as { close?: unknown }).close;
    if (typeof close === 'function') {
      await Reflect.apply(close, result, []).catch(() => undefined);
    }
  }

  private wrapPromise(
    promise: PromiseLike<unknown>,
    method: string,
    registration?: Registration,
    deadlineBound = false,
  ): Promise<unknown> {
    const settlement = Promise.resolve(promise).then(
      async (value): Promise<SettledOperation> => {
        if (registration) await this.installRegistration(registration);
        if (this.revoked) await this.disposeLateResult(method, value);
        return { ok: true, value };
      },
      (error: unknown): SettledOperation => {
        const record = error && typeof error === 'object'
          ? error as { name?: unknown; message?: unknown }
          : {};
        const timeoutLike = record.name === 'TimeoutError'
          || (
            typeof record.message === 'string'
            && /Timeout \d+ms exceeded/i.test(record.message)
          );
        if (
          deadlineBound
          && timeoutLike
          && Date.now() >= this.deadlineAt - 100
        ) {
          return { ok: false, error: new RunCodeTimeoutError() };
        }
        return { ok: false, error };
      },
    );
    this.inFlight.add(settlement);
    void settlement.then(() => {
      this.inFlight.delete(settlement);
    });
    return settlement.then((outcome) => {
      if (!outcome.ok) throw outcome.error;
      if (this.revoked) throw new RunCodeRevokedError();
      return this.wrapValue(outcome.value);
    });
  }

  private invoke(
    target: object,
    property: PropertyKey,
    method: (...args: unknown[]) => unknown,
    input: unknown[],
  ): unknown {
    this.assertUsable();
    const methodName = String(property);
    if (!NON_DISPATCHING_METHODS.has(methodName)) this.dispatched = true;
    const prepared = this.prepareArguments(target, methodName, input);
    const bounded = this.bindPlaywrightTimeout(
      target,
      methodName,
      prepared.args,
    );
    let result: unknown;
    try {
      result = Reflect.apply(method, target, bounded.args);
    } catch (error) {
      throw normalizedError(error);
    }
    if (result && typeof (result as { then?: unknown }).then === 'function') {
      return this.wrapPromise(
        result as PromiseLike<unknown>,
        methodName,
        prepared.registration,
        bounded.deadlineBound,
      );
    }
    if (prepared.registration) {
      // EventEmitter registration methods are synchronous. The JavaScript
      // event loop cannot fire the deadline between Reflect.apply and here,
      // so this registration will be visible to the later revoke cleanup.
      void this.installRegistration(prepared.registration);
    }
    return this.wrapValue(result);
  }

  private wrapValue(value: unknown): unknown {
    if (Array.isArray(value)) {
      return value.map((item) => this.wrapValue(item));
    }
    if (!isProxyCandidate(value)) return value;
    return this.wrapObject(value, false);
  }

  private wrapObject(value: object, force: boolean): object {
    const existing = this.proxies.get(value);
    if (existing) return existing;
    if (!force && !isProxyCandidate(value)) return value;
    const proxy = new Proxy(value, {
      get: (target, property) => {
        this.assertUsable();
        const raw = Reflect.get(target, property, target);
        if (typeof raw !== 'function') return this.wrapValue(raw);
        let targetMethods = this.methods.get(target);
        if (!targetMethods) {
          targetMethods = new Map();
          this.methods.set(target, targetMethods);
        }
        const cached = targetMethods.get(property);
        if (cached) return cached;
        const wrapped = (...args: unknown[]): unknown => (
          this.invoke(target, property, raw, args)
        );
        targetMethods.set(property, wrapped);
        return wrapped;
      },
      set: (target, property, next) => {
        this.assertUsable();
        return Reflect.set(target, property, this.unwrap(next), target);
      },
      defineProperty: (target, property, descriptor) => {
        this.assertUsable();
        return Reflect.defineProperty(target, property, {
          ...descriptor,
          ...(
            Object.prototype.hasOwnProperty.call(descriptor, 'value')
              ? { value: this.unwrap(descriptor.value) }
              : {}
          ),
        });
      },
      deleteProperty: (target, property) => {
        this.assertUsable();
        return Reflect.deleteProperty(target, property);
      },
      getOwnPropertyDescriptor: (target, property) => {
        this.assertUsable();
        const descriptor = Reflect.getOwnPropertyDescriptor(target, property);
        if (!descriptor || descriptor.configurable === false) return descriptor;
        if (Object.prototype.hasOwnProperty.call(descriptor, 'value')) {
          return { ...descriptor, value: this.wrapValue(descriptor.value) };
        }
        return {
          ...descriptor,
          ...(descriptor.get
            ? {
                get: () => {
                  this.assertUsable();
                  return this.wrapValue(Reflect.apply(descriptor.get!, target, []));
                },
              }
            : {}),
          ...(descriptor.set
            ? {
                set: (next: unknown) => {
                  this.assertUsable();
                  Reflect.apply(descriptor.set!, target, [this.unwrap(next)]);
                },
              }
            : {}),
        };
      },
      getPrototypeOf: (target) => {
        this.assertUsable();
        return Reflect.getPrototypeOf(target);
      },
      has: (target, property) => {
        this.assertUsable();
        return Reflect.has(target, property);
      },
      ownKeys: (target) => {
        this.assertUsable();
        return Reflect.ownKeys(target);
      },
    });
    this.proxies.set(value, proxy);
    this.originals.set(proxy, value);
    return proxy;
  }

  async cleanupRegistrations(): Promise<void> {
    const cleanups: Promise<void>[] = [];
    for (const registration of [...this.registrations].reverse()) {
      if (!registration.installed || registration.removed) continue;
      registration.removed = true;
      cleanups.push(
        Promise.resolve(registration.cleanup()).catch(() => undefined),
      );
    }
    await Promise.all(cleanups);
  }

  async drainInFlight(timeoutMs: number): Promise<void> {
    if (!this.inFlight.size || timeoutMs <= 0) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        Promise.all([...this.inFlight]),
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, timeoutMs);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}

/**
 * Execute one async `(page) => ...` function and return its JSON text.
 *
 * `undefined`, functions and symbols stringify to `undefined`, matching
 * JavaScript's JSON.stringify contract; callers omit a text result then.
 */
export async function executeUnsafePlaywrightCode(
  page: Page,
  code: string,
  options: RunCodeOptions,
): Promise<string | undefined> {
  const remaining = Math.floor(options.deadlineAt - Date.now());
  if (remaining <= 0) throw new RunCodeTimeoutError();

  const end = new Deferred<string | undefined>();
  const unhandled = new Deferred<never>();
  let phase: 'running' | 'completed' | 'failed' | 'timedout' = 'running';
  const runContext: RunCodeRejectionContext = {
    onUnhandledRejection: (reason: unknown): void => {
      // Late failures from a successfully installed route/event callback are
      // consumed under their originating context. They never poison a newer,
      // unrelated run and cannot terminate Electron's main process.
      if (phase !== 'running') return;
      if (!unhandled.isDone()) unhandled.reject(reason);
      if (!end.isDone()) end.reject(reason);
    },
  };
  const facade = new RevocablePlaywrightFacade(runContext, options.deadlineAt);

  let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    deadlineTimer = setTimeout(() => reject(new RunCodeTimeoutError()), remaining);
  });

  return await rejectionContext.run(runContext, async () => {
    try {
      const context = createContext({
        page: facade.root(page),
        __end__: end,
      });
      // Concatenation here embeds source as JavaScript rather than as a quoted
      // template literal, so backticks, `${...}` and source comments retain
      // their original meaning without an escaping/rewrite layer.
      const invocation = new Script(
        '(async () => {\n'
        + '  try {\n'
        + `    const result = await (${code})(page);\n`
        + '    __end__.resolve(JSON.stringify(result));\n'
        + '  } catch (e) {\n'
        + '    __end__.reject(e);\n'
        + '  }\n'
        + '})()',
        {
          filename: options.filename || 'browser_run_code_unsafe.js',
        },
      );

      const execute = async (): Promise<string | undefined> => {
        // vm's timeout interrupts synchronous infinite loops before they can
        // monopolize Electron's main thread. The absolute Promise deadline below
        // covers asynchronous Playwright work after the first await.
        const synchronousBudget = Math.max(
          1,
          Math.floor(options.deadlineAt - Date.now()),
        );
        let iifePromise: Promise<void>;
        try {
          iifePromise = invocation.runInContext(context, {
            timeout: synchronousBudget,
          }) as Promise<void>;
        } catch (error) {
          if (
            error
            && typeof error === 'object'
            && (error as { code?: unknown }).code === 'ERR_SCRIPT_EXECUTION_TIMEOUT'
          ) {
            throw new RunCodeTimeoutError();
          }
          throw error;
        }
        // The IIFE normally catches user failures into `end`; retain this handler
        // for failures in the wrapper itself so no Promise can escape unobserved.
        const wrappedIife = Promise.resolve(iifePromise).catch((error: unknown) => {
          end.reject(error);
        });
        await Promise.race([wrappedIife, end.promise, unhandled.promise]);
        return await Promise.race([end.promise, unhandled.promise]);
      };

      const completed = options.withCompletion(execute);
      // Keep the rejection sink live through Playwright's post-action settle
      // window. A route/event callback can fail after the user function returns
      // but while requests caused by it are still being observed.
      const result = await Promise.race([completed, unhandled.promise, deadline]);
      phase = 'completed';
      return result;
    } catch (error) {
      const failure = normalizedError(error);
      facade.revoke();
      await facade.cleanupRegistrations();
      if (failure instanceof RunCodeTimeoutError) {
        phase = 'timedout';
        if (facade.hasDispatchedWork() && options.onTimeout) {
          await options.onTimeout().catch(() => undefined);
        }
        // Recovery navigation destroys renderer-side evaluate/action work.
        // Join the now-cancelled client promises before exposing the timeout.
        await facade.drainInFlight(options.onTimeout ? 2_000 : 50);
        throw failure;
      }
      phase = 'failed';
      throw failure;
    } finally {
      if (deadlineTimer) clearTimeout(deadlineTimer);
    }
  });
}
