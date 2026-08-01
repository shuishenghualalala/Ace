/**
 * IPC schema 校验单测。
 *
 * 覆盖：
 * - GatewayFetchArgs：合法 hostname + path 通过；非白名单 hostname 拒绝；非白名单 path 拒绝
 * - GatewayUploadArgs：仅放行 /api/wiki/upload 精确 path；files 绝对路径、数量上限
 * - ShellOpenExternalArgs：http/https 通过；file/javascript: 拒绝
 * - FeedbackSubmitArgs：title/description 长度上限；images 数组上限；dataUrl 协议校验
 * - DialogSelectFileArgs：filters 数组；maxBytes 上限
 */

import { describe, it, expect } from 'vitest';
import {
  GatewayFetchArgs,
  GatewayUploadArgs,
  ShellOpenExternalArgs,
  ShellOpenPathArgs,
  ShellOpenPathWithArgs,
  FeedbackSubmitArgs,
  FeedbackListArgs,
  DialogSelectFileArgs,
  UpdateStartDownloadArgs,
} from '../../src/shared/ipc-schemas';
import { MAX_DIALOG_FILE_BYTES } from '../../src/shared/constants';

describe('GatewayFetchArgs', () => {
  it('accepts a valid localhost url with /api/ path', () => {
    const r = GatewayFetchArgs.parse({ url: 'http://127.0.0.1:8000/api/cron/jobs' });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.value.url).toBe('http://127.0.0.1:8000/api/cron/jobs');
  });

  it('accepts a localhost hostname', () => {
    const r = GatewayFetchArgs.parse({ url: 'http://localhost:8000/api/sessions' });
    expect(r.ok).toBe(true);
  });

  it('rejects non-local hostname (SSRF)', () => {
    const r = GatewayFetchArgs.parse({ url: 'http://example.com/api/foo' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('hostname');
  });

  it('rejects path that does not start with /api/', () => {
    const r = GatewayFetchArgs.parse({ url: 'http://127.0.0.1:8000/etc/passwd' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('pathname');
  });

  it('rejects non-object input', () => {
    expect(GatewayFetchArgs.parse('string').ok).toBe(false);
    expect(GatewayFetchArgs.parse(null).ok).toBe(false);
  });

  it('accepts optional init.headers / method / body', () => {
    const r = GatewayFetchArgs.parse({
      url: 'http://127.0.0.1:8000/api/sessions',
      init: { method: 'POST', headers: { 'x-test': '1' }, body: '{"k":"v"}' },
    });
    expect(r.ok).toBe(true);
    if (r.ok && r.value.init) {
      expect(r.value.init.method).toBe('POST');
      expect(r.value.init.headers?.['x-test']).toBe('1');
      expect(r.value.init.body).toBe('{"k":"v"}');
    }
  });

  it('rejects init.headers with non-string values', () => {
    const r = GatewayFetchArgs.parse({
      url: 'http://127.0.0.1:8000/api/sessions',
      init: { headers: { bad: 123 as unknown as string } },
    });
    expect(r.ok).toBe(false);
  });
});

describe('ShellOpenExternalArgs', () => {
  it('accepts https url', () => {
    expect(ShellOpenExternalArgs.parse({ url: 'https://example.com' }).ok).toBe(true);
  });

  it('accepts http url', () => {
    expect(ShellOpenExternalArgs.parse({ url: 'http://example.com' }).ok).toBe(true);
  });

  it('rejects file:// protocol', () => {
    const r = ShellOpenExternalArgs.parse({ url: 'file:///etc/passwd' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('protocol');
  });

  it('rejects javascript: protocol', () => {
    expect(ShellOpenExternalArgs.parse({ url: 'javascript:alert(1)' }).ok).toBe(false);
  });

  it('rejects empty string url', () => {
    expect(ShellOpenExternalArgs.parse({ url: '' }).ok).toBe(false);
  });
});

describe('ShellOpenPathArgs', () => {
  // 注意：本 schema 仅做结构校验（非空字符串 path）；根目录白名单在主进程 handler 注入，
  // 不在纯 schema 层测试。这里只覆盖入参形态错位。
  it('accepts a non-empty path object', () => {
    const r = ShellOpenPathArgs.parse({ path: '/home/user/cache/foo.log' });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.value.path).toBe('/home/user/cache/foo.log');
  });

  it('rejects raw string (positional-arg mismatch)', () => {
    expect(ShellOpenPathArgs.parse('/etc/passwd').ok).toBe(false);
  });

  it('rejects empty path', () => {
    expect(ShellOpenPathArgs.parse({ path: '' }).ok).toBe(false);
  });

  it('rejects non-object args', () => {
    expect(ShellOpenPathArgs.parse(null).ok).toBe(false);
    expect(ShellOpenPathArgs.parse(undefined).ok).toBe(false);
  });

  it('rejects non-string path', () => {
    expect(ShellOpenPathArgs.parse({ path: 123 }).ok).toBe(false);
  });
});

describe('ShellOpenPathWithArgs', () => {
  it('accepts path plus a main-process-issued application id', () => {
    const result = ShellOpenPathWithArgs.parse({
      path: '/home/user/report.docx',
      applicationId: 'mac:com.kingsoft.wpsoffice.mac',
    });
    expect(result.ok).toBe(true);
  });

  it('rejects missing or empty application ids', () => {
    expect(ShellOpenPathWithArgs.parse({ path: '/tmp/report.docx' }).ok).toBe(false);
    expect(ShellOpenPathWithArgs.parse({
      path: '/tmp/report.docx',
      applicationId: '',
    }).ok).toBe(false);
  });
});

describe('UpdateStartDownloadArgs', () => {
  it('accepts version + force/reminder type', () => {
    expect(UpdateStartDownloadArgs.parse({ version: '0.23.59', type: 'force' }).ok).toBe(true);
    expect(UpdateStartDownloadArgs.parse({ version: '0.23.59', type: 'reminder' }).ok).toBe(true);
  });

  it('rejects empty version', () => {
    expect(UpdateStartDownloadArgs.parse({ version: '  ', type: 'reminder' }).ok).toBe(false);
  });

  it('rejects invalid type', () => {
    expect(UpdateStartDownloadArgs.parse({ version: '0.23.59', type: 'optional' }).ok).toBe(false);
  });
});

describe('FeedbackSubmitArgs', () => {
  it('accepts valid title + description', () => {
    expect(FeedbackSubmitArgs.parse({ title: 'Bug', description: 'Steps...' }).ok).toBe(true);
  });

  it('rejects title longer than 200 chars', () => {
    expect(FeedbackSubmitArgs.parse({ title: 'x'.repeat(201), description: 'ok' }).ok).toBe(false);
  });

  it('rejects description longer than 5000 chars', () => {
    expect(FeedbackSubmitArgs.parse({ title: 'ok', description: 'x'.repeat(5001) }).ok).toBe(false);
  });

  it('accepts up to 9 data-url images', () => {
    const images = Array.from({ length: 9 }, (_, i) => ({
      name: `img${i}.png`,
      dataUrl: 'data:image/png;base64,iVBORw0KGgo=',
    }));
    expect(FeedbackSubmitArgs.parse({ title: 'ok', description: 'd', images }).ok).toBe(true);
  });

  it('rejects 10+ images', () => {
    const images = Array.from({ length: 10 }, (_, i) => ({
      name: `img${i}.png`,
      dataUrl: 'data:image/png;base64,iVBORw0KGgo=',
    }));
    expect(FeedbackSubmitArgs.parse({ title: 'ok', description: 'd', images }).ok).toBe(false);
  });

  it('rejects image without data: prefix', () => {
    expect(
      FeedbackSubmitArgs.parse({
        title: 'ok',
        description: 'd',
        images: [{ name: 'x.png', dataUrl: 'http://evil.com/x.png' }],
      }).ok,
    ).toBe(false);
  });
});

describe('DialogSelectFileArgs', () => {
  it('accepts empty object (uses defaults)', () => {
    expect(DialogSelectFileArgs.parse({}).ok).toBe(true);
  });

  it('accepts multiSelect + filters + returnType', () => {
    const r = DialogSelectFileArgs.parse({
      multiSelect: true,
      filters: [{ name: 'Images', extensions: ['png', 'jpg'] }],
      returnType: 'dataUrl',
    });
    expect(r.ok).toBe(true);
  });

  it('rejects returnType outside enum', () => {
    expect(
      DialogSelectFileArgs.parse({ returnType: 'unknown' as unknown as 'paths' }).ok,
    ).toBe(false);
  });

  it('rejects maxBytes > MAX_DIALOG_FILE_BYTES', () => {
    expect(DialogSelectFileArgs.parse({ maxBytes: MAX_DIALOG_FILE_BYTES + 1 }).ok).toBe(false);
  });

  it('accepts maxBytes at the boundary', () => {
    expect(DialogSelectFileArgs.parse({ maxBytes: MAX_DIALOG_FILE_BYTES }).ok).toBe(true);
  });

  it('rejects non-array filters', () => {
    expect(DialogSelectFileArgs.parse({ filters: 'png' as unknown as never }).ok).toBe(false);
  });
});

describe('FeedbackListArgs', () => {
  it('accepts empty/undefined and applies defaults (page=1, size=20)', () => {
    const r = FeedbackListArgs.parse(undefined);
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.value.page).toBe(1);
      expect(r.value.size).toBe(20);
    }
  });

  it('rejects page <= 0 (the handler turns this into IPC_ARG_VALIDATION_FAILED)', () => {
    const r = FeedbackListArgs.parse({ page: -1 });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('page');
  });

  it('rejects size above the cap', () => {
    const r = FeedbackListArgs.parse({ size: 999999 });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('size');
  });

  it('accepts size at the boundary (100)', () => {
    const r = FeedbackListArgs.parse({ size: 100 });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.value.size).toBe(100);
  });

  it('accepts the status enum', () => {
    const r = FeedbackListArgs.parse({ page: 2, size: 50, status: 'PENDING' });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.value.page).toBe(2);
      expect(r.value.size).toBe(50);
      expect(r.value.status).toBe('PENDING');
    }
  });

  it('rejects an invalid status value', () => {
    const r = FeedbackListArgs.parse({ status: 'WOOF' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('status');
  });

  it('rejects non-integer page', () => {
    expect(FeedbackListArgs.parse({ page: 1.5 }).ok).toBe(false);
  });

  it('rejects non-object input', () => {
    expect(FeedbackListArgs.parse('string').ok).toBe(false);
    expect(FeedbackListArgs.parse(42).ok).toBe(false);
  });
});

describe('GatewayUploadArgs', () => {
  const validUrl = 'http://127.0.0.1:8000/api/wiki/upload?kb_id=default';

  it('accepts wiki upload url with absolute file paths', () => {
    const r = GatewayUploadArgs.parse({ url: validUrl, files: ['/home/u/docs/a.md', '/tmp/b.pdf'] });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.value.files).toEqual(['/home/u/docs/a.md', '/tmp/b.pdf']);
  });

  it('accepts windows drive / UNC absolute paths', () => {
    const r = GatewayUploadArgs.parse({ url: validUrl, files: ['C:\\docs\\a.md', 'D:/docs/b.md', '\\\\nas\\share\\c.md'] });
    expect(r.ok).toBe(true);
  });

  it('rejects non-upload pathname（非精确白名单端点）', () => {
    const r = GatewayUploadArgs.parse({ url: 'http://127.0.0.1:8000/api/wiki/ingest', files: ['/tmp/a.md'] });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('pathname');
    // /api/ 前缀也不再够用：其它 api 端点一律拒绝
    expect(GatewayUploadArgs.parse({ url: 'http://127.0.0.1:8000/api/sessions', files: ['/tmp/a.md'] }).ok).toBe(false);
  });

  it('rejects non-local hostname (SSRF)', () => {
    const r = GatewayUploadArgs.parse({ url: 'http://example.com/api/wiki/upload', files: ['/tmp/a.md'] });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('hostname');
  });

  it('rejects empty / missing files', () => {
    expect(GatewayUploadArgs.parse({ url: validUrl, files: [] }).ok).toBe(false);
    expect(GatewayUploadArgs.parse({ url: validUrl }).ok).toBe(false);
    expect(GatewayUploadArgs.parse({ url: validUrl, files: ['/tmp/a.md', 1] }).ok).toBe(false);
    expect(GatewayUploadArgs.parse({ url: validUrl, files: [''] }).ok).toBe(false);
  });

  it('rejects relative file paths', () => {
    const r = GatewayUploadArgs.parse({ url: validUrl, files: ['docs/a.md'] });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('absolute path');
    expect(GatewayUploadArgs.parse({ url: validUrl, files: ['./a.md'] }).ok).toBe(false);
    expect(GatewayUploadArgs.parse({ url: validUrl, files: ['~/a.md'] }).ok).toBe(false);
  });

  it('rejects too many files per request', () => {
    const files = Array.from({ length: 33 }, (_, i) => `/tmp/f${i}.md`);
    const r = GatewayUploadArgs.parse({ url: validUrl, files });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('files');
  });

  it('rejects invalid url / non-object input', () => {
    expect(GatewayUploadArgs.parse(null).ok).toBe(false);
    expect(GatewayUploadArgs.parse('x').ok).toBe(false);
    expect(GatewayUploadArgs.parse({ url: 'not-a-url', files: ['/tmp/a.md'] }).ok).toBe(false);
  });
});
