import type { IncomingMessage } from 'http';
import { describe, expect, it, vi } from 'vitest';

vi.mock('electron', () => ({
  app: {
    getPath: () => '',
    on: vi.fn(),
  },
}));

import {
  validateDownloadResponse,
  type Inflight,
} from '../../src/main/update/download-controller';
import { MAX_UPDATE_PACKAGE_BYTES } from '../../src/main/update/update-file-security';

function response(
  statusCode: number,
  headers: Record<string, string | string[] | undefined>,
): IncomingMessage {
  return { statusCode, headers } as unknown as IncomingMessage;
}

function inflight(): Inflight {
  return { abortReason: null } as unknown as Inflight;
}

describe('update download response policy', () => {
  it('allows an unknown Content-Length and leaves the stream cap to the chunk loop', () => {
    expect(validateDownloadResponse(inflight(), response(200, {}), 0)).toEqual({
      append: false,
      totalBytes: 0,
    });
  });

  it('rejects an over-declared package length before writing anything', () => {
    const tooBig = String(MAX_UPDATE_PACKAGE_BYTES + 1);
    expect(() =>
      validateDownloadResponse(
        inflight(),
        response(200, { 'content-length': tooBig }),
        0,
      ),
    ).toThrow('安装包超过大小上限');
  });

  it('accepts the exact package cap', () => {
    const atCap = String(MAX_UPDATE_PACKAGE_BYTES);
    expect(
      validateDownloadResponse(
        inflight(),
        response(200, { 'content-length': atCap }),
        0,
      ),
    ).toEqual({ append: false, totalBytes: MAX_UPDATE_PACKAGE_BYTES });
  });

  it('rejects duplicate Content-Length headers', () => {
    expect(() =>
      validateDownloadResponse(
        inflight(),
        response(200, { 'content-length': ['1', '1'] }),
        0,
      ),
    ).toThrow('安装包 Content-Length 重复');
  });

  it('rejects malformed or negative Content-Length values', () => {
    for (const raw of ['-1', '1.5', '0x10', ' 1']) {
      expect(() =>
        validateDownloadResponse(
          inflight(),
          response(200, { 'content-length': raw }),
          0,
        ),
      ).toThrow();
    }
  });

  it('rejects any non-identity content encoding', () => {
    expect(() =>
      validateDownloadResponse(
        inflight(),
        response(200, { 'content-encoding': 'gzip' }),
        0,
      ),
    ).toThrow('安装包响应不得使用内容编码');
  });

  it('rejects non-200/206 status codes', () => {
    expect(() =>
      validateDownloadResponse(inflight(), response(404, {}), 0),
    ).toThrow('下载失败（HTTP 404）');
  });

  it('rejects a resumed response whose Content-Range does not match the local fragment', () => {
    expect(() =>
      validateDownloadResponse(
        inflight(),
        response(206, {
          'content-length': '3',
          'content-range': 'bytes 10-12/14',
        }),
        10,
      ),
    ).toThrow('断点响应范围与本地片段不匹配');
  });

  it('rejects a resumed response that declares more than the package cap', () => {
    expect(() =>
      validateDownloadResponse(
        inflight(),
        response(206, {
          'content-length': '1',
          'content-range': `bytes ${MAX_UPDATE_PACKAGE_BYTES}-${MAX_UPDATE_PACKAGE_BYTES}/${
            MAX_UPDATE_PACKAGE_BYTES + 1
          }`,
        }),
        MAX_UPDATE_PACKAGE_BYTES,
      ),
    ).toThrow('断点响应范围与本地片段不匹配');
  });
});
