import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  authorizeOwnedImagePath,
  writeOwnedImageToClipboard,
} from '../../src/main/image-clipboard';

const OWNER = 'acct_0123456789abcdef';
let crewHome = '';
let imagePath = '';
let otherOwnerImagePath = '';
let ownerPrivateImagePath = '';

beforeAll(() => {
  crewHome = fs.mkdtempSync(path.join(os.tmpdir(), 'image-clipboard-test-'));
  const dir = path.join(crewHome, 'accounts', OWNER, 'task_workspaces', 'ws');
  fs.mkdirSync(dir, { recursive: true });
  imagePath = path.join(dir, 'shot.png');
  fs.writeFileSync(imagePath, Buffer.from('fake-png'));
  const otherDir = path.join(
    crewHome,
    'accounts',
    'acct_fedcba9876543210',
    'task_workspaces',
    'ws',
  );
  fs.mkdirSync(otherDir, { recursive: true });
  otherOwnerImagePath = path.join(otherDir, 'other.png');
  fs.writeFileSync(otherOwnerImagePath, Buffer.from('other'));
  ownerPrivateImagePath = path.join(crewHome, 'accounts', OWNER, 'private.png');
  fs.writeFileSync(ownerPrivateImagePath, Buffer.from('private'));
});

afterAll(() => fs.rmSync(crewHome, { recursive: true, force: true }));

describe('writeOwnedImageToClipboard', () => {
  it('专用图片授权器拒绝跨账号、账号私有根目录与 crewHome 外路径', () => {
    expect(authorizeOwnedImagePath(imagePath, crewHome, OWNER).filePath).toBe(fs.realpathSync(imagePath));
    expect(() => authorizeOwnedImagePath(otherOwnerImagePath, crewHome, OWNER))
      .toThrow('outside the current account');
    expect(() => authorizeOwnedImagePath(ownerPrivateImagePath, crewHome, OWNER))
      .toThrow('outside the current account');
    expect(() => authorizeOwnedImagePath('/tmp/arbitrary.png', crewHome, OWNER))
      .toThrow('outside the current account');
  });

  it('只解码当前账号授权目录内的图片并写入系统剪贴板', async () => {
    const decoded = { isEmpty: () => false, getSize: () => ({ width: 1200, height: 800 }) };
    const decode = vi.fn(() => decoded);
    const write = vi.fn();

    await expect(writeOwnedImageToClipboard(imagePath, crewHome, OWNER, { decode, write }))
      .resolves.toEqual({ ok: true });
    expect(decode).toHaveBeenCalledWith(Buffer.from('fake-png'));
    expect(write).toHaveBeenCalledWith(decoded);
  });

  it('拒绝未登录身份、越界路径与异常大像素图片', async () => {
    const decode = vi.fn(() => ({
      isEmpty: () => false,
      getSize: () => ({ width: 20_000, height: 20_000 }),
    }));
    const write = vi.fn();

    await expect(writeOwnedImageToClipboard(imagePath, crewHome, null, { decode, write }))
      .rejects.toThrow('outside the current account');
    await expect(writeOwnedImageToClipboard('/tmp/outside.png', crewHome, OWNER, { decode, write }))
      .rejects.toThrow('outside the current account');
    await expect(writeOwnedImageToClipboard(imagePath, crewHome, OWNER, { decode, write }))
      .rejects.toThrow('unsupported or oversized image');
    expect(write).not.toHaveBeenCalled();
  });

  it('账号在异步读取期间变化时不把旧账号图片写入剪贴板', async () => {
    const decoded = { isEmpty: () => false, getSize: () => ({ width: 10, height: 10 }) };
    let current = true;
    const decode = vi.fn(() => {
      current = false;
      return decoded;
    });
    const write = vi.fn();

    await expect(writeOwnedImageToClipboard(
      imagePath,
      crewHome,
      OWNER,
      { decode, write },
      () => current,
    )).rejects.toThrow('image account changed');
    expect(decode).toHaveBeenCalledOnce();
    expect(write).not.toHaveBeenCalled();
  });

  it('拒绝非有限或非整数的解码尺寸与超长 renderer 路径', async () => {
    const write = vi.fn();
    await expect(writeOwnedImageToClipboard(imagePath, crewHome, OWNER, {
      decode: () => ({ isEmpty: () => false, getSize: () => ({ width: Number.NaN, height: 10 }) }),
      write,
    })).rejects.toThrow('unsupported or oversized image');
    expect(() => authorizeOwnedImagePath('x'.repeat(4_097), crewHome, OWNER))
      .toThrow('invalid image path');
    expect(write).not.toHaveBeenCalled();
  });
});
