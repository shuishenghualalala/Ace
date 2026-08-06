import { EventEmitter } from 'node:events';

import { describe, expect, it, vi } from 'vitest';

import {
  NetworkRequestNotFoundError,
  listNetworkRequests,
  networkRequest,
  resetNetworkRequests,
} from '../../src/main/browser/playwright-network';

import type { Page, Request } from '../../src/main/browser/playwright-compat';

interface FakeResponseOptions {
  status?: number;
  statusText?: string;
  headers?: Record<string, string>;
  text?: string;
  body?: Buffer;
  textError?: Error;
  bodyError?: Error;
}

function fakeResponse(options: FakeResponseOptions = {}) {
  return {
    status: vi.fn(() => options.status ?? 200),
    statusText: vi.fn(() => options.statusText ?? 'OK'),
    headers: vi.fn(() => options.headers ?? {}),
    text: vi.fn(async () => {
      if (options.textError) throw options.textError;
      return options.text ?? '';
    }),
    body: vi.fn(async () => {
      if (options.bodyError) throw options.bodyError;
      return options.body ?? Buffer.alloc(0);
    }),
  };
}

function fakeRequest(options: {
  method?: string;
  url: string;
  resourceType?: string;
  headers?: Record<string, string>;
  postData?: string | null;
  response?: ReturnType<typeof fakeResponse> | null;
  failure?: { errorText: string } | null;
  duration?: number;
  navigation?: boolean;
  frame?: object;
  redirectedFrom?: Request | null;
}): Request {
  return {
    method: vi.fn(() => options.method ?? 'GET'),
    url: vi.fn(() => options.url),
    resourceType: vi.fn(() => options.resourceType ?? 'document'),
    headers: vi.fn(() => options.headers ?? {}),
    postData: vi.fn(() => options.postData ?? null),
    existingResponse: vi.fn(() => options.response ?? null),
    failure: vi.fn(() => options.failure ?? null),
    isNavigationRequest: vi.fn(() => options.navigation ?? false),
    frame: vi.fn(() => {
      if (options.frame) return options.frame;
      throw new Error('frame unavailable in request-only fixture');
    }),
    redirectedFrom: vi.fn(() => options.redirectedFrom ?? null),
    timing: vi.fn(() => ({
      startTime: 0,
      domainLookupStart: -1,
      domainLookupEnd: -1,
      connectStart: -1,
      secureConnectionStart: -1,
      connectEnd: -1,
      requestStart: 0,
      responseStart: options.duration ?? -1,
      responseEnd: options.duration ?? -1,
    })),
  } as unknown as Request;
}

function fakePage(requests: Request[]): Page {
  const events = new EventEmitter();
  const mainFrame = {};
  return {
    requests: vi.fn(async () => requests),
    mainFrame: vi.fn(() => mainFrame),
    on: events.on.bind(events),
    off: events.off.bind(events),
    emit: events.emit.bind(events),
  } as unknown as Page;
}

