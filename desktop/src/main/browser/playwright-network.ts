/**
 * Playwright-compatible network inspection, implemented only with public APIs.
 *
 * The MCP backend keeps request numbers tied to the unfiltered request list:
 * filtering a list must never renumber it, because `browser_network_request`
 * resolves the printed number against the same page request list.
 */

import type { Page, Request } from './playwright-compat';

/**
 * 请求头里承载会话身份的字段，一律只报存在与长度，不报值。
 *
 * 这些值不是"页面内容"，而是**当前登录用户的身份本身**。一旦进入模型上下文，
 * 一次 navigate 就能把内网 session 带到任意外部地址；而读网络是个纯观测动作，
 * 拿到 Cookie 原文对任何合法用途都没有帮助。
 *
 * 判定按**小写全名精确匹配**，不做前缀/包含匹配：`x-request-cookie-policy`
 * 这类无害头不该被抹，而真正危险的头名字是固定的、可枚举的。
 */
const CREDENTIAL_HEADER_NAMES: ReadonlySet<string> = new Set([
  'authorization',
  'proxy-authorization',
  'cookie',
  'set-cookie',
  'cookie2',
  'set-cookie2',
  'www-authenticate',
  'proxy-authenticate',
  'x-api-key',
  'x-auth-token',
  'x-amz-security-token',
  'x-csrf-token',
  'x-xsrf-token',
]);

/**
 * 把一个请求头渲染成一行，凭据类只留形状。
 *
 * 保留长度是刻意的：调试"为什么这个请求 401"时，"Authorization 存在且长 862 字节"
 * 与"没有 Authorization"是两个完全不同的结论，而这个区别不泄漏任何身份。
 */
function renderHeaderLine(name: string, value: string): string {
  if (!CREDENTIAL_HEADER_NAMES.has(name.trim().toLowerCase())) {
    return `${name}: ${value}`;
  }
  return `${name}: <redacted ${value.length} bytes>`;
}

export const NETWORK_REQUEST_PARTS = [
  'request-headers',
  'request-body',
  'response-headers',
  'response-body',
] as const;

export type NetworkRequestPart = typeof NETWORK_REQUEST_PARTS[number];

export interface NetworkRequestsOptions {
  static: boolean;
  filter?: string;
}

export interface NetworkTextPayload {
  format: 'text';
  title: 'Network' | 'Request' | 'Request headers' | 'Request body'
    | 'Response headers' | 'Response body';
  text: string;
  extension: 'log' | 'txt';
}

export interface NetworkBinaryPayload {
  format: 'binary';
  title: 'Response body';
  base64: string;
  mimeType: string;
  extension: string;
}

export interface NetworkEmptyPayload {
  format: 'empty';
  title: 'Request body' | 'Response headers' | 'Response body';
}

export type NetworkPayload =
  | NetworkTextPayload
  | NetworkBinaryPayload
  | NetworkEmptyPayload;

export class NetworkRequestNotFoundError extends Error {
  readonly index: number;

  constructor(index: number) {
    super(
      `Request #${index} not found. `
      + 'Use browser_network_requests to see available indexes.',
    );
    this.name = 'NetworkRequestNotFoundError';
    this.index = index;
  }
}

interface PageRequestLedger {
  requests: Request[];
  seen: Set<Request>;
  initialized: boolean;
  initializing: Promise<void> | null;
  pending: Array<{ request: Request; reset: boolean }>;
}

const pageRequestLedgers = new WeakMap<object, PageRequestLedger>();

function isMainFrameNavigation(page: Page, request: Request): boolean {
  if (!request.isNavigationRequest()) return false;
  try {
    // One browser navigation may contain an arbitrary redirect chain. Only
    // the first request starts a new "since navigation" ledger; resetting on
    // every redirected request would silently discard the redirect history
    // and renumber the final request back to #1.
    return request.frame() === page.mainFrame() && request.redirectedFrom() === null;
  } catch {
    return false;
  }
}

function resetLedger(ledger: PageRequestLedger): void {
  ledger.requests.length = 0;
  ledger.seen.clear();
}

function appendRequest(ledger: PageRequestLedger, request: Request): void {
  if (ledger.seen.has(request)) return;
  ledger.seen.add(request);
  ledger.requests.push(request);
}

function ledgerForPage(page: Page): PageRequestLedger {
  const current = pageRequestLedgers.get(page);
  if (current) return current;
  const ledger: PageRequestLedger = {
    requests: [],
    seen: new Set(),
    initialized: false,
    initializing: null,
    pending: [],
  };
  pageRequestLedgers.set(page, ledger);

  // Page.request objects received through the public event API are retained by
  // Playwright instead of being collected out of page.requests()' rolling
  // window. This gives numbers printed by one list call stable meaning for a
  // later detail call, without CDP/private network plumbing.
  page.on('request', (request) => {
    const reset = isMainFrameNavigation(page, request);
    if (!ledger.initialized) {
      ledger.pending.push({ request, reset });
      return;
    }
    if (reset) resetLedger(ledger);
    appendRequest(ledger, request);
  });
  return ledger;
}

