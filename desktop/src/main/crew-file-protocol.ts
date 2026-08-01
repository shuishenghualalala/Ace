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

function isWithin(base: string, target: string): boolean {
  const relative = path.relative(base, target);
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function samePath(left: string, right: string): boolean {
  return process.platform === 'win32'
    ? path.resolve(left).toLocaleLowerCase() === path.resolve(right).toLocaleLowerCase()
    : path.resolve(left) === path.resolve(right);
}

export interface ResolvedCrewFile {
  filePath: string;
  contentType: string;
  identity: {
    dev: number;
    ino: number;
    nlink: number;
    size: number;
    mtimeMs: number;
    ctimeMs: number;
  };
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

/** 把 crew-file:///<encodeURIComponent(绝对路径)> 解析成允许服出的本地文件。
 *  不合法 / 越界 / 扩展名不允许 / 非常规文件时返回 null（调用方回 404）。 */
export function resolveCrewFilePath(
  rawUrl: string,
  crewHome: string,
  ownerSegment: string,
): ResolvedCrewFile | null {
  if (rawUrl.length > 16_384 || crewHome.length > 4_096 || ownerSegment.length > 64) return null;
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== `${CREW_FILE_SCHEME}:` || parsed.search) return null;
  const encodedPath = parsed.pathname.replace(/^\/+/, '');
  if (!encodedPath) return null;
  let candidate: string;
  try {
    candidate = decodeURIComponent(encodedPath);
  } catch {
    return null;
  }
  if (!path.isAbsolute(candidate) || !/^acct_[0-9a-f]{16}$/i.test(ownerSegment)) return null;
  const accountsRoot = path.join(path.resolve(crewHome), 'accounts');
  const resolved = path.resolve(candidate);
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
  // All authorization happens in one canonical namespace. This both accepts
  // macOS /var -> /private/var aliases and rejects owner/task roots implemented
  // as links, including links into another account under the same accounts root.
  if (!samePath(realOwnerRoot, path.join(realAccountsRoot, ownerSegment))) return null;
  if (!isWithin(realAccountsRoot, realOwnerRoot)) return null;
  // 渠道入站文件默认落在 <crewHome>/tmp/<channel>-files/，
  // 与账号目录隔离但仍在 crewHome 内，需要一并放行供桌面端渲染/定位。
  const realCrewHome = path.dirname(realAccountsRoot);
  const allowedRoots = [
    resolveCanonicalOwnedRoot(realOwnerRoot, 'task_workspaces'),
    resolveCanonicalOwnedRoot(realOwnerRoot, 'uploads'),
    resolveCanonicalCrewHomeRoot(realCrewHome, 'tmp'),
  ].filter((root): root is string => Boolean(root));
  if (!allowedRoots.some((root) => isWithin(root, real))) return null;
  const contentType = IMAGE_CONTENT_TYPES[path.extname(real).toLowerCase()];
  if (!contentType) return null;
  try {
    const fileStat = fs.statSync(real);
    // A hard link inside account A could otherwise alias an inode whose other
    // name lives in account B; screenshots/uploads are expected to be
    // single-link artifacts, so reject ambiguous inode ownership.
    if (!fileStat.isFile() || fileStat.nlink !== 1) return null;
    return {
      filePath: real,
      contentType,
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

/** Open the already-authorized inode without following a swapped leaf link.
 * The fstat identity closes the resolve -> open race for both leaf and ancestor
 * replacement: a path redirected after authorization cannot be read. */
export async function readResolvedCrewFile(
  resolved: ResolvedCrewFile,
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
