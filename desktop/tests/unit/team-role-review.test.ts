import { describe, expect, it } from 'vitest';
import { classifyRoleAssessment, type RoleAssessment } from '../../src/ui/features/team-role-review';

const base: RoleAssessment = {
  coverage: { required: ['review'], covered: [], uncovered: [], unverified: ['review'] },
  capability_evidence_confidence: 0,
  members: [{ agent_id: 'a', role_key: 'reviewer', available: true,
    coverage: { required: ['review'], covered: [], missing: [], unknown: ['review'], unavailable: [] } }],
  role_suggestions: [{ agent_id: 'a', role_key: 'reviewer', responsibility: '审查证据' }],
};

describe('role assessment policy', () => {
  it('does not treat missing member data as evidence of unavailability', () => {
    expect(classifyRoleAssessment({ a: 'reviewer' }, { ...base, members: [] }).kind).toBe('incomplete');
  });
  it('requires both risk confirmation and a duty when a risky role lacks a suggestion', () => {
    const result = classifyRoleAssessment({ a: 'reviewer' }, { ...base,
      members: [{ ...base.members[0], coverage: { ...base.members[0].coverage, missing: ['review'] } }],
      role_suggestions: [],
    });
    expect(result.kind).toBe('manual');
    expect(result.risks.a).toBe('pending');
  });
  it('does not allow confirmation to bypass an unavailable runtime', () => {
    const result = classifyRoleAssessment({ a: 'reviewer' }, { ...base,
      members: [{ ...base.members[0], available: false }],
    });
    expect(result.kind).toBe('unavailable');
    expect(result.risks.a).toBe('unavailable');
  });
});
