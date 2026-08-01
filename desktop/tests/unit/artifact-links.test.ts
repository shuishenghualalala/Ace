import { describe, expect, it } from 'vitest';

import { htmlArtifactPathFromHref, httpUrlFromHref } from '../../src/ui/artifact-links';

describe('artifact link classification', () => {
  it('recognizes UOS, Windows, relative, and file URL HTML paths', () => {
    expect(htmlArtifactPathFromHref('/tmp/site/index.html')).toBe('/tmp/site/index.html');
    expect(htmlArtifactPathFromHref('dist/page.htm?preview=1#top')).toBe('dist/page.htm');
    expect(htmlArtifactPathFromHref('C:\\work\\site\\index.html')).toBe('C:\\work\\site\\index.html');
    expect(htmlArtifactPathFromHref('file:///tmp/site/My%20Page.html')).toBe('/tmp/site/My Page.html');
  });

  it('rejects fake suffixes, anchors, and remote URLs as local artifacts', () => {
    expect(htmlArtifactPathFromHref('index.html.txt')).toBeNull();
    expect(htmlArtifactPathFromHref('#section')).toBeNull();
    expect(htmlArtifactPathFromHref('https://example.com/index.html')).toBeNull();
  });

  it('accepts only valid HTTP(S) URLs for browser navigation', () => {
    expect(httpUrlFromHref('https://example.com/path')).toBe('https://example.com/path');
    expect(httpUrlFromHref('javascript:alert(1)')).toBeNull();
    expect(httpUrlFromHref('/tmp/index.html')).toBeNull();
  });
});
