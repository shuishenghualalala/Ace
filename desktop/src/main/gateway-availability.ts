import type { GatewayInstanceProbe } from './gateway-instance-auth';

export type StandaloneGatewayAction = 'reuse' | 'wait' | 'reject' | 'start-managed';

/** Decide ownership before Desktop is allowed to spawn a managed fallback. */
export function chooseStandaloneGatewayAction(
  probe: GatewayInstanceProbe,
  candidatePresent: boolean,
): StandaloneGatewayAction {
  if (probe.verified) return 'reuse';
  if (!candidatePresent) return 'start-managed';
  return probe.status === 'untrusted' ? 'reject' : 'wait';
}

export type GatewayCandidateWaitResult =
  | { status: 'ready'; probe: GatewayInstanceProbe }
  | { status: 'untrusted'; probe: GatewayInstanceProbe }
  | { status: 'gone' };

export interface GatewayCandidateWaitOptions {
  probe: () => Promise<GatewayInstanceProbe>;
  shouldContinue: () => boolean | Promise<boolean>;
  retryDelayMs?: number;
  sleep?: (delayMs: number) => Promise<void>;
}

const defaultSleep = (delayMs: number): Promise<void> => new Promise((resolve) => {
  setTimeout(resolve, delayMs);
});

/**
 * Wait for one known Gateway candidate without replacing it on a transient timeout.
 * A reachable listener with an invalid proof is rejected immediately; an unreachable
 * but still-present candidate keeps ownership of its port and is allowed to recover.
 */
export async function waitForGatewayCandidate(
  options: GatewayCandidateWaitOptions,
): Promise<GatewayCandidateWaitResult> {
  const sleep = options.sleep ?? defaultSleep;
  const retryDelayMs = Math.max(0, options.retryDelayMs ?? 200);
  while (await options.shouldContinue()) {
    const probe = await options.probe();
    if (probe.verified) return { status: 'ready', probe };
    if (probe.status === 'untrusted') return { status: 'untrusted', probe };
    await sleep(retryDelayMs);
  }
  return { status: 'gone' };
}

/** Keep a previously verified connection through timeouts while its listener exists. */
export function nextGatewayConnectionState(
  currentConnected: boolean,
  probe: GatewayInstanceProbe,
  candidatePresent: boolean,
): boolean {
  if (probe.verified) return true;
  if (probe.status === 'untrusted' || !candidatePresent) return false;
  return currentConnected;
}
