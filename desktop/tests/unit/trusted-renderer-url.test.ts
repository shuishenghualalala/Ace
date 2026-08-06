import * as path from 'path';
import { pathToFileURL } from 'url';
import { describe, expect, it } from 'vitest';
import { isTrustedRendererFileUrl } from '../../src/main/trusted-renderer-url';

describe('isTrustedRendererFileUrl', () => {
  const expectedFile = path.resolve('/tmp/Crew renderer/index.html');
  const exact = pathToFileURL(expectedFile).href;

  it('accepts only the exact local renderer file URL', () => {
    expect(isTrustedRendererFileUrl(exact, expectedFile)).toBe(true);
    expect(isTrustedRendererFileUrl(`${exact}?launchMode=dev`, expectedFile, '?launchMode=dev')).toBe(true);
  });

  it('rejects protocol, hostname, pathname, query, and fragment changes', () => {
    const pathname = pathToFileURL(expectedFile).pathname;
    for (const candidate of [
      `https://example.invalid${pathname}`,
      `file://attacker.invalid${pathname}`,
      pathToFileURL(path.resolve('/tmp/other/index.html')).href,
      `${exact}?injected=1`,
      `${exact}?launchMode=account`,
      `${exact}#injected`,
      'not a URL',
    ]) {
      expect(isTrustedRendererFileUrl(candidate, expectedFile), candidate).toBe(false);
    }
  });
});
