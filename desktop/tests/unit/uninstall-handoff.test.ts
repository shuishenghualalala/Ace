import type { BrowserWindow } from 'electron';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const fixture = vi.hoisted(() => ({
  showMessageBoxSync: vi.fn(),
  writeFileSync: vi.fn(),
  launch: vi.fn(),
  image: { isEmpty: () => false },
  crewHome: '/ace-uninstall-test/crew',
  userData: '/ace-uninstall-test/user-data',
  executable: '/ace-uninstall-test/Crew.app/Contents/MacOS/crew-desktop',
}));

vi.mock('electron', () => ({
  app: { getPath: (name: string) => name === 'exe' ? fixture.executable : fixture.userData },
  dialog: { showMessageBoxSync: fixture.showMessageBoxSync },
  nativeImage: { createFromPath: () => fixture.image },
}));
vi.mock('fs', () => ({ existsSync: () => true, writeFileSync: fixture.writeFileSync }));
vi.mock('../../src/main/update/installer-handoff', () => ({ launchDetachedInstaller: fixture.launch }));

import { handleUninstall, setUninstallDeps } from '../../src/main/uninstall';

function gate() {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  return { pending, release };
}

function installDeps(preparation: Promise<void> | null = Promise.resolve()) {
  const dependencies = {
    getMainWindow: () => ({ isDestroyed: () => false }) as BrowserWindow,
    beginHandoff: vi.fn(() => preparation),
    finishHandoff: vi.fn(),
    suppressBackendOverlay: vi.fn(),
    cleanupSecurity: vi.fn().mockResolvedValue(true),
  };
  setUninstallDeps(dependencies);
  return dependencies;
}

describe('uninstall shared handoff', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubEnv('CREW_HOME', fixture.crewHome);
    fixture.showMessageBoxSync.mockReturnValue(0);
    fixture.launch.mockResolvedValue(undefined);
  });
  afterEach(() => vi.unstubAllEnvs());

  it('leaves the app running when the uninstall confirmation is cancelled or another shutdown owns the handoff', async () => {
    const busy = installDeps(null);
    fixture.showMessageBoxSync.mockReturnValueOnce(1);
    await handleUninstall();
    expect(busy.beginHandoff).not.toHaveBeenCalled();
    await handleUninstall();
    expect(busy.beginHandoff).toHaveBeenCalledOnce();
    expect(busy.finishHandoff).not.toHaveBeenCalled();
    expect(fixture.launch).not.toHaveBeenCalled();
  });

  it('waits for the drain and native launch before committing, with user data retained', async () => {
    const drain = gate();
    const launch = gate();
    const dependencies = installDeps(drain.pending);
    fixture.launch.mockReturnValue(launch.pending);
    const running = handleUninstall();

    expect(dependencies.suppressBackendOverlay).toHaveBeenCalledOnce();
    expect(dependencies.cleanupSecurity).not.toHaveBeenCalled();
    expect(fixture.launch).not.toHaveBeenCalled();
    drain.release();
    await vi.waitFor(() => expect(fixture.launch).toHaveBeenCalledOnce());
    const script = fixture.writeFileSync.mock.calls[0]?.[1];
    expect(script).not.toContain(fixture.crewHome);
    expect(script).not.toContain(fixture.userData);
    expect(dependencies.finishHandoff).not.toHaveBeenCalled();
    launch.release();
    await running;
    expect(dependencies.finishHandoff).toHaveBeenCalledExactlyOnceWith(true);
  });

  it('uses the same two deletion directories and releases the gate when native launch fails', async () => {
    const dependencies = installDeps();
    fixture.showMessageBoxSync.mockReturnValueOnce(0).mockReturnValueOnce(1);
    fixture.launch.mockRejectedValue(new Error('native launch failed'));
    await handleUninstall();
    const script = fixture.writeFileSync.mock.calls[0]?.[1];
    expect(script).toContain(fixture.crewHome);
    expect(script).toContain(fixture.userData);
    expect(fixture.launch).toHaveBeenCalledOnce();
    expect(dependencies.finishHandoff).toHaveBeenCalledExactlyOnceWith(false);
    expect(fixture.showMessageBoxSync).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
      type: 'error', detail: 'native launch failed',
    }));
  });

  it('reports a failed drain and releases the gate without invoking cleanup or launching', async () => {
    const dependencies = installDeps(Promise.reject(new Error('drain failed')));
    await handleUninstall();
    expect(dependencies.cleanupSecurity).not.toHaveBeenCalled();
    expect(fixture.launch).not.toHaveBeenCalled();
    expect(dependencies.finishHandoff).toHaveBeenCalledExactlyOnceWith(false);
  });
});