describe('Playwright-compatible network tools', () => {
  it('keeps original 1-based indexes, hides successful static traffic, and lists a failure once', async () => {
    const document = fakeRequest({
      url: 'https://example.test/',
      response: fakeResponse(),
    });
    const fetchRequest = fakeRequest({
      url: 'https://example.test/api/user',
      resourceType: 'fetch',
      response: fakeResponse({ status: 200, statusText: 'OK' }),
    });
    const failed = fakeRequest({
      url: 'https://example.test/logo.png',
      resourceType: 'image',
      failure: { errorText: 'net::ERR_FAILED' },
    });
    const serverError = fakeRequest({
      url: 'https://example.test/app.js',
      resourceType: 'script',
      response: fakeResponse({
        status: 500,
        statusText: 'Internal Server Error',
      }),
    });

    const payload = await listNetworkRequests(
      fakePage([document, fetchRequest, failed, failed, serverError]),
      { static: false },
    );

    expect(payload.text).toBe([
      '2. [GET] https://example.test/api/user => [200] OK',
      '3. [GET] https://example.test/logo.png => [FAILED] net::ERR_FAILED',
      '4. [GET] https://example.test/app.js => [500] Internal Server Error',
      '',
      'Note: 1 static request not shown, run with "static" option to see it.',
    ].join('\n'));
  });

  it('applies a URL regexp without renumbering matching requests', async () => {
    const requests = [
      fakeRequest({
        url: 'https://example.test/static.css',
        resourceType: 'stylesheet',
        response: fakeResponse(),
      }),
      fakeRequest({
        url: 'https://example.test/api/first',
        resourceType: 'xhr',
        response: fakeResponse(),
      }),
      fakeRequest({
        url: 'https://example.test/api/second',
        resourceType: 'fetch',
        response: fakeResponse({ status: 404, statusText: 'Not Found' }),
      }),
    ];

    const payload = await listNetworkRequests(fakePage(requests), {
      static: false,
      filter: '/api/',
    });

    expect(payload.text).toContain('2. [GET] https://example.test/api/first => [200] OK');
    expect(payload.text).toContain(
      '3. [GET] https://example.test/api/second => [404] Not Found',
    );
    expect(payload.text).not.toContain('1. [GET]');
  });

  it('rejects an invalid JavaScript regular expression explicitly', async () => {
    await expect(
      listNetworkRequests(fakePage([]), { static: false, filter: '[' }),
    ).rejects.toThrow('Invalid regular expression');
  });

  it('renders full request metadata and body hints', async () => {
    const request = fakeRequest({
      method: 'post',
      url: 'https://example.test/api/save',
      resourceType: 'xhr',
      headers: {
        accept: 'application/json',
        'x-order': 'second',
      },
      postData: '{"name":"Crew"}',
      duration: 12.6,
      response: fakeResponse({
        status: 201,
        statusText: 'Created',
        headers: {
          'content-type': 'application/problem+json; charset=utf-8',
          'x-response': 'yes',
        },
      }),
    });

    const payload = await networkRequest(fakePage([request]), 1);

    expect(payload).toEqual({
      format: 'text',
      title: 'Request',
      extension: 'log',
      text: [
        '#1 [POST] https://example.test/api/save',
        '',
        '  General',
        '    status:    [201] Created',
        '    duration:  13ms',
        '    type:      xhr',
        '    mimeType:  application/problem+json',
        '',
        '  Request headers',
        '    accept: application/json',
        '    x-order: second',
        '',
        '  Response headers',
        '    content-type: application/problem+json; charset=utf-8',
        '    x-response: yes',
        '',
        'Call browser_network_request with part="request-body" to read the request body.',
        'Call browser_network_request with part="response-body" to read the response body.',
      ].join('\n'),
    });
  });

  it('returns headers and request/text response bodies without changing characters', async () => {
    const exactRequestBody = '\0<xml>&\n雪🙂\r\n';
    const exactResponseBody = '<script>x & y</script>\n尾部\0';
    const request = fakeRequest({
      method: 'POST',
      url: 'https://example.test/api/raw',
      resourceType: 'fetch',
      headers: { 'x-one': '1', 'x-two': '二' },
      postData: exactRequestBody,
      response: fakeResponse({
        headers: { 'content-type': 'text/plain; charset=utf-8' },
        text: exactResponseBody,
      }),
    });
    const page = fakePage([request]);

    await expect(networkRequest(page, 1, 'request-headers')).resolves.toMatchObject({
      format: 'text',
      text: 'x-one: 1\nx-two: 二',
    });
    await expect(networkRequest(page, 1, 'request-body')).resolves.toMatchObject({
      format: 'text',
      text: exactRequestBody,
    });
    await expect(networkRequest(page, 1, 'response-body')).resolves.toMatchObject({
      format: 'text',
      text: exactResponseBody,
    });
  });

  it('凭据类请求头只报形状，不报值', async () => {
    // Cookie / Authorization 不是"页面内容"，是**当前登录用户的身份本身**。
    // 一旦进模型上下文，一次 navigate 就能把内网 session 带到任意外部地址，
    // 而读网络这个动作拿到 Cookie 原文对任何合法用途都没有帮助。
    const request = fakeRequest({
      method: 'POST',
      url: 'https://intranet.test/api/ticket',
      resourceType: 'fetch',
      headers: {
        'Cookie': 'JSESSIONID=abcdef0123456789; role=admin',
        'Authorization': 'Bearer eyJhbGciOi.payload.sig',
        'X-Api-Key': 'k-live-001',
        // 名字里带 cookie 但不是凭据的头不能被误伤——判定是全名精确匹配
        'x-request-cookie-policy': 'strict',
        'content-type': 'application/json',
      },
      response: fakeResponse({
        headers: { 'set-cookie': 'JSESSIONID=rotated; HttpOnly' },
        text: '{}',
      }),
    });
    const page = fakePage([request]);

    const headers = await networkRequest(page, 1, 'request-headers');
    const text = (headers as { text: string }).text;
    expect(text).not.toContain('abcdef0123456789');
    expect(text).not.toContain('eyJhbGciOi');
    expect(text).not.toContain('k-live-001');
    // 保留长度：调试 401 时「有 Authorization 且长 N 字节」与「没有」是两个结论
    expect(text).toContain('Authorization: <redacted 29 bytes>');
    expect(text).toContain('Cookie: <redacted 39 bytes>');
    expect(text).toContain('X-Api-Key: <redacted 10 bytes>');
    // 非凭据头原样保留
    expect(text).toContain('x-request-cookie-policy: strict');
    expect(text).toContain('content-type: application/json');

    // 响应头里的 set-cookie 同样要抹，而且列表详情那条渲染路径也要覆盖
    const responseHeaders = await networkRequest(page, 1, 'response-headers');
    expect((responseHeaders as { text: string }).text).not.toContain('rotated');

    const detail = await listNetworkRequests(page, { static: false });
    expect(detail.text).not.toContain('abcdef0123456789');
  });

  it('returns binary response bytes as reversible base64 with the pinned MIME extension', async () => {
    const bytes = Buffer.from([0, 255, 1, 2, 128, 13, 10]);
    const request = fakeRequest({
      url: 'https://example.test/picture',
      response: fakeResponse({
        headers: { 'content-type': 'image/jpeg; charset=binary' },
        body: bytes,
      }),
    });

    const payload = await networkRequest(
      fakePage([request]),
      1,
      'response-body',
    );

    expect(payload).toEqual({
      format: 'binary',
      title: 'Response body',
      base64: bytes.toString('base64'),
      mimeType: 'image/jpeg',
      extension: 'jpg',
    });
    if (payload.format !== 'binary') throw new Error('expected binary payload');
    expect(Buffer.from(payload.base64, 'base64')).toEqual(bytes);
  });

  it('returns an empty part when no body exists or Playwright cannot read it', async () => {
    const noBody = fakeRequest({
      url: 'https://example.test/no-content',
      response: fakeResponse({
        status: 204,
        statusText: 'No Content',
        headers: { 'content-type': 'application/octet-stream' },
      }),
    });
    const unreadableText = fakeRequest({
      url: 'https://example.test/text',
      response: fakeResponse({
        headers: { 'content-type': 'text/plain' },
        textError: new Error('body unavailable'),
      }),
    });

    await expect(
      networkRequest(fakePage([noBody]), 1, 'response-body'),
    ).resolves.toEqual({ format: 'empty', title: 'Response body' });
    await expect(
      networkRequest(fakePage([unreadableText]), 1, 'response-body'),
    ).resolves.toEqual({ format: 'empty', title: 'Response body' });
  });

  it('reports an out-of-range index with the MCP recovery instruction', async () => {
    await expect(networkRequest(fakePage([]), 9)).rejects.toEqual(
      new NetworkRequestNotFoundError(9),
    );
  });

  it('keeps printed indexes stable when new requests arrive before detail lookup', async () => {
    const first = fakeRequest({
      url: 'https://example.test/api/first',
      resourceType: 'fetch',
      response: fakeResponse(),
    });
    const second = fakeRequest({
      url: 'https://example.test/api/second',
      resourceType: 'fetch',
      response: fakeResponse(),
    });
    const page = fakePage([first]);
    await listNetworkRequests(page, { static: false });

    (page as unknown as EventEmitter).emit('request', second);

    const firstDetail = await networkRequest(page, 1);
    const secondDetail = await networkRequest(page, 2);
    expect(firstDetail).toMatchObject({
      format: 'text',
      text: expect.stringContaining('https://example.test/api/first'),
    });
    expect(secondDetail).toMatchObject({
      format: 'text',
      text: expect.stringContaining('https://example.test/api/second'),
    });
    expect(page.requests).toHaveBeenCalledOnce();
  });

  it('resets the per-Page index before an explicit navigation', async () => {
    const oldRequest = fakeRequest({
      url: 'https://example.test/old',
      resourceType: 'fetch',
      response: fakeResponse(),
    });
    const page = fakePage([oldRequest]);
    await listNetworkRequests(page, { static: false });

    resetNetworkRequests(page);
    const document = fakeRequest({
      url: 'https://next.test/',
      response: fakeResponse(),
    });
    Object.assign(document as unknown as Record<string, unknown>, {
      isNavigationRequest: vi.fn(() => true),
      frame: vi.fn(() => ({ id: 'main' })),
    });
    (page as unknown as EventEmitter).emit('request', document);

    const payload = await listNetworkRequests(page, { static: true });
    expect(payload.text).toBe('1. [GET] https://next.test/ => [200] OK');
  });

  it('starts a new ledger once and preserves every request in a redirect chain', async () => {
    const page = fakePage([]);
    const mainFrame = page.mainFrame();
    resetNetworkRequests(page);
    const first = fakeRequest({
      url: 'https://example.test/start',
      response: fakeResponse({ status: 302, statusText: 'Found' }),
      navigation: true,
      frame: mainFrame,
    });
    const second = fakeRequest({
      url: 'https://example.test/middle',
      response: fakeResponse({ status: 307, statusText: 'Temporary Redirect' }),
      navigation: true,
      frame: mainFrame,
      redirectedFrom: first,
    });
    const final = fakeRequest({
      url: 'https://example.test/final',
      response: fakeResponse(),
      navigation: true,
      frame: mainFrame,
      redirectedFrom: second,
    });

    (page as unknown as EventEmitter).emit('request', first);
    (page as unknown as EventEmitter).emit('request', second);
    (page as unknown as EventEmitter).emit('request', final);

    const payload = await listNetworkRequests(page, { static: true });
    expect(payload.text).toBe([
      '1. [GET] https://example.test/start => [302] Found',
      '2. [GET] https://example.test/middle => [307] Temporary Redirect',
      '3. [GET] https://example.test/final => [200] OK',
    ].join('\n'));
  });
});
