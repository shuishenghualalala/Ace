import { constants, realpathSync } from 'node:fs';
import { lstat, open, stat } from 'node:fs/promises';
import path from 'node:path';

/** A preview may read static assets beside its approved HTML, never its parents. */
export interface ArtifactResourceRoot {
  directory: string;
  dev: number;
  ino: number;
}

const MAX_RESOURCE_BYTES = 20 * 1024 * 1024;
const CONTENT_TYPES: Record<string, string> = {
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.wasm': 'application/wasm',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf',
  '.mp3': 'audio/mpeg', '.mp4': 'video/mp4', '.webm': 'video/webm',
};

export async function readArtifactResource(
  root: ArtifactResourceRoot,
  pathname: string,
): Promise<{ content: ArrayBuffer; contentType: string } | null> {
  let handle: Awaited<ReturnType<typeof open>> | undefined;
  try {
    const relative = decodeURIComponent(pathname).replace(/^\//, '');
    const segments = relative.split('/');
    if (!relative || /[\\\0:]/.test(relative) || segments.some((segment) => !segment || segment.startsWith('.'))) return null;
    const contentType = CONTENT_TYPES[path.extname(relative).toLowerCase()];
    if (!contentType) return null;
    const file = path.join(root.directory, ...segments);
    // Reject symlinked files/ancestors and replacement of the granted directory.
    if (realpathSync.native(file) !== file || realpathSync.native(root.directory) !== root.directory) return null;
    const [info, directoryInfo] = await Promise.all([lstat(file), stat(root.directory)]);
    if (directoryInfo.dev !== root.dev || directoryInfo.ino !== root.ino
      || !info.isFile() || info.nlink !== 1 || info.size > MAX_RESOURCE_BYTES) return null;
    const matches = (value: typeof info): boolean => value.isFile()
      && value.dev === info.dev && value.ino === info.ino && value.nlink === 1
      && value.size === info.size && value.mtimeMs === info.mtimeMs && value.ctimeMs === info.ctimeMs;
    handle = await open(file, constants.O_RDONLY | (process.platform === 'win32' ? 0 : constants.O_NOFOLLOW));
    if (!matches(await handle.stat()) || realpathSync.native(file) !== file) return null;
    const bytes = Buffer.alloc(info.size);
    let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await handle.read(bytes, offset, bytes.length - offset, offset);
      if (!bytesRead) break;
      offset += bytesRead;
    }
    const extra = Buffer.alloc(1);
    const { bytesRead: extraBytes } = await handle.read(extra, 0, 1, info.size);
    const finalDirectory = await stat(root.directory);
    if (offset !== bytes.length || extraBytes || !matches(await handle.stat())
      || finalDirectory.dev !== root.dev || finalDirectory.ino !== root.ino
      || realpathSync.native(file) !== file) return null;
    return { content: Uint8Array.from(bytes).buffer, contentType };
  } catch {
    return null;
  } finally {
    await handle?.close().catch(() => undefined);
  }
}
