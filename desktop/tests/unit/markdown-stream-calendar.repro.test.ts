/**
 * Repro tests for calendar C++ streaming garble (历 / % / fence).
 */
import { describe, it, expect } from 'vitest';
import { preprocessStreamMarkdown } from '../../src/ui/markdown-stream';
import { renderMarkdownHtml } from '../../src/ui/markdown';

const TITLE = '## 日历 C++ 伪代码实现';
const OPEN_FENCE = '\n\n```cpp\n';
const PARTIAL_CODE = `#include <iostream>
bool isLeapYear(int year) {
    return (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
}

int getDaysInMonth(int year, int month) {
    int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};`;

function assertPreservesCalendarStream(source: string): void {
  const pre = preprocessStreamMarkdown(source, true);
  expect(pre, `preprocess dropped 历 for:\n${source}`).toContain('历');
  if (source.includes('%')) {
    expect(pre, `preprocess dropped % for:\n${source}`).toMatch(/%/);
  }

  const out = renderMarkdownHtml(source, { isStreaming: true });
  expect(out, `render dropped 历 for:\n${source}`).toContain('历');
  if (source.includes('```')) {
    expect(out, `render should use code block for:\n${source}`).toContain('code-block-wrapper');
  }
  if (source.includes('%')) {
    expect(out, `render dropped % for:\n${source}`).toMatch(/%/);
  }
}

describe('calendar C++ streaming repro', () => {
  it('title only', () => {
    assertPreservesCalendarStream(TITLE);
  });

  it('title + opening fence line', () => {
    assertPreservesCalendarStream(TITLE + OPEN_FENCE);
  });

  it('title + partial code before closing fence', () => {
    assertPreservesCalendarStream(TITLE + OPEN_FENCE + PARTIAL_CODE);
  });

  it('title and fence on same line (bad model output)', () => {
    const bad = '## 日历 C++ 伪代码实现 ```cpp\n#include <iostream>';
    const pre = preprocessStreamMarkdown(bad, true);
    expect(pre).toContain('历');
    const out = renderMarkdownHtml(bad, { isStreaming: true });
    expect(out).toContain('历');
    // Ideally still code block — document current behavior
    // expect(out).toContain('code-block-wrapper');
  });

  it('code without fence yet (worst case)', () => {
    const noFence = `${TITLE}\n\n${PARTIAL_CODE}`;
    const pre = preprocessStreamMarkdown(noFence, true);
    expect(pre).toContain('历');
    // preprocess must not strip % when code is still outside fence
    expect(pre).toMatch(/year % 4/);
  });
});
