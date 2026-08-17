import type { GatewayInstanceProbe } from './gateway-instance-auth';

export type StandaloneGatewayAction = 'reuse' | 'wait' | 'reject' | 'start-managed';

/** Identity verification decides Gateway ownership; component readiness is separate. */
export function standaloneGatewayUsable(probe: GatewayInstanceProbe): boolean {
  return probe.verified;
}

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
  | { status: 'gone' }
  | { status: 'timeout' };

export interface GatewayCandidateWaitOptions {
  probe: () => Promise<GatewayInstanceProbe>;
  shouldContinue: () => boolean | Promise<boolean>;
  retryDelayMs?: number;
  maxWaitMs?: number;
  now?: () => number;
  sleep?: (delayMs: number) => Promise<void>;
}

const defaultSleep = (delayMs: number): Promise<void> => new Promise((resolve) => {
  setTimeout(resolve, delayMs);
});

/**
 * Wait for one known Gateway candidate without replacing it on a transient timeout.
 * A reachable listener with an invalid proof is rejected immediately. An unreachable
 * candidate gets a short recovery window; after that the caller may start a managed
 * Gateway on a different port instead of waiting forever on a stale listener.
 */
export async function waitForGatewayCandidate(
  options: GatewayCandidateWaitOptions,
): Promise<GatewayCandidateWaitResult> {
  const sleep = options.sleep ?? defaultSleep;
  const retryDelayMs = Math.max(0, options.retryDelayMs ?? 200);
  const now = options.now ?? Date.now;
  const maxWaitMs = Math.max(0, options.maxWaitMs ?? 10_000);
  const deadline = now() + maxWaitMs;
  while (await options.shouldContinue()) {
    if (now() >= deadline) return { status: 'timeout' };
    const probe = await options.probe();
    if (probe.verified) return { status: 'ready', probe };
    if (probe.status === 'untrusted') return { status: 'untrusted', probe };
    const remaining = deadline - now();
    if (remaining <= 0) return { status: 'timeout' };
    await sleep(Math.min(retryDelayMs, remaining));
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
