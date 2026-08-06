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
