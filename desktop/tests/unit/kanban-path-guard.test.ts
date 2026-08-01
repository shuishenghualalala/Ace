/**
 * normalizeAllowedRoot 归一化测试。
 *
 * 后端在无项目工作空间时会把 workflow.context.workspace_root_path 存成 ""，
 * 直接透传给 shell:openPath 会触发 IPC_ARG_VALIDATION_FAILED
 * （allowedRoot: must be non-empty），必须归一化为 undefined。
 */

import { describe, expect, it } from 'vitest';
import { normalizeAllowedRoot } from '../../src/ui/features/kanban-board';

describe('normalizeAllowedRoot', () => {
  it('空串 / 空白串 / 非字符串统一归一化为 undefined', () => {
    expect(normalizeAllowedRoot('')).toBeUndefined();
    expect(normalizeAllowedRoot('   ')).toBeUndefined();
    expect(normalizeAllowedRoot('\n\t')).toBeUndefined();
    expect(normalizeAllowedRoot(undefined)).toBeUndefined();
    expect(normalizeAllowedRoot(null)).toBeUndefined();
    expect(normalizeAllowedRoot(123)).toBeUndefined();
    expect(normalizeAllowedRoot({})).toBeUndefined();
  });

  it('合法路径保留并去除首尾空白', () => {
    expect(normalizeAllowedRoot('/data/proj')).toBe('/data/proj');
    expect(normalizeAllowedRoot('  /data/proj  ')).toBe('/data/proj');
    expect(normalizeAllowedRoot('C:\\work\\proj')).toBe('C:\\work\\proj');
  });
});
