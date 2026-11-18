import { SecurityDecisionReceiptArgs, type SecurityDecisionArgs } from '../../shared/ipc-schemas';
import { interactiveOwnerKey } from './interactive-owner';

const KEY_PREFIX = 'crew.security.decision.v1.';
const RETENTION_MS = 24 * 60 * 60 * 1000;

export interface PendingDecision {
  owner: string;
  args: SecurityDecisionReceiptArgs;
  expires: number;
}

function key(owner: string, submissionId: string): string {
  return KEY_PREFIX + owner + '.' + submissionId;
}

/** Keep only the data needed for a read-only check across renderer and Gateway restarts. */
export async function rememberDecision(
  command: SecurityDecisionArgs, owner: string,
): Promise<PendingDecision> {
  const { alwaysArgvPrefix, ...scope } = command;
  const args: SecurityDecisionReceiptArgs = { ...scope, submissionId: crypto.randomUUID().replaceAll('-', '') };
  if (alwaysArgvPrefix?.length) {
    const prefix = new TextEncoder().encode(JSON.stringify(alwaysArgvPrefix));
    const digest = await crypto.subtle.digest('SHA-256', prefix);
    args.prefixHash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  }
  if (!owner || owner !== interactiveOwnerKey()) throw new Error('审批账号已切换');
  const entry = { owner, args, expires: Date.now() + RETENTION_MS };
  localStorage.setItem(key(owner, args.submissionId), JSON.stringify(entry));
  return entry;
}

export function pendingDecisions(owner: string): PendingDecision[] {
  if (!owner) return [];
  const entries: PendingDecision[] = [];
  const prefix = KEY_PREFIX + owner + '.';
  for (const name of Object.keys(localStorage)) {
    if (!name.startsWith(prefix)) continue;
    try {
      const entry = JSON.parse(localStorage.getItem(name) ?? 'null');
      const args = SecurityDecisionReceiptArgs.parse(entry?.args);
      if (args.ok && entry.owner === owner && Number.isFinite(entry.expires)
          && entry.expires > Date.now() && name === key(owner, args.value.submissionId)) {
        entries.push({ owner, args: args.value, expires: entry.expires });
        continue;
      }
    } catch { /* A partial local record cannot identify an acknowledgement. */ }
    localStorage.removeItem(name);
  }
  return entries;
}

export function receiptStatus(value: unknown, entry: PendingDecision): string | undefined {
  if (!value || typeof value !== 'object') return;
  const receipt = value as Record<string, unknown>;
  const { args } = entry;
  if (receipt.request_id !== args.requestId || receipt.submission_id !== args.submissionId
      || receipt.decision !== args.decision) return;
  if (receipt.status === 'recorded' || receipt.status === 'pending' || receipt.status === 'unknown') return receipt.status;
  return undefined;
}

/** Consume the local notice once. A settled receipt cannot authorize another command. */
export function finishDecision(entry: PendingDecision): boolean {
  const name = key(entry.owner, entry.args.submissionId);
  if (entry.owner !== interactiveOwnerKey() || !localStorage.getItem(name)) return false;
  localStorage.removeItem(name);
  return true;
}
