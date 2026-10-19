import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  canonicalOwnedShellRoot,
  isPathWithin,
  resolveAllowedShellPath,
} from '../../src/main/shell-allowed-path';

const owner = 'acct_0123456789abcdef';
let root = '';
let crewHome = '';
let downloads = '';
let taskRoot = '';

beforeAll(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'ace-shell-path-'));
  crewHome = path.join(root, 'Crew');
  downloads = path.join(root, 'Downloads');
  taskRoot = path.join(crewHome, 'accounts', owner, 'task_workspaces');
  fs.mkdirSync(taskRoot, { recursive: true });
  fs.mkdirSync(downloads);
});

afterAll(() => fs.rmSync(root, { recursive: true, force: true }));

function authorize(file: string): string {
  return resolveAllowedShellPath(file, [downloads, taskRoot], {
    root: crewHome,
    allowedRoots: [taskRoot],
  });
}

describe('shell file authorization', () => {
  it('allows the current account task file', () => {
    const file = path.join(taskRoot, 'report.txt');
    fs.writeFileSync(file, 'report');
    expect(authorize(file)).toBe(fs.realpathSync(file));
  });

  it('rejects a linked account task root', () => {
    const linkedOwner = 'acct_aaaaaaaaaaaaaaaa';
    const link = path.join(crewHome, 'accounts', linkedOwner, 'task_workspaces');
    fs.mkdirSync(path.dirname(link), { recursive: true });
    try { fs.symlinkSync(taskRoot, link, 'dir'); } catch { return; }
    expect(canonicalOwnedShellRoot(crewHome, linkedOwner, 'task_workspaces')).toBeNull();
  });

  it('rejects another account credentials even when a broad parent root is supplied', () => {
    const secret = path.join(crewHome, 'accounts', 'acct_fedcba9876543210', '.env');
    fs.mkdirSync(path.dirname(secret), { recursive: true });
    fs.writeFileSync(secret, 'KEY=secret');
    expect(() => resolveAllowedShellPath(secret, [crewHome], {
      root: crewHome,
      allowedRoots: [taskRoot],
    })).toThrow('outside the current account');
  });

  it('rejects a symlink from Downloads to the Gateway instance key', () => {
    const secret = path.join(crewHome, '.gateway-instance', 'gateway-instance.key');
    fs.mkdirSync(path.dirname(secret), { recursive: true });
    fs.writeFileSync(secret, 'secret');
    const link = path.join(downloads, 'report.txt');
    try { fs.symlinkSync(secret, link); } catch { return; }
    expect(() => authorize(link)).toThrow('sensitive security/audit resource');
  });

  it('rejects a symlink from Downloads into another account', () => {
    const secret = path.join(crewHome, 'accounts', 'acct_fedcba9876543210', '.env');
    const link = path.join(downloads, 'owner-report.txt');
    try { fs.symlinkSync(secret, link); } catch { return; }
    expect(() => authorize(link)).toThrow('outside the current account');
  });

  it('compares Windows paths without case-sensitive string prefixes', () => {
    const win = path.win32;
    expect(isPathWithin('C:\\Users\\Alice\\Crew', 'c:\\users\\alice\\crew\\task.txt', win)).toBe(true);
    expect(isPathWithin('C:\\Users\\Alice\\Crew', 'c:\\users\\alice\\crew-other\\task.txt', win)).toBe(false);
  });
});
