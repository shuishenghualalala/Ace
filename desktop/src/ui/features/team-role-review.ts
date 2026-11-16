import type { backendApi } from '../backend-client';

export type RoleAssessment = Awaited<ReturnType<typeof backendApi.assessExternalTeam>>;
export type RoleRiskDecision = 'pending' | 'accepted' | 'unavailable';

/** Classify one immutable review result without changing the roster. */
export function classifyRoleAssessment(
  changes: Record<string, string>, assessment: RoleAssessment,
): { kind: 'ready' | 'confirmation' | 'unavailable' | 'manual' | 'incomplete'; risks: Record<string, RoleRiskDecision> } {
  const ids = Object.keys(changes);
  const risks: Record<string, RoleRiskDecision> = {};
  for (const id of ids) {
    const member = assessment.members.find((item) => item.agent_id === id);
    if (!member || member.role_key !== changes[id] || typeof member.available !== 'boolean') return { kind: 'incomplete', risks: {} };
    if (member.available === false || member.coverage.unavailable.length) risks[id] = 'unavailable';
    else if (member.coverage.missing.length) risks[id] = 'pending';
  }
  if (Object.values(risks).includes('unavailable')) return { kind: 'unavailable', risks };
  const missingDuty = ids.some((id) => !assessment.role_suggestions?.some(
    (suggestion) => suggestion.agent_id === id && suggestion.role_key === changes[id] && suggestion.responsibility.trim(),
  ));
  if (ids.length && (assessment.role_suggestion_error || missingDuty)) return { kind: 'manual', risks };
  return { kind: Object.keys(risks).length ? 'confirmation' : 'ready', risks };
}
