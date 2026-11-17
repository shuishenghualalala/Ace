import type { ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { launchUpdateInstaller } from '../../src/main/update/installer-handoff';

class InstallerProcess extends EventEmitter {
  readonly unref = vi.fn();
  killed = false;
  readonly kill = vi.fn(() => { this.killed = true; });

  constructor(public pid: number | undefined) {
    super();
  }

  get process(): ChildProcess {
    return this as unknown as ChildProcess;
  }
}

describe('launchUpdateInstaller', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('waits for the native Windows installer to spawn before detaching the handle', async () => {
    const child = new InstallerProcess(101);
    const spawn = vi.fn(() => child.process);
    const destination = path.resolve('Crew Setup.exe');
    const openPath = vi.fn();
    const handoff = launchUpdateInstaller(destination, { platform: 'win32', spawn, openPath });

    expect(spawn).toHaveBeenCalledExactlyOnceWith(destination, ['/SILENT', '/NORESTART'], {
      detached: true, stdio: 'ignore', windowsHide: true,
    });
    expect(child.unref).not.toHaveBeenCalled();
    child.emit('spawn');
    await expect(handoff).resolves.toEqual({ kind: 'installer-started' });
    expect(child.unref).toHaveBeenCalledOnce();
    expect(openPath).not.toHaveBeenCalled();
  });

  it('rejects an asynchronous native launch error without unref or a fallback that might double launch', async () => {
    const child = new InstallerProcess(undefined);
    const spawn = vi.fn(() => child.process);
    const openPath = vi.fn();
    const handoff = launchUpdateInstaller('Crew Setup.exe', { platform: 'win32', spawn, openPath });
    child.emit('error', new Error('ENOENT'));
    await expect(handoff).rejects.toThrow('ENOENT');
    expect(child.unref).not.toHaveBeenCalled();
    expect(child.kill).not.toHaveBeenCalled();
    expect(openPath).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('rejects a missing launch acknowledgement and stops only its own late child', async () => {
    const child = new InstallerProcess(undefined);
    const handoff = launchUpdateInstaller('Crew Setup.exe', {
      platform: 'win32', spawn: () => child.process, openPath: vi.fn(), timeoutMs: 20,
    });
    const failure = expect(handoff).rejects.toThrow('等待安装程序启动超时');
    await vi.advanceTimersByTimeAsync(20);
    await failure;
    expect(child.unref).not.toHaveBeenCalled();
    expect(child.kill).not.toHaveBeenCalled();

    child.pid = 101;
    child.emit('spawn');
    expect(child.kill).toHaveBeenCalledOnce();
    expect(child.unref).not.toHaveBeenCalled();
  });

  it.each([
    { uid: 0, command: 'dpkg', prefix: ['-i'] },
    { uid: 101, command: 'pkexec', prefix: ['dpkg', '-i'] },
  ])('uses $command for a Linux installer and schedules restart only after launch', async ({ uid, command, prefix }) => {
    const installer = new InstallerProcess(202);
    const restarter = new InstallerProcess(203);
    const spawn = vi.fn().mockReturnValueOnce(installer.process).mockReturnValueOnce(restarter.process);
    const chmod = vi.fn().mockResolvedValue(undefined);
    const destination = path.resolve('crew-desktop.deb');
    const handoff = launchUpdateInstaller(destination, {
      platform: 'linux', spawn, openPath: vi.fn(), getuid: () => uid, chmod,
    });
    await Promise.resolve();

    expect(chmod).toHaveBeenCalledExactlyOnceWith(destination, 0o644);
    expect(spawn).toHaveBeenCalledExactlyOnceWith(command, [...prefix, destination], { detached: true, stdio: 'ignore' });
    expect(installer.unref).not.toHaveBeenCalled();
    installer.emit('spawn');
    await Promise.resolve();
    expect(spawn).toHaveBeenLastCalledWith(
      'sh', ['-c', expect.any(String), 'crew-restarter', '202', String(process.pid)], { detached: true, stdio: 'ignore' },
    );
    expect(restarter.unref).not.toHaveBeenCalled();
    restarter.emit('spawn');
    await expect(handoff).resolves.toEqual({ kind: 'installer-started', restartScheduled: true });
    expect(installer.unref).toHaveBeenCalledOnce();
    expect(restarter.unref).toHaveBeenCalledOnce();
  });

  it('reports a missing Linux restarter without treating the confirmed installation as retryable', async () => {
    const installer = new InstallerProcess(202);
    const restarter = new InstallerProcess(undefined);
    const spawn = vi.fn().mockReturnValueOnce(installer.process).mockReturnValueOnce(restarter.process);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const handoff = launchUpdateInstaller('crew-desktop.deb', {
        platform: 'linux', spawn, openPath: vi.fn(), getuid: () => 0, chmod: vi.fn(),
      });
      await Promise.resolve();
      installer.emit('spawn');
      await Promise.resolve();
      restarter.emit('error', new Error('shell unavailable'));
      await expect(handoff).resolves.toEqual({ kind: 'installer-started', restartScheduled: false });
      expect(installer.kill).not.toHaveBeenCalled();
      expect(restarter.unref).not.toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });

  it('hands a macOS disk image to the confirmed open process', async () => {
    const child = new InstallerProcess(303);
    const destination = path.resolve('Crew.dmg');
    const spawn = vi.fn(() => child.process);
    const handoff = launchUpdateInstaller(destination, { platform: 'darwin', spawn, openPath: vi.fn() });
    expect(spawn).toHaveBeenCalledExactlyOnceWith('open', [destination], { detached: true, stdio: 'ignore' });
    expect(child.unref).not.toHaveBeenCalled();
    child.emit('spawn');
    await expect(handoff).resolves.toEqual({ kind: 'package-opened' });
    expect(child.unref).toHaveBeenCalledOnce();
  });

  it('awaits generic OS opening, including reported failure and deadline', async () => {
    const destination = path.resolve('Crew.zip');
    const spawn = vi.fn();
    const openPath = vi.fn().mockResolvedValueOnce('').mockResolvedValueOnce('handler unavailable')
      .mockImplementationOnce(() => new Promise(() => {}));
    const options = { platform: 'darwin' as const, spawn, openPath, timeoutMs: 20 };
    await expect(launchUpdateInstaller(destination, options)).resolves.toEqual({ kind: 'package-opened' });
    await expect(launchUpdateInstaller(destination, options)).rejects.toThrow('handler unavailable');
    const blocked = expect(launchUpdateInstaller(destination, options)).rejects.toThrow('等待系统打开安装包超时');
    await vi.advanceTimersByTimeAsync(20);
    await blocked;
    expect(openPath).toHaveBeenNthCalledWith(3, destination);
    expect(spawn).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
