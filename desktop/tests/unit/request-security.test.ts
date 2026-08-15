import { describe, expect, it } from 'vitest';

import {
  isSecureRemoteUrl,
  parseBody,
  sanitizeRequestHeaders,
} from '../../src/main/request';

describe('request response error boundary', () => {
  it('does not return malformed upstream body or parser details', () => {
    const result = parseBody(
      502,
      '{"token":"should-not-cross","path":"C:\\\\private\\\\secret"',
    );

    expect(result).toEqual({
      success: false,
      statusCode: 502,
      message: '响应格式无效',
    });
    expect(result).not.toHaveProperty('raw');
    expect(JSON.stringify(result)).not.toContain('should-not-cross');
    expect(JSON.stringify(result)).not.toContain('private');
  });

  it('does not expose HTML upstream bodies', () => {
    const result = parseBody(500, '<html>credential=secret</html>');

    expect(result).toEqual({
      success: false,
      statusCode: 500,
      message: '请求失败：返回 HTML 页面',
      isHTML: true,
    });
    expect(JSON.stringify(result)).not.toContain('credential');
  });

  it('keeps valid JSON data unchanged', () => {
    expect(parseBody(200, '{"ok":true}')).toEqual({
      success: true,
      statusCode: 200,
      data: { ok: true },
    });
  });

  it('rejects https URLs that carry userinfo credentials', () => {
    expect(isSecureRemoteUrl('https://user:password@api.example.test/v1')).toBe(false);
    expect(isSecureRemoteUrl('https://api.example.test/v1')).toBe(true);
    expect(isSecureRemoteUrl('http://api.example.test/v1')).toBe(false);
  });

  it('strips renderer authentication and hop-by-hop headers', () => {
    const sanitized = sanitizeRequestHeaders(
      {
        Authorization: 'Bearer renderer-forged',
        'Proxy-Authorization': 'Basic forged',
        Connection: 'keep-alive',
        'Transfer-Encoding': 'chunked',
        Host: 'forged.example.test',
        'Content-Length': '999999',
        'X-Trace': 'kept',
      },
      'Bearer host-trusted',
    );

    expect(sanitized['Authorization']).toBe('Bearer host-trusted');
    expect(sanitized).not.toHaveProperty('Proxy-Authorization');
    expect(sanitized).not.toHaveProperty('Connection');
    expect(sanitized).not.toHaveProperty('Transfer-Encoding');
    expect(sanitized).not.toHaveProperty('Host');
    expect(sanitized).not.toHaveProperty('Content-Length');
    expect(sanitized['X-Trace']).toBe('kept');
  });

  it('drops renderer Authorization when no host token exists', () => {
    const sanitized = sanitizeRequestHeaders(
      { Authorization: 'Bearer renderer-forged' },
      undefined,
    );
    expect(sanitized).not.toHaveProperty('Authorization');
  });

  it('rejects CRLF header injection', () => {
    expect(() => sanitizeRequestHeaders({ 'X-Trace': 'a\r\nSet-Cookie: evil' }))
      .toThrow('非法字符');
  });
});
