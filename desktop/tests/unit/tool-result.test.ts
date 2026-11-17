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

  it('uses terminal status rather than the process ID to display a foreground result', () => {
    const result = (body: object) => formatToolResultDisplay('terminal', JSON.stringify({ session_id: 'proc_1', ...body }));
    expect(result({ status: 'completed', output: '', exit_code: 0 })).toBe('terminal completed');
    expect(result({ status: 'failed', output: 'partial 雪', exit_code: 2 })).toBe('terminal exited with code 2\n\npartial 雪');
    expect(result({ status: 'timed_out', output: '' })).toBe('terminal timed out');
    expect(result({ status: 'cancelled', output: '', error: '命令已取消' })).toBe('命令已取消');
    expect(result({ status: 'backgrounded', exit_code: null })).toBe('Background process started: proc_1');
    expect(result({})).toBe('Background process started: proc_1');
  });

  it('returns plain text for non-json results', () => {
    expect(formatToolResultDisplay('file_read', 'hello world')).toBe('hello world');
  });
});
