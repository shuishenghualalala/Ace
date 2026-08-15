import fs from 'node:fs';
import path from 'node:path';

interface FileIdentity {
  dev: number;
  ino: number;
  nlink: number;
  size: number;
  mtimeMs: number;
  ctimeMs: number;
}

interface SelectedFileGrant {
  canonicalPath: string;
  handle: fs.promises.FileHandle;
  identity: FileIdentity;
  maxBytes: number;
  expiresAt: number;
}

export interface ConsumedSelectedFile {
  canonicalPath: string;
  bytes: Buffer;
}

const DEFAULT_GRANT_TTL_MS = 5 * 60 * 1000;
const MAX_GRANTS_PER_RENDERER = 64;

function identityOf(stat: fs.Stats): FileIdentity {
  return {
    dev: stat.dev,
    ino: stat.ino,
    nlink: stat.nlink,
    size: stat.size,
    mtimeMs: stat.mtimeMs,
    ctimeMs: stat.ctimeMs,
  };
}

function sameIdentity(left: FileIdentity, right: fs.Stats): boolean {
  return (
    right.isFile()
    && left.dev === right.dev
    && left.ino === right.ino
    && left.nlink === right.nlink
    && left.size === right.size
    && left.mtimeMs === right.mtimeMs
    && left.ctimeMs === right.ctimeMs
  );
}

function comparablePath(value: string): string {
  const normalized = path.normalize(value);
  return process.platform === 'win32' ? normalized.toLowerCase() : normalized;
}

async function openRegularFileNoFollow(filePath: string): Promise<fs.promises.FileHandle> {
  const noFollow = process.platform !== 'win32' && typeof fs.constants.O_NOFOLLOW === 'number'
    ? fs.constants.O_NOFOLLOW
    : 0;
  return fs.promises.open(filePath, fs.constants.O_RDONLY | noFollow);
}

function closeGrant(grant: SelectedFileGrant | undefined): void {
  if (!grant) return;
  void grant.handle.close().catch(() => undefined);
}

/**
 * One-shot host authority for files explicitly selected through Electron's
 * native picker. Renderer-provided paths alone never create a grant.
 */
export class SelectedFileAuthority {
  private readonly grantsByRenderer = new Map<number, Map<string, SelectedFileGrant>>();

  constructor(
    private readonly ttlMs = DEFAULT_GRANT_TTL_MS,
    private readonly now: () => number = Date.now,
  ) {}

  async authorize(rendererId: number, filePath: string, maxBytes: number): Promise<string> {
    const absolute = path.resolve(filePath);
    const canonicalPath = await fs.promises.realpath(absolute);
    if (comparablePath(absolute) !== comparablePath(canonicalPath)) {
      throw new Error('selected file path must be canonical');
    }

    const handle = await openRegularFileNoFollow(canonicalPath);
    try {
      const postOpenCanonicalPath = await fs.promises.realpath(absolute);
      if (comparablePath(postOpenCanonicalPath) !== comparablePath(canonicalPath)) {
        throw new Error('selected file path changed while opening');
      }
      const stat = await handle.stat();
      if (!stat.isFile() || stat.nlink !== 1) {
        throw new Error('selected path is not a single-link regular file');
      }
      if (stat.size > maxBytes) {
        throw new Error(`FILE_TOO_LARGE: ${stat.size} > ${maxBytes}`);
      }

      const grants = this.prune(rendererId);
      const key = comparablePath(canonicalPath);
      closeGrant(grants.get(key));
      if (!grants.has(key) && grants.size >= MAX_GRANTS_PER_RENDERER) {
        const oldest = grants.keys().next().value as string | undefined;
        if (oldest) {
          closeGrant(grants.get(oldest));
          grants.delete(oldest);
        }
      }
      grants.set(key, {
        canonicalPath,
        handle,
        identity: identityOf(stat),
        maxBytes,
        expiresAt: this.now() + this.ttlMs,
      });
      return canonicalPath;
    } catch (error) {
      await handle.close();
      throw error;
    }
  }

  async consume(
    rendererId: number,
    filePath: string,
    hardMaxBytes: number,
  ): Promise<ConsumedSelectedFile> {
    const key = comparablePath(path.resolve(filePath));
    const grants = this.prune(rendererId);
    const grant = grants.get(key);
    if (!grant) {
      throw new Error('file was not selected by this renderer');
    }
    // Remove before any await below so concurrent/replayed IPC calls fail closed.
    grants.delete(key);
    if (grants.size === 0) this.grantsByRenderer.delete(rendererId);

    try {
      const stat = await grant.handle.stat();
      const effectiveMax = Math.min(grant.maxBytes, hardMaxBytes);
      if (!sameIdentity(grant.identity, stat)) {
        throw new Error('selected file identity changed');
      }
      if (stat.size > effectiveMax) {
        throw new Error(`FILE_TOO_LARGE: ${stat.size} > ${effectiveMax}`);
      }
      const bytes = await grant.handle.readFile();
      if (bytes.byteLength !== stat.size) {
        throw new Error('selected file size changed while reading');
      }
      return { canonicalPath: grant.canonicalPath, bytes };
    } finally {
      await grant.handle.close();
    }
  }

  clearRenderer(rendererId: number): void {
    for (const grant of this.grantsByRenderer.get(rendererId)?.values() ?? []) {
      closeGrant(grant);
    }
    this.grantsByRenderer.delete(rendererId);
  }

  private prune(rendererId: number): Map<string, SelectedFileGrant> {
    const grants = this.grantsByRenderer.get(rendererId) ?? new Map<string, SelectedFileGrant>();
    const now = this.now();
    for (const [key, grant] of grants) {
      if (grant.expiresAt <= now) {
        closeGrant(grant);
        grants.delete(key);
      }
    }
    if (grants.size > 0 || !this.grantsByRenderer.has(rendererId)) {
      this.grantsByRenderer.set(rendererId, grants);
    }
    return grants;
  }
}

export const selectedFileAuthority = new SelectedFileAuthority();