async function initializeLedger(page: Page, ledger: PageRequestLedger): Promise<void> {
  if (ledger.initialized) return;
  ledger.initializing ??= (async () => {
    const snapshot = await page.requests();
    // If a main-frame request arrived while page.requests() was in flight, its
    // event is the exact current-document boundary. Do not reintroduce older
    // requests from the rolling snapshot ahead of it.
    let lastReset = -1;
    for (let index = ledger.pending.length - 1; index >= 0; index -= 1) {
      if (!ledger.pending[index].reset) continue;
      lastReset = index;
      break;
    }
    if (lastReset < 0) {
      for (const request of snapshot) appendRequest(ledger, request);
    }
    for (const event of ledger.pending.slice(Math.max(0, lastReset))) {
      if (event.reset) resetLedger(ledger);
      appendRequest(ledger, event.request);
    }
    ledger.pending.length = 0;
    ledger.initialized = true;
  })().finally(() => {
    ledger.initializing = null;
  });
  await ledger.initializing;
}

async function requestsFromPage(page: Page): Promise<Request[]> {
  const ledger = ledgerForPage(page);
  await initializeLedger(page, ledger);
  return [...ledger.requests];
}

/**
 * Start a new stable request index before an explicit tool/address-bar
 * navigation. The already-installed public request listener captures the
 * document request and every resource that follows it.
 */
export function resetNetworkRequests(page: Page): void {
  const ledger = ledgerForPage(page);
  resetLedger(ledger);
  ledger.pending.length = 0;
  ledger.initialized = true;
}

function isSuccessfulResponse(request: Request): boolean {
  if (request.failure()) return false;
  const response = request.existingResponse();
  return Boolean(response && response.status() < 400);
}

function isFetch(request: Request): boolean {
  return ['fetch', 'xhr'].includes(request.resourceType());
}

function renderRequestLine(request: Request): string {
  const response = request.existingResponse();
  let line = `[${request.method().toUpperCase()}] ${request.url()}`;
  if (response) {
    line += ` => [${response.status()}] ${response.statusText()}`;
  } else if (request.failure()) {
    line += ` => [FAILED] ${request.failure()?.errorText ?? 'Unknown error'}`;
  }
  return line;
}

export async function listNetworkRequests(
  page: Page,
  options: NetworkRequestsOptions,
): Promise<NetworkTextPayload> {
  const allRequests = await requestsFromPage(page);
  let filter: RegExp | undefined;
  if (options.filter) {
    try {
      filter = new RegExp(options.filter);
    } catch {
      throw new Error('Invalid regular expression');
    }
  }

  const lines: string[] = [];
  let hiddenStaticCount = 0;
  for (let index = 0; index < allRequests.length; index += 1) {
    const request = allRequests[index];
    if (!options.static && !isFetch(request) && isSuccessfulResponse(request)) {
      hiddenStaticCount += 1;
      continue;
    }
    if (filter) {
      filter.lastIndex = 0;
      if (!filter.test(request.url())) continue;
    }
    lines.push(`${index + 1}. ${renderRequestLine(request)}`);
  }
  if (hiddenStaticCount > 0) {
    lines.push(
      `\nNote: ${hiddenStaticCount} static request`
      + `${hiddenStaticCount === 1 ? '' : 's'} not shown, run with "static" option `
      + `to see ${hiddenStaticCount === 1 ? 'it' : 'them'}.`,
    );
  }
  return {
    format: 'text',
    title: 'Network',
    text: lines.join('\n'),
    extension: 'log',
  };
}

function canHaveResponseBody(
  response: ReturnType<Request['existingResponse']>,
): boolean {
  if (!response) return false;
  const status = response.status();
  // RFC 7230 statuses that cannot carry a response body.
  return status !== 204 && status !== 304 && !(status >= 100 && status < 200);
}

function appendHeaderSection(
  lines: string[],
  title: string,
  headers: Record<string, string>,
): void {
  const entries = Object.entries(headers);
  if (!entries.length) return;
  lines.push('');
  lines.push(`  ${title}`);
  for (const [name, value] of entries) {
    lines.push(`    ${renderHeaderLine(name, value)}`);
  }
}

function computeDurationMs(request: Request): number | undefined {
  const timing = request.timing();
  if (!timing || timing.responseEnd < 0) return undefined;
  return Math.round(timing.responseEnd);
}

