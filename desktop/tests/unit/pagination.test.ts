/**
 * 分页纯函数单测。
 */
import { describe, it, expect } from 'vitest';
import { paginate, totalPages, pageNumbers } from '../../src/ui/pagination';

describe('paginate', () => {
  it('returns first page slice', () => {
    expect(paginate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 1, 5)).toEqual([1, 2, 3, 4, 5]);
  });

  it('returns second page slice', () => {
    expect(paginate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 2, 5)).toEqual([6, 7, 8, 9, 10]);
  });

  it('clamps page beyond total to last page', () => {
    // 10 items / pageSize 5 → 2 pages；page 3 应回退到 page 2
    expect(paginate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3, 5)).toEqual([6, 7, 8, 9, 10]);
  });

  it('clamps page < 1 to first page', () => {
    expect(paginate([1, 2, 3], 0, 2)).toEqual([1, 2]);
  });

  it('returns empty for empty input', () => {
    expect(paginate([], 1, 5)).toEqual([]);
  });

  it('treats pageSize <= 0 as 1', () => {
    expect(paginate([1, 2, 3], 2, 0)).toEqual([2]);
  });
});

describe('totalPages', () => {
  it('0 items → 1 (never 0)', () => {
    expect(totalPages(0, 10)).toBe(1);
  });

  it('exact division', () => {
    expect(totalPages(10, 5)).toBe(2);
  });

  it('rounds up on remainder', () => {
    expect(totalPages(11, 5)).toBe(3);
  });

  it('treats pageSize <= 0 as 1', () => {
    expect(totalPages(5, 0)).toBe(5);
  });
});

describe('pageNumbers', () => {
  it('lists all when total <= 7', () => {
    expect(pageNumbers(1, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(pageNumbers(3, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it('adds leading ellipsis when current far right', () => {
    // current=8, total=10 → [1,'…',6,7,8,9,10]
    expect(pageNumbers(8, 10)).toEqual([1, '…', 6, 7, 8, 9, 10]);
  });

  it('adds trailing ellipsis when current far left', () => {
    expect(pageNumbers(1, 10)).toEqual([1, 2, 3, '…', 10]);
  });

  it('adds both ellipses when current in middle', () => {
    expect(pageNumbers(5, 10)).toEqual([1, '…', 3, 4, 5, 6, 7, '…', 10]);
  });
});
