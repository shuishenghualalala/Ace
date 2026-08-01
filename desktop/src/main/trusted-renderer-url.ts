import { pathToFileURL } from 'url';

/** Exact file URL check shared by renderer navigation and privileged IPC. */
export function isTrustedRendererFileUrl(candidateUrl: string, expectedFilePath: string): boolean {
  try {
    const candidate = new URL(candidateUrl);
    const expected = pathToFileURL(expectedFilePath);
    return candidate.protocol === 'file:'
      && candidate.protocol === expected.protocol
      && candidate.hostname === ''
      && candidate.hostname === expected.hostname
      && candidate.pathname === expected.pathname
      && candidate.username === ''
      && candidate.password === ''
      && candidate.port === ''
      && candidate.search === ''
      && candidate.hash === '';
  } catch {
    return false;
  }
}
