import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import { appendPrivateSync } from '../../src/main/private-append';

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), 'ace-private-append-'));
  temporaryDirectories.push(directory);
  return directory;
}

describe('appendPrivateSync', () => {
  it('creates an owner-only regular file and appends atomically', () => {
    const directory = temporaryDirectory();
    const file = join(directory, 'crash.log');

    appendPrivateSync(file, 'first\n');
    appendPrivateSync(file, 'second\n');

    expect(readFileSync(file, 'utf8')).toBe('first\nsecond\n');
    expect(statSync(file).isFile()).toBe(true);
    if (process.platform !== 'win32') {
      expect(statSync(file).mode & 0o777).toBe(0o600);
    }
  });

  it('refuses to follow a symlink at the final path component', () => {
    const directory = temporaryDirectory();
    const outside = join(directory, 'outside.log');
    const link = join(directory, 'crash.log');
    try {
      symlinkSync(outside, link);
    } catch {
      return;
    }
    expect(() => appendPrivateSync(link, 'data')).toThrow();
    expect(existsSync(outside)).toBe(false);
  });
});
