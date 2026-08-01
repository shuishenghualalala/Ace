/** crew-file 协议路径边界（resolveCrewFilePath）单测。 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  readResolvedCrewFile,
  resolveCrewFilePath,
} from '../../src/main/crew-file-protocol';

let crewHome = '';
let pngPath = '';
let uploadPath = '';
let tmpPath = '';
const OWNER_SEGMENT = 'acct_0123456789abcdef';

beforeAll(() => {
  crewHome = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-file-test-'));
  const dir = path.join(crewHome, 'accounts', OWNER_SEGMENT, 'task_workspaces', 'ws1', 'downloads', 'browser');
  fs.mkdirSync(dir, { recursive: true });
  pngPath = path.join(dir, 'shot.png');
  fs.writeFileSync(pngPath, Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  const uploads = path.join(crewHome, 'accounts', OWNER_SEGMENT, 'uploads');
  fs.mkdirSync(uploads, { recursive: true });
  uploadPath = path.join(uploads, 'attachment.png');
  fs.writeFileSync(uploadPath, Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  const tmpDir = path.join(crewHome, 'tmp', 'feishu-files');
  fs.mkdirSync(tmpDir, { recursive: true });
  tmpPath = path.join(tmpDir, 'channel-image.png');
  fs.writeFileSync(tmpPath, Buffer.from([0x89, 0x50, 0x4e, 0x47]));
});

afterAll(() => {
  fs.rmSync(crewHome, { recursive: true, force: true });
});

const urlOf = (p: string) => `crew-file://img/${encodeURIComponent(p)}`;

describe('resolveCrewFilePath', () => {
  it('放行 task_workspaces 内的图片文件', () => {
    const resolved = resolveCrewFilePath(urlOf(pngPath), crewHome, OWNER_SEGMENT);
    expect(resolved?.filePath).toBe(fs.realpathSync(pngPath));
    expect(resolved?.contentType).toBe('image/png');
  });

  it('放行当前账号 uploads 内的图片附件', () => {
    const resolved = resolveCrewFilePath(urlOf(uploadPath), crewHome, OWNER_SEGMENT);
    expect(resolved?.filePath).toBe(fs.realpathSync(uploadPath));
    expect(resolved?.identity.size).toBe(4);
  });

  it('放行 crewHome/tmp 下的渠道入站图片', () => {
    const resolved = resolveCrewFilePath(urlOf(tmpPath), crewHome, OWNER_SEGMENT);
    expect(resolved?.filePath).toBe(fs.realpathSync(tmpPath));
    expect(resolved?.contentType).toBe('image/png');
  });

  it('拒绝 tmp 目录通过符号链接逃逸到 crewHome 外', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-file-tmp-link-'));
    const outside = path.join(root, 'accounts', OWNER_SEGMENT);
    const tmpLink = path.join(root, 'tmp');
    fs.mkdirSync(outside, { recursive: true });
    try {
      fs.symlinkSync(os.tmpdir(), tmpLink, 'dir');
    } catch {
      fs.rmSync(root, { recursive: true, force: true });
      return; // 平台不支持 symlink 时跳过
    }
    const escaped = path.join(tmpLink, 'escaped.png');
    fs.writeFileSync(escaped, 'x');
    try {
      expect(resolveCrewFilePath(urlOf(escaped), root, OWNER_SEGMENT)).toBeNull();
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
      fs.rmSync(escaped, { force: true });
    }
  });

  it('拒绝 task_workspaces 之外的路径', () => {
    const outside = path.join(crewHome, 'accounts', OWNER_SEGMENT, 'secret.png');
    fs.writeFileSync(outside, 'x');
    expect(resolveCrewFilePath(urlOf(outside), crewHome, OWNER_SEGMENT)).toBeNull();
  });

  it('拒绝 crewHome 之外的路径', () => {
    expect(resolveCrewFilePath(urlOf('/etc/passwd.png'), crewHome, OWNER_SEGMENT)).toBeNull();
  });

  it('拒绝目录遍历', () => {
    const traversal = path.join(crewHome, 'accounts', OWNER_SEGMENT, 'task_workspaces', '..', '..', 'secret.png');
    expect(resolveCrewFilePath(urlOf(traversal), crewHome, OWNER_SEGMENT)).toBeNull();
  });

  it('拒绝非图片扩展名与不存在的文件', () => {
    const txt = path.join(crewHome, 'accounts', OWNER_SEGMENT, 'task_workspaces', 'ws1', 'note.txt');
    fs.writeFileSync(txt, 'x');
    expect(resolveCrewFilePath(urlOf(txt), crewHome, OWNER_SEGMENT)).toBeNull();
    expect(
      resolveCrewFilePath(
        urlOf(path.join(crewHome, 'accounts', OWNER_SEGMENT, 'task_workspaces', 'ws1', 'missing.png')),
        crewHome,
        OWNER_SEGMENT,
      ),
    ).toBeNull();
  });

  it('拒绝符号链接逃逸', () => {
    const outside = path.join(os.tmpdir(), 'crew-file-outside.png');
    fs.writeFileSync(outside, 'x');
    const link = path.join(crewHome, 'accounts', OWNER_SEGMENT, 'task_workspaces', 'ws1', 'link.png');
    try {
      fs.symlinkSync(outside, link);
    } catch {
      return; // 平台不支持 symlink 时跳过
    }
    expect(resolveCrewFilePath(urlOf(link), crewHome, OWNER_SEGMENT)).toBeNull();
    fs.rmSync(outside, { force: true });
  });

  it('拒绝错误协议与带 query 的 URL', () => {
    expect(resolveCrewFilePath(`file://${pngPath}`, crewHome, OWNER_SEGMENT)).toBeNull();
    expect(resolveCrewFilePath(`${urlOf(pngPath)}?x=1`, crewHome, OWNER_SEGMENT)).toBeNull();
  });

  it('只允许当前账号目录', () => {
    const other = path.join(
      crewHome,
      'accounts',
      'acct_fedcba9876543210',
      'task_workspaces',
      'ws2',
      'other.png',
    );
    fs.mkdirSync(path.dirname(other), { recursive: true });
    fs.writeFileSync(other, 'other');
    expect(resolveCrewFilePath(urlOf(other), crewHome, OWNER_SEGMENT)).toBeNull();
  });

  it('拒绝用当前账号目录内的硬链接别名读取其它账号 inode', () => {
    const other = path.join(
      crewHome,
      'accounts',
      'acct_fedcba9876543210',
      'task_workspaces',
      'ws-hardlink',
      'other.png',
    );
    fs.mkdirSync(path.dirname(other), { recursive: true });
    fs.writeFileSync(other, 'other');
    const alias = path.join(path.dirname(pngPath), 'other-hardlink.png');
    try {
      fs.linkSync(other, alias);
    } catch {
      return; // filesystem does not support hard links
    }
    expect(resolveCrewFilePath(urlOf(alias), crewHome, OWNER_SEGMENT)).toBeNull();
  });

  it('拒绝当前账号目录链接到同一 accounts 下的另一个账号', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-file-owner-link-'));
    const otherOwner = path.join(root, 'accounts', 'acct_fedcba9876543210');
    const target = path.join(otherOwner, 'task_workspaces', 'ws', 'other.png');
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, 'other');
    const activeOwner = path.join(root, 'accounts', OWNER_SEGMENT);
    try {
      fs.symlinkSync(otherOwner, activeOwner, 'dir');
    } catch {
      fs.rmSync(root, { recursive: true, force: true });
      return;
    }
    try {
      const throughActiveOwner = path.join(activeOwner, 'task_workspaces', 'ws', 'other.png');
      expect(resolveCrewFilePath(urlOf(throughActiveOwner), root, OWNER_SEGMENT)).toBeNull();
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('拒绝 task_workspaces 链接到账号目录内的其它位置', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-file-task-link-'));
    const owner = path.join(root, 'accounts', OWNER_SEGMENT);
    const privateRoot = path.join(owner, 'private-images');
    const target = path.join(privateRoot, 'ws', 'private.png');
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, 'private');
    const taskRoot = path.join(owner, 'task_workspaces');
    try {
      fs.symlinkSync(privateRoot, taskRoot, 'dir');
    } catch {
      fs.rmSync(root, { recursive: true, force: true });
      return;
    }
    try {
      const throughTaskRoot = path.join(taskRoot, 'ws', 'private.png');
      expect(resolveCrewFilePath(urlOf(throughTaskRoot), root, OWNER_SEGMENT)).toBeNull();
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('接受 canonical 候选路径与非 canonical crewHome alias', () => {
    const aliasRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-file-alias-'));
    const alias = path.join(aliasRoot, 'home-link');
    try {
      fs.symlinkSync(crewHome, alias, 'dir');
    } catch {
      fs.rmSync(aliasRoot, { recursive: true, force: true });
      return;
    }
    try {
      const resolved = resolveCrewFilePath(
        urlOf(fs.realpathSync(pngPath)),
        alias,
        OWNER_SEGMENT,
      );
      expect(resolved?.filePath).toBe(fs.realpathSync(pngPath));
    } finally {
      fs.rmSync(aliasRoot, { recursive: true, force: true });
    }
  });

  it('打开前路径被换成符号链接时拒绝读取', async () => {
    const victim = path.join(path.dirname(pngPath), 'race.png');
    const outside = path.join(os.tmpdir(), `crew-file-race-${process.pid}.png`);
    fs.writeFileSync(victim, 'inside');
    fs.writeFileSync(outside, 'outside');
    const resolved = resolveCrewFilePath(urlOf(victim), crewHome, OWNER_SEGMENT);
    expect(resolved).not.toBeNull();
    fs.renameSync(victim, `${victim}.old`);
    try {
      fs.symlinkSync(outside, victim);
    } catch {
      fs.renameSync(`${victim}.old`, victim);
      fs.rmSync(outside, { force: true });
      return;
    }
    try {
      await expect(readResolvedCrewFile(resolved!)).rejects.toThrow();
    } finally {
      fs.rmSync(victim, { force: true });
      fs.renameSync(`${victim}.old`, victim);
      fs.rmSync(outside, { force: true });
    }
  });

  it('拒绝授权后同 inode 同长度的原地改写', async () => {
    const victim = path.join(path.dirname(pngPath), 'in-place.png');
    fs.writeFileSync(victim, 'inside');
    const resolved = resolveCrewFilePath(urlOf(victim), crewHome, OWNER_SEGMENT);
    expect(resolved).not.toBeNull();
    fs.writeFileSync(victim, 'change');
    const changedAt = new Date(resolved!.identity.mtimeMs + 2_000);
    fs.utimesSync(victim, changedAt, changedAt);
    await expect(readResolvedCrewFile(resolved!)).rejects.toThrow('identity changed');
  });
});
