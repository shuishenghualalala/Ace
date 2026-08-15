import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { createHash, generateKeyPairSync, sign } from 'crypto';
import { afterAll, describe, expect, it } from 'vitest';
import {
  canonicalUpdateSignaturePayload,
  openVerifiedUpdateArtifact,
  packageKindFromPath,
  verifyPackageIntegrity,
  verifyPackageSignature,
} from '../../src/main/update/update-integrity';

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
    expect(packageKindFromPath('/a/crew-desktop.dmg')).toBe('dmg');
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
    {
      name: 'dmg: 正确 koly trailer 通过',
      file: 'ok.dmg',
      data: Buffer.concat([Buffer.from('payload'), Buffer.from('koly'), Buffer.alloc(508)]),
      ok: true,
      message: undefined,
    },
    {
      name: 'dmg: 缺少 koly trailer 不通过',
      file: 'bad.dmg',
      data: Buffer.alloc(512),
      ok: false,
      message: 'koly',
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
    const missing = path.join(os.tmpdir(), 'mw-does-not-exist-xyz.exe');
    const r = verifyPackageIntegrity(missing, 0);
    expect(r.ok).toBe(false);
    expect(r.message).toBe('安装包完整性校验失败');
    expect(r.message).not.toContain(missing);
  });

  it('不支持的格式不通过', () => {
    const p = writeTemp('weird.tar', Buffer.from('whatever'));
    const r = verifyPackageIntegrity(p, 0);
    expect(r.ok).toBe(false);
  });
});

describe('verifyPackageSignature', () => {
  it('binds a signed envelope to the exact version, filename, size, and package hash', () => {
    const packagePath = writeTemp(
      'signed-package.exe',
      Buffer.concat([Buffer.from('MZ'), Buffer.from('signed payload')]),
    );
    const { privateKey, publicKey } = generateKeyPairSync('ed25519');
    const metadata = {
      schema: 1 as const,
      version: '1.2.3',
      filename: path.basename(packagePath),
      package_sha256: createHash('sha256').update(fs.readFileSync(packagePath)).digest('hex'),
      package_size: fs.statSync(packagePath).size,
    };
    const signature = sign(
      null,
      canonicalUpdateSignaturePayload(metadata),
      privateKey,
    ).toString('base64');
    const signaturePath = writeTemp(
      'signed-package.exe.sig',
      Buffer.from(JSON.stringify({ ...metadata, signature }), 'utf8'),
    );
    const publicKeyDer = publicKey.export({ format: 'der', type: 'spki' }).toString('base64');

    expect(verifyPackageSignature(packagePath, signaturePath, publicKeyDer, '1.2.3')).toEqual({
      ok: true,
    });
    expect(verifyPackageSignature(packagePath, signaturePath, publicKeyDer, '1.2.4').ok).toBe(false);

    fs.appendFileSync(packagePath, Buffer.from('tampered'));
    expect(verifyPackageSignature(packagePath, signaturePath, publicKeyDer, '1.2.3').ok).toBe(false);
  });

  it('fails closed when the detached signature is missing', () => {
    const packagePath = writeTemp(
      'unsigned-package.exe',
      Buffer.concat([Buffer.from('MZ'), Buffer.from('unsigned payload')]),
    );
    const { publicKey } = generateKeyPairSync('ed25519');
    const publicKeyDer = publicKey.export({ format: 'der', type: 'spki' }).toString('base64');

    const result = verifyPackageSignature(
      packagePath,
      `${packagePath}.missing.sig`,
      publicKeyDer,
      '1.2.3',
    );
    expect(result.ok).toBe(false);
    expect(result.message).toBe('更新包签名校验失败');
    expect(result.message).not.toContain(packagePath);
  });

  it('rejects hardlinked packages before signature verification', () => {
    const packagePath = writeTemp(
      'hardlinked-package.exe',
      Buffer.concat([Buffer.from('MZ'), Buffer.from('payload')]),
    );
    const hardlinkPath = `${packagePath}.link.exe`;
    fs.linkSync(packagePath, hardlinkPath);
    tmpFiles.push(hardlinkPath);
    const { publicKey } = generateKeyPairSync('ed25519');
    const publicKeyDer = publicKey.export({ format: 'der', type: 'spki' }).toString('base64');

    expect(
      verifyPackageSignature(
        packagePath,
        `${packagePath}.missing.sig`,
        publicKeyDer,
        '1.2.3',
      ).ok,
    ).toBe(false);
  });

  it('holds verified descriptors and detects a verify-to-launch path swap', () => {
    const packageBytes = Buffer.concat([
      Buffer.from('MZ'),
      Buffer.from('launch-bound payload'),
    ]);
    const packagePath = writeTemp('launch-bound.exe', packageBytes);
    const { privateKey, publicKey } = generateKeyPairSync('ed25519');
    const metadata = {
      schema: 1 as const,
      version: '1.2.3',
      filename: path.basename(packagePath),
      package_sha256: createHash('sha256').update(packageBytes).digest('hex'),
      package_size: packageBytes.length,
    };
    const signature = sign(
      null,
      canonicalUpdateSignaturePayload(metadata),
      privateKey,
    ).toString('base64');
    const signaturePath = writeTemp(
      'launch-bound.exe.sig',
      Buffer.from(JSON.stringify({ ...metadata, signature })),
    );
    const publicKeyDer = publicKey.export({ format: 'der', type: 'spki' }).toString('base64');
    const lease = openVerifiedUpdateArtifact(
      packagePath,
      signaturePath,
      publicKeyDer,
      '1.2.3',
    );

    try {
      const displaced = `${packagePath}.verified`;
      try {
        fs.renameSync(packagePath, displaced);
        tmpFiles.push(displaced);
      } catch (error) {
        // Windows normally blocks replacement while the verification handle is open.
        expect((error as NodeJS.ErrnoException).code).toMatch(/^(?:EACCES|EPERM)$/);
        expect(() => lease.revalidate()).not.toThrow();
        return;
      }
      fs.writeFileSync(packagePath, packageBytes);
      expect(() => lease.revalidate()).toThrow(/replaced/);
    } finally {
      lease.close();
    }
  });
});
