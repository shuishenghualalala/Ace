/**
 * Shell-path allow/deny policy for the renderer-facing file-read IPC.
 *
 * ``resolveShellAllowedPath`` (index.ts) first allow-lists a set of roots, but some
 * sensitive resources sit *under* those roots and must never be exposed to the
 * renderer. This module holds the pure, testable deny predicate.
 *
 * Why deny these even under an allowed root (security audit M-1):
 *  - ``.gateway-instance`` holds the 64-byte instance HMAC key. A renderer that reads
 *    it can forge Desktop security proofs.
 *  - ``*.db`` / ``-wal`` / ``-shm`` are raw SQLite (audit / session / rules). Reading
 *    them as text is never legitimate, and the audit DB spans owners.
 */

import fs from 'node:fs';
import path from 'node:path';

const DENIED_SEGMENTS = new Set(['.gateway-instance']);
// Covers SQLite audit/session/rules DBs and their WAL/SHM/journal sidecars.
const SENSITIVE_DB_RE = /\.(db|db-wal|db-shm|db-journal)$/i;

/** True if a resolved absolute path is a sensitive security/audit resource. */
export function isDeniedShellPath(resolved: string): boolean {
  if (!resolved) return false;
  // Compare lowercased: the protected resources live under NTFS (case-insensitive),
  // so a renderer-supplied `.Gateway-Instance` must not evade the deny.
  const lower = resolved.toLowerCase();
  if (lower.split(/[\\/]+/).some((seg) => DENIED_SEGMENTS.has(seg))) return true;
  return SENSITIVE_DB_RE.test(lower);
}

/** Compare components, including case-insensitive Windows paths. */
export function isPathWithin(root: string, target: string, paths: typeof path = path): boolean {
  const relative = paths.relative(root, target);
  return relative === '' || (
    relative !== '..'
    && !relative.startsWith(`..${paths.sep}`)
    && !paths.isAbsolute(relative)
  );
}

/** Do not trust a linked owner directory as an authorization root. */
export function canonicalOwnedShellRoot(
  crewHome: string,
  ownerSegment: string,
  segment: 'task_workspaces' | 'uploads',
): string | null {
  try {
    const homeReal = fs.realpathSync(crewHome);
    const candidate = path.join(crewHome, 'accounts', ownerSegment, segment);
    const real = fs.realpathSync(candidate);
    const expected = path.join(homeReal, 'accounts', ownerSegment, segment);
    return isPathWithin(real, expected) && isPathWithin(expected, real) ? real : null;
  } catch {
    return null;
  }
}

/** Authorize the real target, not the renderer's possibly linked path. */
export function resolveAllowedShellPath(
  rawPath: string,
  allowedRoots: string[],
  protectedHome?: { root: string; allowedRoots: string[] },
): string {
  if (!path.isAbsolute(rawPath)) throw new Error('path must be absolute');
  const requested = path.resolve(rawPath);
  if (isDeniedShellPath(requested)) throw new Error('path is a sensitive security/audit resource');
  const real = fs.realpathSync(requested);
  if (isDeniedShellPath(real)) throw new Error('path is a sensitive security/audit resource');
  if (protectedHome) {
    let homeReal: string;
    try {
      homeReal = fs.realpathSync(protectedHome.root);
    } catch {
      homeReal = path.resolve(protectedHome.root);
    }
    if (isPathWithin(homeReal, real) && !protectedHome.allowedRoots.some((root) => {
      try {
        return isPathWithin(fs.realpathSync(root), real);
      } catch {
        return false;
      }
    })) {
      throw new Error('path is outside the current account');
    }
  }
  const allowed = allowedRoots.some((root) => {
    try {
      return isPathWithin(fs.realpathSync(root), real);
    } catch {
      return false;
    }
  });
  if (!allowed) throw new Error('path not under any allowed root');
  return real;
}
