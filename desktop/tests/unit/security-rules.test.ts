import { describe, expect, it } from 'vitest';
import {
  formatSecurityRule,
  formatSecurityRulePermissions,
} from '../../src/ui/features/security-rules';

describe('security rule display', () => {
  it('shows the token prefix and working directory', () => {
    expect(formatSecurityRule({ decision: 'allow', argv_prefix: ['python', '-m'], cwd: 'D:/work' }))
      .toBe('允许 python -m · 目录 D:/work');
  });

  it('prefers the redacted approval summary when an exact action is stored', () => {
    expect(formatSecurityRule({
      decision: 'allow',
      kind: 'exec',
      exact_digest: 'digest',
      action_summary: '执行命令：git status',
      action_detail: '具体命令：git status\n工作目录：D:/work',
    })).toBe('允许 执行命令：git status');
  });

  it('keeps persistent permission overlays visible', () => {
    expect(formatSecurityRulePermissions({
      additional_permissions: {
        filesystem: [{ root: '/tmp/release', access: 'read_write' }],
        network: [{ host: 'uploads.example.com', port: 443, protocol: 'https' }],
      },
    })).toBe(
      '额外文件权限（读写）：/tmp/release\n额外网络权限：uploads.example.com:443（https）',
    );
  });
});
