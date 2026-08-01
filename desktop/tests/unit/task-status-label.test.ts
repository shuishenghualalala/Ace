import { describe, expect, it } from 'vitest';
import { taskStatusLabel } from '../../src/shared/task-status';

describe('taskStatusLabel', () => {
  it.each([
    ['completed', '已完成'],
    ['failed', '失败'],
    ['cancelled', '已取消'],
    ['timed_out', '已超时'],
  ])('renders %s as %s', (status, label) => {
    expect(taskStatusLabel(status)).toBe(label);
  });
});
