/** crew-file:// 私有协议：把当前账号拥有的任务图片（如 browser_use screenshot、
 *  用户上传附件以及各渠道入站的临时图片）安全地喂给对话 UI 的 <img>，不开放任意 file:// 访问。
 *
 * 安全边界：
 * - 只允许 GET / HEAD；
 * - 路径必须落在 <crewHome>/accounts/<acct>/{task_workspaces,uploads}/ 或
 *   <crewHome>/tmp/ 之内（resolve + relative 校验，realpath 后再校验一次，拒绝符号链接逃逸）；
 * - 仅放行常见图片扩展名，Content-Type 按扩展名给定并加 nosniff。
 */

import { protocol } from 'electron';
import fs from 'node:fs';
import path from 'node:path';

export const CREW_FILE_SCHEME = 'crew-file';
const CREW_FILE_HOST = 'img';
const REENCODED_PATH_SEPARATOR = /%(?:25)*(?:2f|5c)/i;
const WINDOWS_RESERVED_NAMES = new Set([
  'CON', 'PRN', 'AUX', 'NUL',
  ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`),
]);

const IMAGE_CONTENT_TYPES: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

function hasControlCharacter(value: string): boolean {
  for (const character of value) {
    const code = character.charCodeAt(0);
    if (code < 0x20 || (code >= 0x7f && code <= 0x9f)) return true;
  }
  return false;
}

function isWithin(base: string, target: string): boolean {
  const relative = path.relative(base, target);
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function samePath(left: string, right: string): boolean {
  return process.platform === 'win32'
    ? path.resolve(left).toLocaleLowerCase() === path.resolve(right).toLocaleLowerCase()
    : path.resolve(left) === path.resolve(right);
}

function isSafeLocalPathSyntax(value: string): boolean {
  if (!value || hasControlCharacter(value)) return false;

  const normalized = value.replace(/\\/g, '/');
  const lowerNormalized = normalized.toLowerCase();
  if (
    normalized.startsWith('//')
    || lowerNormalized.startsWith('/??/')
  ) return false;

  const hasDrive = /^[A-Za-z]:/.test(value);
  if (hasDrive) {
    if (!/^[A-Za-z]:[\\/]/.test(value) || process.platform !== 'win32') return false;
  } else if (process.platform === 'win32') {
    // Drive-less rooted paths bind to the current drive and are ambiguous.
    return false;
  } else if (value.includes('\\')) {
    // Backslash is a Windows separator, not a portable POSIX URI character.
    return false;
  }

  if (process.platform === 'win32') {
    const remainder = normalized.slice(2);
    if (remainder.includes(':')) return false;
    for (const component of remainder.split('/')) {
      if (!component || component === '.' || component === '..') continue;
      if (
        component.endsWith(' ')
        || component.endsWith('.')
        || /[<>"|?*]/.test(component)
        || WINDOWS_RESERVED_NAMES.has(component.split('.', 1)[0].toUpperCase())
      ) return false;
    }
  }

  return path.isAbsolute(value);
}

function decodeCrewFilePath(rawUrl: string): string | null {
  if (typeof rawUrl !== 'string' || rawUrl.length > 16_384 || hasControlCharacter(rawUrl)) {
    return null;
  }
  // The producer percent-encodes backslashes. Rejecting literal ones avoids
  // URL-parser normalization turning a non-producer URI into another path.
  if (rawUrl.includes('\\')) return null;

  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (
    parsed.protocol !== `${CREW_FILE_SCHEME}:`
    || parsed.hostname !== CREW_FILE_HOST
    || parsed.username !== ''
    || parsed.password !== ''
    || parsed.port !== ''
    || parsed.search
    || parsed.hash
    || !parsed.pathname.startsWith('/')
  ) return null;

  const encodedPath = parsed.pathname.slice(1);
  // encodeURIComponent(absPath) emits one opaque path component after the
  // placeholder host. Literal separators would be a different URI grammar.
  if (!encodedPath || encodedPath.includes('/')) return null;

  let candidate: string;
  try {
    candidate = decodeURIComponent(encodedPath);
  } catch {
    return null;
  }
  if (REENCODED_PATH_SEPARATOR.test(candidate) || !isSafeLocalPathSyntax(candidate)) {
    return null;
  }
  return candidate;
}

export interface ResolvedOwnedFile {
  filePath: string;
  identity: {
    dev: number;
    ino: number;
    nlink: number;
    size: number;
    mtimeMs: number;
    ctimeMs: number;
  };
}

export interface ResolvedCrewFile extends ResolvedOwnedFile {
  contentType: string;
}

function resolveCanonicalOwnedRoot(ownerRoot: string, segment: 'task_workspaces' | 'uploads'): string | null {
  try {
    const candidate = path.join(ownerRoot, segment);
    const real = fs.realpathSync(candidate);
    return samePath(real, candidate) && isWithin(ownerRoot, real) ? real : null;
  } catch {
    return null;
  }
}

function resolveCanonicalCrewHomeRoot(crewHomeRoot: string, segment: string): string | null {
  try {
    const candidate = path.join(crewHomeRoot, segment);
    const real = fs.realpathSync(candidate);
    return samePath(real, candidate) && isWithin(crewHomeRoot, real) ? real : null;
  } catch {
    return null;
  }
}

/**
 * Resolve a renderer-provided existing file against the current account's
 * canonical task/upload roots. The returned path and inode identity are the
 * only values callers may use for subsequent file operations.
 */
export function resolveOwnedFilePath(
  rawPath: string,
  crewHome: string,
  ownerSegment: string,
  includeSharedTmp = false,
): ResolvedOwnedFile | null {
  if (
    typeof rawPath !== 'string'
    || typeof crewHome !== 'string'
    || typeof ownerSegment !== 'string'
    || rawPath.length === 0
    || rawPath.length > 4_096
    || crewHome.length > 4_096
    || ownerSegment.length > 64
    || !isSafeLocalPathSyntax(rawPath)
    || !/^acct_[0-9a-f]{16}$/i.test(ownerSegment)
  ) {
    return null;
  }

  const accountsRoot = path.join(path.resolve(crewHome), 'accounts');
  const resolved = path.resolve(rawPath);
  let real: string;
  let realAccountsRoot: string;
  let realOwnerRoot: string;
  try {
    realAccountsRoot = fs.realpathSync(accountsRoot);
    realOwnerRoot = fs.realpathSync(path.join(realAccountsRoot, ownerSegment));
    real = fs.realpathSync(resolved);
  } catch {
    return null;
  }
  if (!samePath(realOwnerRoot, path.join(realAccountsRoot, ownerSegment))) return null;
  if (!isWithin(realAccountsRoot, realOwnerRoot)) return null;

  const allowedRoots = [
    resolveCanonicalOwnedRoot(realOwnerRoot, 'task_workspaces'),
    resolveCanonicalOwnedRoot(realOwnerRoot, 'uploads'),
    ...(includeSharedTmp
      ? [resolveCanonicalCrewHomeRoot(path.dirname(realAccountsRoot), 'tmp')]
      : []),
  ].filter((root): root is string => Boolean(root));
  if (!allowedRoots.some((root) => isWithin(root, real))) return null;

  try {
    const fileStat = fs.statSync(real);
    if (!fileStat.isFile() || fileStat.nlink !== 1) return null;
    return {
      filePath: real,
      identity: {
        dev: fileStat.dev,
        ino: fileStat.ino,
        nlink: fileStat.nlink,
        size: fileStat.size,
        mtimeMs: fileStat.mtimeMs,
        ctimeMs: fileStat.ctimeMs,
      },
    };
  } catch {
    return null;
  }
}

/** 把 crew-file:///<encodeURIComponent(绝对路径)> 解析成允许服出的本地文件。
 *  不合法 / 越界 / 扩展名不允许 / 非常规文件时返回 null（调用方回 404）。 */
export function resolveCrewFilePath(
  rawUrl: string,
  crewHome: string,
  ownerSegment: string,
): ResolvedCrewFile | null {
  if (typeof crewHome !== 'string' || typeof ownerSegment !== 'string') return null;
  if (crewHome.length > 4_096 || ownerSegment.length > 64) return null;
  const candidate = decodeCrewFilePath(rawUrl);
  if (!candidate) return null;
  const contentType = IMAGE_CONTENT_TYPES[path.extname(candidate).toLowerCase()];
  if (!contentType) return null;
  // Channel images may live in the shared crewHome/tmp ingress area. General
  // renderer file APIs intentionally do not opt into this extra root.
  const resolved = resolveOwnedFilePath(candidate, crewHome, ownerSegment, true);
  return resolved ? { ...resolved, contentType } : null;
}

/** Open the already-authorized inode without following a swapped leaf link.
 * The fstat identity closes the resolve -> open race for both leaf and ancestor
 * replacement: a path redirected after authorization cannot be read. */
export async function readResolvedCrewFile(
  resolved: ResolvedOwnedFile,
  includeBody = true,
): Promise<Buffer | null> {
  const noFollow = process.platform !== 'win32' && typeof fs.constants.O_NOFOLLOW === 'number'
    ? fs.constants.O_NOFOLLOW
    : 0;
  const handle = await fs.promises.open(resolved.filePath, fs.constants.O_RDONLY | noFollow);
  try {
    const opened = await handle.stat();
    const identityMatches = (value: fs.Stats): boolean => (
      value.isFile()
      && value.dev === resolved.identity.dev
      && value.ino === resolved.identity.ino
      && value.nlink === resolved.identity.nlink
      && value.size === resolved.identity.size
      && value.mtimeMs === resolved.identity.mtimeMs
      && value.ctimeMs === resolved.identity.ctimeMs
    );
    if (!identityMatches(opened)) {
      throw new Error('crew-file identity changed');
    }
    const body = includeBody ? await handle.readFile() : null;
    // Detect same-inode, same-size in-place rewrites during the read. ctime is
    // not user-restorable on ordinary filesystems, unlike mtime.
    if (!identityMatches(await handle.stat())) {
      throw new Error('crew-file changed while reading');
    }
    return body;
  } finally {
    await handle.close();
  }
}

/** 安装 crew-file:// 协议处理器；须在 app ready 后调用。 */
export function registerCrewFileProtocol(
  getCrewHome: () => string,
  getOwnerSegment: () => string | null,
): void {
  console.log(`[crew-file] protocol registered, crewHome=${getCrewHome()}`);
  protocol.handle(CREW_FILE_SCHEME, async (request) => {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method Not Allowed', { status: 405 });
    }
    const crewHome = getCrewHome();
    const ownerSegment = getOwnerSegment();
    const resolved = ownerSegment
      ? resolveCrewFilePath(request.url, crewHome, ownerSegment)
      : null;
    console.log(`[crew-file] ${request.method} -> ${resolved ? 'ACCEPTED' : 'REJECTED'}`);
    if (!resolved) return new Response('Not Found', { status: 404 });
    try {
      const body = await readResolvedCrewFile(resolved, request.method !== 'HEAD');
      // Account/gateway identity may change while the async file read is in
      // flight. Never finish an old account's response into the new UI.
      if (getOwnerSegment() !== ownerSegment || getCrewHome() !== crewHome) {
        return new Response('Not Found', { status: 404 });
      }
      const responseBody = body ? Uint8Array.from(body) : null;
      return new Response(responseBody, {
        status: 200,
        headers: {
          'Content-Type': resolved.contentType,
          'Cache-Control': 'no-store',
          'X-Content-Type-Options': 'nosniff',
          'Content-Security-Policy': "default-src 'none'",
        },
      });
    } catch {
      return new Response('Not Found', { status: 404 });
    }
  });
}
