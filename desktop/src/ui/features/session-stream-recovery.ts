/** Tracks sessions waiting for canonical history after a replay gap. */
interface SessionStreamRecovery {
  progress: number;
  verifiedFollowupId?: string;
  historyAppliedForFollowup?: string;
}

const recoveries = new Map<string, SessionStreamRecovery>();

export function getSessionStreamRecovery(sessionId: string): SessionStreamRecovery | undefined {
  return recoveries.get(sessionId);
}

export function beginSessionStreamRecovery(sessionId: string): SessionStreamRecovery {
  const recovery: SessionStreamRecovery = { progress: 0 };
  recoveries.set(sessionId, recovery);
  return recovery;
}

export function cancelSessionStreamRecovery(sessionId: string): void {
  recoveries.delete(sessionId);
}
