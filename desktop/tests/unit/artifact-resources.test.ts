import { link, mkdir, mkdtemp, realpath, rename, rm, stat, symlink, truncate, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readArtifactResource, type ArtifactResourceRoot } from '../../src/main/browser/artifact-resources';

describe('local HTML preview resources', () => {
  let temporary: string;
  let root: ArtifactResourceRoot;
  beforeEach(async () => {
    temporary = await realpath(await mkdtemp(path.join(os.tmpdir(), 'crew-preview-assets-')));
    const directory = path.join(temporary, 'dist');
    await mkdir(directory);
    const info = await stat(directory);
    root = { directory, dev: info.dev, ino: info.ino };
  });
  afterEach(async () => { await rm(temporary, { recursive: true, force: true }); });

  it('loads nested and URL-encoded static files with the right MIME types', async () => {
    await mkdir(path.join(root.directory, 'assets'));
    await writeFile(path.join(root.directory, 'assets/样式 表.css'), '.app { display: grid; }');
    const resource = await readArtifactResource(root, '/assets/%E6%A0%B7%E5%BC%8F%20%E8%A1%A8.css');
    expect(resource?.contentType).toBe('text/css; charset=utf-8');
    expect(new TextDecoder().decode(resource?.content)).toBe('.app { display: grid; }');
  });

  it('rejects parent traversal, hidden files, alternate streams, and unsupported file types', async () => {
    await writeFile(path.join(temporary, 'outside.css'), 'private');
    await writeFile(path.join(root.directory, '.env.json'), '{"private":true}');
    await writeFile(path.join(root.directory, 'secret.txt'), 'private');
    for (const name of ['/../outside.css', '/%2e%2e%2foutside.css', '/.env.json', '/secret.txt', '/a.css:secret', '/%00.css', '/%zz.css', '/assets\\outside.css']) {
      expect(await readArtifactResource(root, name)).toBeNull();
    }
  });

  it('rejects symlink and hardlink escapes', async () => {
    const outside = path.join(temporary, 'outside.css');
    await writeFile(outside, 'private');
    await symlink(outside, path.join(root.directory, 'linked.css'));
    await link(outside, path.join(root.directory, 'hardlinked.css'));
    expect(await readArtifactResource(root, '/linked.css')).toBeNull();
    expect(await readArtifactResource(root, '/hardlinked.css')).toBeNull();
  });

  it('rejects replacement of the directory granted when the preview opened', async () => {
    await rename(root.directory, `${root.directory}-old`);
    await mkdir(root.directory);
    await writeFile(path.join(root.directory, 'app.js'), 'replacement');
    expect(await readArtifactResource(root, '/app.js')).toBeNull();
  });

  it('bounds resource reads and allows an empty stylesheet', async () => {
    const file = path.join(root.directory, 'styles.css');
    await writeFile(file, '');
    expect((await readArtifactResource(root, '/styles.css'))?.content.byteLength).toBe(0);
    await truncate(file, 20 * 1024 * 1024 + 1);
    expect(await readArtifactResource(root, '/styles.css')).toBeNull();
  });
});
