import type { GatewayCursor } from '../api/transport';

/** Tracks sessions waiting for canonical history after a replay gap. */
interface SessionStreamRecovery {
  progress: number;
  verifiedFollowupId?: string;
  historyAppliedForFollowup?: string;
  repair?: {
    minimumCut: GatewayCursor | null;
    attempt?: { id: string; connection: () => boolean; epoch?: string };
  };
}

const recoveries = new Map<string, SessionStreamRecovery>();

export function getSessionStreamRecovery(sessionId: string): SessionStreamRecovery | undefined {
  return recoveries.get(sessionId);
}

export function beginSessionStreamRecovery(sessionId: string, minimumRepairCut?: GatewayCursor | null): SessionStreamRecovery {
  const recovery: SessionStreamRecovery = minimumRepairCut === undefined
    ? { progress: 0 } : { progress: 0, repair: { minimumCut: minimumRepairCut } };
  recoveries.set(sessionId, recovery);
  return recovery;
}

export function cancelSessionStreamRecovery(sessionId: string): void {
  recoveries.delete(sessionId);
}
