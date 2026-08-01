import { describe, expect, it } from 'vitest';
import { isPlanDocumentPath } from '../../src/ui/plan-document-path';

describe('isPlanDocumentPath', () => {
  it('matches crew plans layout', () => {
    expect(isPlanDocumentPath('C:\\Users\\x\\.crew\\plans\\owner\\sid\\plan_20260709_120000_1_ab.md')).toBe(true);
    expect(isPlanDocumentPath('/home/u/.crew/plans/owner/sid/plan_20260709_120000_1_ab.md')).toBe(true);
    expect(isPlanDocumentPath('plans/owner/sid/plan_20260709_120000_1_ab.md')).toBe(true);
  });

  it('rejects ordinary source files', () => {
    expect(isPlanDocumentPath('Crew/desktop/src/ui/chat-render.ts')).toBe(false);
    expect(isPlanDocumentPath('docs/plan.md')).toBe(false);
    expect(isPlanDocumentPath('C:\\Users\\x\\Desktop\\ppt\\tetris.html')).toBe(false);
  });
});
