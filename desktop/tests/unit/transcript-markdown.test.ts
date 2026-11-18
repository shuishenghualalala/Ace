/** @vitest-environment happy-dom */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { patchTranscriptMarkdown } from '../../src/ui/components/transcript';
import { renderMarkdownHtml, renderMarkdownHtmlStreaming } from '../../src/ui/markdown';

// Happy DOM drops the first root tag in DOMPurify. Use the real Markdown
// renderer around that DOM shim; Chromium exercises the sanitizer separately.
vi.mock('dompurify', () => ({ default: { sanitize: (markup: string) => markup } }));

vi.mock('../../src/ui/markdown', async (importOriginal) => {
  const markdown = await importOriginal<typeof import('../../src/ui/markdown')>();
  return {
    ...markdown,
    renderMarkdownHtml: vi.fn(markdown.renderMarkdownHtml),
    renderMarkdownHtmlStreaming: vi.fn(markdown.renderMarkdownHtmlStreaming),
  };
});

function canonicalMarkup(source: string, streaming = false): string {
  const target = document.createElement('div');
  target.innerHTML = streaming ? renderMarkdownHtmlStreaming(source) : renderMarkdownHtml(source);
  return normalizedMarkup(target);
}

function normalizedMarkup(target: HTMLElement): string {
  return target.innerHTML.replace(/\r\n?/g, '\n');
}

afterEach(() => {
  vi.clearAllMocks();
  document.body.replaceChildren();
});

describe('streaming transcript Markdown', () => {
  it('keeps heading, selection, copy button and hydrated Mermaid nodes across live and final updates', () => {
    const target = document.createElement('div');
    document.body.append(target);
    const source = [
      '# Heading',
      '',
      'A **stable** paragraph.',
      '',
      '~~~js',
      'const values = [1, 2];',
      '~~~',
      '',
      '~~~mermaid',
      'graph TD; A-->B;',
      '~~~',
      '',
      'Tail',
    ].join('\n');
    patchTranscriptMarkdown(target, source, true);
    const heading = target.querySelector('h1')!;
    const stable = target.querySelector('p')!;
    const copy = target.querySelector('button[data-copy]')!;
    const chart = target.querySelector('[data-mermaid]')!;
    const range = document.createRange();
    range.selectNodeContents(stable);
    document.getSelection()?.addRange(range);
    copy.textContent = 'Copied';
    chart.replaceChildren(document.createElement('svg'));

    const changed = patchTranscriptMarkdown(target, source + ' grows', true);
    expect(changed.map((element) => element.textContent)).toEqual(['Tail grows']);
    expect(target.querySelector('h1')).toBe(heading);
    expect(target.querySelector('p')).toBe(stable);
    expect(document.getSelection()?.getRangeAt(0).startContainer).toBe(stable);
    expect(target.querySelector('button[data-copy]')).toBe(copy);
    expect(target.querySelector('[data-mermaid]')).toBe(chart);

    expect(patchTranscriptMarkdown(target, source + ' grows', false)).toEqual([]);
    expect(target.querySelector('button[data-copy]')?.textContent).toBe('Copied');
    expect(target.querySelector('[data-mermaid] svg')).not.toBeNull();
  });

  it('waits for global link definitions while keeping preceding independent blocks stable', () => {
    const target = document.createElement('div');
    const start = '# Heading\n\nRead [the guide][guide].\n\n';
    patchTranscriptMarkdown(target, start, true);
    const heading = target.querySelector('h1');
    expect(target.querySelector('a')).toBeNull();
    const source = start + '[guide]: https://example.org/guide\n\nUse [the guide][guide].';
    patchTranscriptMarkdown(target, source, true);
    expect(target.querySelector('h1')).toBe(heading);
    expect(target.querySelectorAll('a[href="https://example.org/guide"]')).toHaveLength(2);
    expect(normalizedMarkup(target)).toBe(canonicalMarkup(source, true));
    patchTranscriptMarkdown(target, source);
    expect(normalizedMarkup(target)).toBe(canonicalMarkup(source));
  });

  it('keeps lists, GFM tables, math, Wiki links and fences correct across split stream chunks', () => {
    const chunks = [
      '# Heading\r', '\n', '\r\n',
      '- [x] Task\n\n', '  Continued inside the task.\n\n',
      '- Next item\n\n', 'Following paragraph\n\n',
      '| a | b |\n| - | - |\n', '| 1 | 2 |\n\n',
      '$$\nx^2 + y^2', ' = z^2\n$$\n\n',
      '~~~python\nmessage = \"hello', ' [world]\"\n~~~\n\n',
      'A [[multi', ' word]] label and [inline](https://example.org).\n\n',
      'Literal <img src=x onerror=alert(1)> and [bad](javascript:alert%281%29)',
    ];
    const target = document.createElement('div');
    let source = '';
    for (const chunk of chunks) {
      source += chunk;
      patchTranscriptMarkdown(target, source, true);
      expect(normalizedMarkup(target)).toBe(canonicalMarkup(source, true));
    }
    patchTranscriptMarkdown(target, source, false);
    expect(normalizedMarkup(target)).toBe(canonicalMarkup(source));
    expect(target.querySelector('img, script, [onerror], [href^="javascript:"]')).toBeNull();
  });

  it('keeps an embedded BOM when it is the only character in the new tail', () => {
    const target = document.createElement('div');
    const start = '# Heading\n\n';
    patchTranscriptMarkdown(target, start, true);
    const heading = target.querySelector('h1');
    for (const source of [start + '\uFEFF', start + '\uFEFF suffix']) {
      patchTranscriptMarkdown(target, source, true);
      expect(target.querySelector('h1')).toBe(heading);
      expect(normalizedMarkup(target)).toBe(canonicalMarkup(source, true));
    }
  });

  it('uses a whole render after a rewrite and removes predictive formatting at finalization', () => {
    const target = document.createElement('div');
    patchTranscriptMarkdown(target, '# Original\n\nInitial **bold', true);
    expect(target.querySelector('strong')?.textContent).toBe('bold');
    const replacement = '# Revised\n\nLiteral **opening';
    patchTranscriptMarkdown(target, replacement, true);
    expect(target.querySelector('h1')?.textContent).toBe('Revised');
    expect(normalizedMarkup(target)).toBe(canonicalMarkup(replacement, true));
    patchTranscriptMarkdown(target, replacement, false);
    expect(normalizedMarkup(target)).toBe(canonicalMarkup(replacement));
    expect(target.querySelector('strong')).toBeNull();
  });

  it('parses only a small fraction of the cumulative text of a long multi-paragraph live response', () => {
    const target = document.createElement('div');
    let source = '';
    let wholeResponseCharacters = 0;
    for (let index = 1; index <= 100; index += 1) {
      const paragraph = 'Paragraph ' + index + ' ' + 'safe words '.repeat(20);
      for (const chunk of [paragraph.slice(0, 100), paragraph.slice(100), '\n\n']) {
        source += chunk;
        wholeResponseCharacters += source.length;
        patchTranscriptMarkdown(target, source, true);
      }
    }
    const actualParsedCharacters = [
      ...vi.mocked(renderMarkdownHtml).mock.calls,
      ...vi.mocked(renderMarkdownHtmlStreaming).mock.calls,
    ].reduce((sum, [markdown]) => sum + markdown.length, 0);
    expect(actualParsedCharacters).toBeLessThan(wholeResponseCharacters / 20);
    expect(target.querySelectorAll('p')).toHaveLength(100);
  });
});
