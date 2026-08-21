import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  FULL_ACCESS_CONFIRMATION,
  SECURITY_APPROVAL_CHOICES,
  SECURITY_MODE_OPTIONS,
  assignSecurityMode,
  approvalBoundaryLabel,
  formatApprovalSummary,
  securityModeForSession,
  selectNextConversationMode,
} from '../../src/ui/features/security-approval';

describe('security approval UI model', () => {
  beforeEach(async () => {
    await selectNextConversationMode('request_approval', () => true);
  });

  it('offers all four explicit approval outcomes including reject', () => {
    expect(SECURITY_APPROVAL_CHOICES).toEqual(['once', 'session', 'always', 'reject']);
  });

  it('offers the four explicit Crew security modes', () => {
    expect(SECURITY_MODE_OPTIONS.map((option) => option.label)).toEqual([
      '只读',
      '请求批准',
      '替我审批',
      '完全访问权限',
    ]);
  });

  it('defaults each new conversation to request approval', async () => {
    await expect(assignSecurityMode('session-a')).resolves.toBe('request_approval');
    expect(securityModeForSession('session-a')).toBe('request_approval');
  });

  it('requires an explicit confirmation before full access', async () => {
    const reject = vi.fn(() => false);
    await expect(selectNextConversationMode('full_access', reject)).resolves.toBe(false);
    await expect(assignSecurityMode('session-b')).resolves.toBe('request_approval');
    expect(reject).toHaveBeenCalledOnce();
  });

  it('does not claim that full access is limited to only the current conversation', () => {
    expect(FULL_ACCESS_CONFIRMATION).not.toContain('只对当前对话');
    expect(FULL_ACCESS_CONFIRMATION).not.toContain('仅在当前对话');
  });

  it('waits for the styled confirmation result before granting full access', async () => {
    const reject = vi.fn(async () => false);
    await expect(selectNextConversationMode('full_access', reject)).resolves.toBe(false);
    expect(reject).toHaveBeenCalledOnce();
  });

  it('does not persist full access as a blank-composer preset', async () => {
    await expect(selectNextConversationMode('full_access', () => true)).resolves.toBe(true);
    await assignSecurityMode('full-access-conversation');
    await assignSecurityMode('after-full-access');
    expect(securityModeForSession('full-access-conversation')).toBe('request_approval');
    expect(securityModeForSession('after-full-access')).toBe('request_approval');
  });

  it('assigns an accepted mode only to subsequently created conversations', async () => {
    await assignSecurityMode('old');
    await expect(selectNextConversationMode('auto_review', () => true)).resolves.toBe(true);
    await assignSecurityMode('new');
    expect(securityModeForSession('old')).toBe('request_approval');
    expect(securityModeForSession('new')).toBe('auto_review');
  });

  it('shows the specific raw command before final wrapped argv', () => {
    expect(formatApprovalSummary({ action: {
      kind: 'exec',
      raw_command: 'git status',
      argv: ['pwsh', '-NoProfile', '-Command', 'utf8-prefix;git status'],
      cwd: 'D:/work',
    } })).toContain(
      '具体命令：git status\n最终执行参数：pwsh -NoProfile -Command utf8-prefix;git status\n工作目录：D:/work',
    );
  });

  it('discloses the effective sandbox profile and unknown command side effects', () => {
    const summary = formatApprovalSummary({
      action: {
        kind: 'exec',
        raw_command: 'python build.py',
        argv: ['python', 'build.py'],
        cwd: 'D:/work',
      },
      effective_permissions: {
        kind: 'managed',
        filesystem: [
          { root: 'D:/work', access: 'read' },
          { root: 'D:/work/dist', access: 'read_write' },
        ],
        network_policy: 'restricted',
        network: [{ protocol: 'https', host: 'api.example.com', port: 443 }],
      },
    });

    expect(summary).toContain('运行边界：受管沙箱');
    expect(summary).toContain('可写范围：D:/work/dist');
    expect(summary).toContain('网络边界：受限/显式目标');
    expect(summary).toContain('网络目标：https://api.example.com:443');
    expect(summary).toContain('未知副作用：命令可能组合已授权文件/网络能力');
  });

  it('shows the exact path and network target from NormalizedAction fields', () => {
    expect(formatApprovalSummary({ action: {
      kind: 'file', path: 'D:/work/report.txt', operation: 'write',
    } })).toContain('文件：D:/work/report.txt\n文件操作：写入');
    expect(formatApprovalSummary({ action: {
      kind: 'network', host: 'github.com', port: 443, protocol: 'https',
    } })).toContain('联网目标：github.com:443（https）');
  });

  it('shows the proposed file change preview', () => {
    expect(formatApprovalSummary({
      preview: '--- before\n+++ after\n-old\n+new',
      action: { kind: 'file', path: 'D:/work/report.txt', operation: 'patch' },
    })).toContain('变更预览：\n--- before\n+++ after\n-old\n+new');
  });

  it('shows every additional permission that approval will grant', () => {
    const summary = formatApprovalSummary({
      preview: '上传构建产物',
      action: { kind: 'exec', raw_command: 'upload build.zip', argv: ['bash', '-lc', 'upload build.zip'] },
      additional_permissions: {
        filesystem: [{ root: '/tmp/release', access: 'read_write' }],
        network: [{ host: 'uploads.example.com', port: 443, protocol: 'https', allow_private: false }],
        allow_local_binding: true,
      },
    });

    expect(summary).toContain('文件系统：/tmp/release（read_write）');
    expect(summary).toContain('网络：https://uploads.example.com:443（仅公网）');
    expect(summary).toContain('允许本地端口监听');
    expect(summary).toContain('申请说明：上传构建产物');
  });

  it('distinguishes sandbox, additional-permission and escalated command boundaries', () => {
    const sandboxed = formatApprovalSummary({
      action: { kind: 'exec', raw_command: 'git status', argv: ['git', 'status'], cwd: '/work' },
    });
    const expanded = formatApprovalSummary({
      action: { kind: 'exec', raw_command: 'cat /opt/report', argv: ['cat', '/opt/report'], cwd: '/work' },
      additional_permissions: {
        sandbox_permissions: 'with_additional_permissions',
        filesystem: [{ root: '/opt/report', access: 'read' }],
      },
    });
    const escalated = formatApprovalSummary({
      action: { kind: 'exec', raw_command: 'system-tool', argv: ['system-tool'], cwd: '/work' },
      additional_permissions: { sandbox_permissions: 'require_escalated' },
    });

    expect(sandboxed).toContain('执行边界：仅在当前沙箱内执行');
    expect(expanded).toContain('执行边界：留在沙箱内，并增加下方明确权限');
    expect(escalated).toContain('执行边界：请求当前完整命令使用宿主用户权限');
    expect(escalated).toContain('包括 Ace 自身数据');
    expect(escalated).toContain('授权只匹配上面显示的完整动作；任一字符变化都会重新判断');
    expect(approvalBoundaryLabel({ action: { kind: 'exec' } })).toBe('仅在当前沙箱内执行');
    expect(approvalBoundaryLabel({
      additional_permissions: {
        sandbox_permissions: 'with_additional_permissions',
        filesystem: [{ root: '/Users/me/Desktop/file.png', access: 'read_write' }],
      },
    })).toBe('沙箱内增加读写权限：/Users/me/Desktop/file.png');
    expect(approvalBoundaryLabel({
      additional_permissions: { sandbox_permissions: 'require_escalated' },
    })).toBe('脱离沙箱执行');
  });

  it('explains managed external-agent network expansion in user-facing language', () => {
    const summary = formatApprovalSummary({
      tool_name: 'external_agent',
      risk_class: 'external_agent_network',
      action: { kind: 'exec', argv: ['/opt/agent', 'external-agent', 'codex'], cwd: '/work' },
      additional_permissions: {
        network: [{ host: 'api.openai.com', port: 443, protocol: 'https' }],
      },
    });

    expect(summary).toContain('风险：启动外部智能体并允许其访问模型服务');
    expect(summary).toContain('网络：https://api.openai.com:443（仅公网）');
  });

  it('does not expose removed channel tool names in approval summaries', () => {
    const summary = formatApprovalSummary({
      tool_name: 'sms_send_file',
      risk_class: 'external_file_read',
      action: {
        kind: 'file',
        path: 'D:/private/report.txt',
        operation: 'read',
      },
    });

    expect(summary).toContain('风险：读取项目外文件');
    expect(summary).not.toContain('sms_send_file');
  });
});
