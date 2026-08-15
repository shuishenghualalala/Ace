import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { SelectedFileAuthority } from '../../src/main/selected-file-authority';

describe('SelectedFileAuthority', () => {
  let tempDir = '';
  let filePath = '';

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ace-selected-file-'));
    filePath = path.join(tempDir, 'upload.txt');
    fs.writeFileSync(filePath, 'approved');
  });

  afterEach(() => {
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  it('binds a one-shot grant to the renderer that used the native picker', async () => {
    const authority = new SelectedFileAuthority();
    await authority.authorize(7, filePath, 1024);

    await expect(authority.consume(8, filePath, 1024)).rejects.toThrow(
      'not selected by this renderer',
    );
    await expect(authority.consume(7, filePath, 1024)).resolves.toMatchObject({
      canonicalPath: fs.realpathSync(filePath),
      bytes: Buffer.from('approved'),
    });
    await expect(authority.consume(7, filePath, 1024)).rejects.toThrow(
      'not selected by this renderer',
    );
  });

  it('fails closed when the selected file changes before upload', async () => {
    const authority = new SelectedFileAuthority();
    await authority.authorize(7, filePath, 1024);
    fs.appendFileSync(filePath, '-changed');

    await expect(authority.consume(7, filePath, 1024)).rejects.toThrow(
      'identity changed',
    );
  });

  it('never follows a replacement path after picker authorization', async () => {
    const authority = new SelectedFileAuthority();
    await authority.authorize(7, filePath, 1024);
    const moved = path.join(tempDir, 'original.txt');
    fs.renameSync(filePath, moved);
    fs.writeFileSync(filePath, 'attacker-replacement');

    await expect(authority.consume(7, filePath, 1024)).rejects.toThrow(
      'identity changed',
    );
  });

  it('expires grants and enforces both picker and upload byte limits', async () => {
    let now = 100;
    const authority = new SelectedFileAuthority(50, () => now);
    await expect(authority.authorize(7, filePath, 2)).rejects.toThrow('FILE_TOO_LARGE');

    await authority.authorize(7, filePath, 1024);
    await expect(authority.consume(7, filePath, 2)).rejects.toThrow('FILE_TOO_LARGE');

    await authority.authorize(7, filePath, 1024);
    now = 151;
    await expect(authority.consume(7, filePath, 1024)).rejects.toThrow(
      'not selected by this renderer',
    );
  });

  it('rejects hard-linked selections', async () => {
    const linkPath = path.join(tempDir, 'second-link.txt');
    fs.linkSync(filePath, linkPath);
    const authority = new SelectedFileAuthority();

    await expect(authority.authorize(7, filePath, 1024)).rejects.toThrow(
      'single-link regular file',
    );
  });
});
