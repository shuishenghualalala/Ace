import { describe, expect, it } from 'vitest';
import { formatToolResultDisplay } from '../../src/ui/tool-result';

describe('formatToolResultDisplay', () => {
  it('extracts terminal stdout from JSON wrapper', () => {
    const raw = JSON.stringify({
      success: true,
      cwd: 'C:\\work',
      command: 'python search.py',
      output: '世界杯\n04:00 乌拉圭 vs 葡萄牙',
    });
    expect(formatToolResultDisplay('terminal', raw)).toBe('世界杯\n04:00 乌拉圭 vs 葡萄牙');
  });

  it('returns plain text for non-json results', () => {
    expect(formatToolResultDisplay('file_read', 'hello world')).toBe('hello world');
  });
});
