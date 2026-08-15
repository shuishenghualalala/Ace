/**
 * normalizeWorkspaceId 归一化测试。
 *
 * shell:openPath 只接受 Workspace ID，由主进程从已鉴权 Gateway 记录解析根目录；
 * 空状态必须归一化为 undefined。
 */

import { describe, expect, it } from 'vitest';
import { normalizeWorkspaceId } from '../../src/ui/features/kanban-board';

describe('normalizeWorkspaceId', () => {
  it('空串 / 空白串 / 非字符串统一归一化为 undefined', () => {
    expect(normalizeWorkspaceId('')).toBeUndefined();
    expect(normalizeWorkspaceId('   ')).toBeUndefined();
    expect(normalizeWorkspaceId('\n\t')).toBeUndefined();
    expect(normalizeWorkspaceId(undefined)).toBeUndefined();
    expect(normalizeWorkspaceId(null)).toBeUndefined();
    expect(normalizeWorkspaceId(123)).toBeUndefined();
    expect(normalizeWorkspaceId({})).toBeUndefined();
  });

  it('合法 Workspace ID 保留并去除首尾空白', () => {
    expect(normalizeWorkspaceId('workspace-123')).toBe('workspace-123');
    expect(normalizeWorkspaceId('  workspace-123  ')).toBe('workspace-123');
  });
});
