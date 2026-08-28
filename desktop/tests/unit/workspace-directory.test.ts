import * as path from 'path';
import { describe, expect, it, vi } from 'vitest';
import { resolveWorkspaceFilePath } from '../../src/main/workspace-directory';

function regularFileStat() {
  return {
    isDirectory: () => false,
    isFile: () => true,
    dev: 1,
    ino: 2,
    nlink: 1,
    size: 42,
    mtimeMs: 3,
    ctimeMs: 4,
  };
}

describe('resolveWorkspaceFilePath', () => {
  it('authorizes a canonical file inside a POSIX workspace', async () => {
    const fileSystem = {
      realpath: vi.fn(async () => '/work/project/report.html'),
      stat: vi.fn(async () => regularFileStat()),
    };
    const resolved = await resolveWorkspaceFilePath(
      '/work/project/report.html',
      '/work/project',
      fileSystem as never,
    );

    expect(resolved?.filePath).toBe('/work/project/report.html');
    expect(resolved?.identity.size).toBe(42);
  });

  it('rejects a real path that escapes the workspace', async () => {
    const fileSystem = {
      realpath: vi.fn(async () => '/work/private/report.html'),
      stat: vi.fn(async () => regularFileStat()),
    };
    await expect(resolveWorkspaceFilePath(
      '/work/project/link.html',
      '/work/project',
      fileSystem as never,
    )).resolves.toBeNull();
  });

  it('handles Windows drive paths with the platform path rules', async () => {
    const fileSystem = {
      realpath: vi.fn(async () => 'C:\\work\\project\\report.html'),
      stat: vi.fn(async () => regularFileStat()),
    };
    const resolved = await resolveWorkspaceFilePath(
      'C:\\work\\project\\report.html',
      'C:\\work\\project',
      fileSystem as never,
      path.win32,
    );

    expect(resolved?.filePath).toBe('C:\\work\\project\\report.html');
  });
});
