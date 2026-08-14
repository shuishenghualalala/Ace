import { describe, expect, it } from 'vitest';
import {
  approvalChoiceLabel,
  formatSecurityAudit,
  modeLabel,
} from '../../src/ui/features/security-audit';

describe('security audit display', () => {
  it('keeps backend and stable errors visible', () => {
    expect(formatSecurityAudit({ action_type: 'exec', decision: 'deny', sandbox_backend: 'bwrap', stable_error_code: 'E_POLICY' }))
      .toContain('exec · deny · bwrap · E_POLICY');
  });

  it('formats the conversation, safe summary, approval choice and mode in Chinese', () => {
    const event = {
      timestamp: 1_700_000_000,
      action_type: 'approval_decision',
      decision: 'session',
      session_id: 'session-1234567890',
      action_summary: '执行命令：git status',
      approval_mode: 'auto_review',
    };

    expect(formatSecurityAudit(event)).toContain('session-1234567890');
    expect(formatSecurityAudit(event)).toContain('执行命令：git status');
    expect(approvalChoiceLabel(event)).toBe('当前会话');
    expect(modeLabel(event.approval_mode)).toBe('替我审批');
  });
});