function partHint(part: 'request-body' | 'response-body'): string {
  const subject = part === 'request-body' ? 'request body' : 'response body';
  return `Call browser_network_request with part="${part}" to read the ${subject}.`;
}

function renderRequestDetails(index: number, request: Request): string {
  const response = request.existingResponse();
  const responseHeaders = response?.headers();
  const lines: string[] = [
    `#${index} [${request.method().toUpperCase()}] ${request.url()}`,
    '',
    '  General',
  ];
  if (response) {
    lines.push(`    status:    [${response.status()}] ${response.statusText()}`);
  } else if (request.failure()) {
    lines.push(
      `    status:    [FAILED] ${request.failure()?.errorText ?? 'Unknown error'}`,
    );
  }
  const duration = computeDurationMs(request);
  if (duration !== undefined) lines.push(`    duration:  ${duration}ms`);
  lines.push(`    type:      ${request.resourceType()}`);
  const contentType = responseHeaders?.['content-type'];
  if (contentType) lines.push(`    mimeType:  ${contentType.split(';')[0].trim()}`);

  appendHeaderSection(lines, 'Request headers', request.headers());
  if (responseHeaders) appendHeaderSection(lines, 'Response headers', responseHeaders);

  const hints: string[] = [];
  if (request.postData()) hints.push(partHint('request-body'));
  if (canHaveResponseBody(response)) hints.push(partHint('response-body'));
  if (hints.length) lines.push('', ...hints);
  return lines.join('\n');
}

function renderHeaders(headers: Record<string, string>): string {
  return Object.entries(headers)
    .map(([name, value]) => renderHeaderLine(name, value))
    .join('\n');
}

function isTextualMimeType(mimeType: string): boolean {
  // Keep byte-for-byte behavior with the pinned Playwright isomorphic/mimeType.ts classifier.
  return /^(text\/.*?|application\/(json|(x-)?javascript|xml.*?|ecmascript|graphql|x-www-form-urlencoded)|image\/svg(\+xml)?|application\/.*?(\+json|\+xml))(;\s*charset=.*)?$/.test(
    mimeType,
  );
}

function extensionForMimeType(contentType: string | undefined): string {
  const subtype = (contentType ?? '').split(';')[0].split('/')[1]?.trim().toLowerCase() ?? '';
  if (!subtype) return 'bin';
  const tail = subtype.includes('+') ? subtype.split('+').at(-1) ?? '' : subtype;
  if (tail === 'plain') return 'txt';
  if (tail === 'javascript' || tail === 'ecmascript') return 'js';
  if (tail === 'jpeg') return 'jpg';
  return tail.replace(/[^a-z0-9]/g, '') || 'bin';
}

async function renderRequestPart(
  request: Request,
  part: NetworkRequestPart,
): Promise<NetworkPayload> {
  if (part === 'request-headers') {
    return {
      format: 'text',
      title: 'Request headers',
      text: renderHeaders(request.headers()),
      extension: 'txt',
    };
  }
  if (part === 'request-body') {
    const data = request.postData();
    if (data === null) return { format: 'empty', title: 'Request body' };
    return {
      format: 'text',
      title: 'Request body',
      text: data,
      extension: 'txt',
    };
  }

  const response = request.existingResponse();
  if (!response) {
    return {
      format: 'empty',
      title: part === 'response-headers' ? 'Response headers' : 'Response body',
    };
  }
  if (part === 'response-headers') {
    return {
      format: 'text',
      title: 'Response headers',
      text: renderHeaders(response.headers()),
      extension: 'txt',
    };
  }

  const contentType = response.headers()['content-type'] ?? '';
  if (isTextualMimeType(contentType)) {
    try {
      return {
        format: 'text',
        title: 'Response body',
        text: await response.text(),
        extension: 'txt',
      };
    } catch {
      return { format: 'empty', title: 'Response body' };
    }
  }
  if (!canHaveResponseBody(response)) {
    return { format: 'empty', title: 'Response body' };
  }
  try {
    const body = await response.body();
    if (!body.length) return { format: 'empty', title: 'Response body' };
    return {
      format: 'binary',
      title: 'Response body',
      base64: body.toString('base64'),
      mimeType: contentType.split(';')[0].trim(),
      extension: extensionForMimeType(contentType),
    };
  } catch {
    return { format: 'empty', title: 'Response body' };
  }
}

export async function networkRequest(
  page: Page,
  index: number,
  part?: NetworkRequestPart,
): Promise<NetworkPayload> {
  const allRequests = await requestsFromPage(page);
  const request = allRequests[index - 1];
  if (!request) throw new NetworkRequestNotFoundError(index);
  if (part) return await renderRequestPart(request, part);
  return {
    format: 'text',
    title: 'Request',
    text: renderRequestDetails(index, request),
    extension: 'log',
  };
}
