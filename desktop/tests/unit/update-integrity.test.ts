import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { afterAll, describe, expect, it } from 'vitest';
import { packageKindFromPath, verifyPackageIntegrity } from '../../src/main/update/update-integrity';

const tmpFiles: string[] = [];

function writeTemp(name: string, data: Buffer): string {
  const p = path.join(os.tmpdir(), `mw-integrity-${name}`);
  fs.writeFileSync(p, data);
  tmpFiles.push(p);
  return p;
}

afterAll(() => {
  for (const p of tmpFiles) {
    try {
      fs.rmSync(p, { force: true });
    } catch {
      /* ignore */
    }
  }
});

describe('packageKindFromPath', () => {
  it('按后缀分类', () => {
    expect(packageKindFromPath('/a/Setup.exe')).toBe('exe');
    expect(packageKindFromPath('/a/crew-desktop.deb')).toBe('deb');
    expect(packageKindFromPath('/a/pkg.tar.gz')).toBe('unknown');
  });
});

describe('verifyPackageIntegrity', () => {
  it.each([
    {
      name: 'exe: 正确 MZ 头通过',
      file: 'ok.exe',
      data: Buffer.concat([Buffer.from('MZ'), Buffer.from('rest-of-pe')]),
      ok: true,
      message: undefined,
    },
    {
      name: 'exe: 错误头不通过',
      file: 'bad.exe',
      data: Buffer.from('XXcorrupt'),
      ok: false,
      message: 'MZ',
    },
    {
      name: 'deb: 正确 !<arch> 头通过',
      file: 'ok.deb',
      data: Buffer.concat([Buffer.from('!<arch>\n'), Buffer.from('control')]),
      ok: true,
      message: undefined,
    },
    {
      name: 'deb: 错误头不通过',
      file: 'bad.deb',
      data: Buffer.from('not-an-archive'),
      ok: false,
      message: 'arch',
    },
  ] as Array<{ name: string; file: string; data: Buffer; ok: boolean; message?: string }>)(
    '$name',
    ({ file, data, ok, message }) => {
      const p = writeTemp(file, data);
      const r = verifyPackageIntegrity(p, 0);
      expect(r.ok).toBe(ok);
      if (message !== undefined) {
        expect(r.message).toContain(message);
      }
    },
  );

  it('size 不符（下载不完整）不通过', () => {
    const p = writeTemp('truncated.exe', Buffer.concat([Buffer.from('MZ'), Buffer.from('x')]));
    const r = verifyPackageIntegrity(p, 1024); // 期望 1024，实际仅 3
    expect(r.ok).toBe(false);
    expect(r.message).toContain('大小不符');
  });

  it('文件不存在不通过', () => {
    const r = verifyPackageIntegrity(path.join(os.tmpdir(), 'mw-does-not-exist-xyz.exe'), 0);
    expect(r.ok).toBe(false);
  });

  it('不支持的格式不通过', () => {
    const p = writeTemp('weird.tar', Buffer.from('whatever'));
    const r = verifyPackageIntegrity(p, 0);
    expect(r.ok).toBe(false);
  });
});
