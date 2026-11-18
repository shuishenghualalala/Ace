/** @vitest-environment happy-dom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { backendApi, state, isObservationDevLaunch } = vi.hoisted(() => ({
  backendApi: {
    usage: vi.fn(),
    runtimeConcurrency: vi.fn(),
    sessionsStatus: vi.fn(),
    systemMetrics: vi.fn(),
    platforms: vi.fn(),
    systemLogs: vi.fn(),
    tracingCapabilities: vi.fn(),
    tracingLogs: vi.fn(),
  },
  state: { backendConnected: true },
  isObservationDevLaunch: vi.fn(),
}));

vi.mock('../../src/ui/backend-client', () => ({ backendApi }));
vi.mock('../../src/ui/state', () => ({ state }));
vi.mock('../../src/shared/observability', () => ({ isObservationDevLaunch }));

describe('System settings refresh lifecycle', () => {
  let feature: typeof import('../../src/ui/features/system-page');

  const overview = (): HTMLElement => document.getElementById('settings-pane-system')!;
  const logs = (): HTMLElement => document.getElementById('settings-pane-sys-logs')!;
  const modal = (): HTMLElement => document.getElementById('settings-modal')!;

  function open(pane: 'system' | 'sys-logs'): void {
    modal().hidden = false;
    overview().hidden = pane !== 'system';
    logs().hidden = pane !== 'sys-logs';
    feature.setSystemSettingsPane(pane);
  }

  beforeEach(async () => {
    vi.useFakeTimers();
    vi.resetModules();
    vi.resetAllMocks();
    state.backendConnected = true;
    isObservationDevLaunch.mockReturnValue(false);
    backendApi.usage.mockResolvedValue({ total_tokens: 35 });
    backendApi.runtimeConcurrency.mockResolvedValue({ max_active_runs: 2, global_active: 1, global_queued: 0 });
    backendApi.sessionsStatus.mockResolvedValue({});
    backendApi.systemMetrics.mockResolvedValue({ uptime_s: 7200 });
    backendApi.platforms.mockResolvedValue([]);
    backendApi.systemLogs.mockResolvedValue({ items: [], total: 0 });
    document.body.innerHTML = `
      <div id="settings-modal" hidden>
        <section id="settings-pane-system" hidden><div id="system-page-root"></div></section>
        <section id="settings-pane-sys-logs" hidden>
          <input id="sys-logs-keyword">
          <input id="sys-logs-auto-refresh" type="checkbox">
          <div id="sys-logs-list"></div>
        </section>
      </div>
    `;
    feature = await import('../../src/ui/features/system-page');
  });

  afterEach(() => {
    feature.disposeSystemTab();
    document.body.replaceChildren();
    vi.useRealTimers();
  });

  it('fetches overview once on entry, skips duplicate activation and stops after disposal', async () => {
    const dispose = feature.bindSystemTab();
    feature.renderSystemOverview();
    await vi.advanceTimersByTimeAsync(16000);
    expect(backendApi.usage).not.toHaveBeenCalled();
    expect(document.querySelector('[data-system-page]')).toBeNull();

    open('system');
    open('system');
    feature.renderSystemOverview();
    await vi.advanceTimersByTimeAsync(0);
    expect(backendApi.usage).toHaveBeenCalledTimes(1);
    expect(backendApi.platforms).toHaveBeenCalledTimes(1);
    expect(document.getElementById('sys-kpi-memory')?.textContent).toBe('35');
    await vi.advanceTimersByTimeAsync(8000);
    expect(backendApi.usage).toHaveBeenCalledTimes(2);

    dispose();
    feature.renderSystemOverview();
    await vi.advanceTimersByTimeAsync(16000);
    expect(backendApi.usage).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops log polling and pending search when the setting is hidden or closed', async () => {
    feature.bindSystemTab();
    const checkbox = document.getElementById('sys-logs-auto-refresh') as HTMLInputElement;
    checkbox.checked = true;
    open('sys-logs');
    await vi.advanceTimersByTimeAsync(3000);
    expect(backendApi.systemLogs).toHaveBeenCalledTimes(2);

    const keyword = document.getElementById('sys-logs-keyword') as HTMLInputElement;
    keyword.value = 'new query';
    keyword.dispatchEvent(new Event('input'));
    modal().hidden = true;
    await vi.advanceTimersByTimeAsync(3000);
    expect(backendApi.systemLogs).toHaveBeenCalledTimes(2);
    feature.setSystemSettingsPane(null);
    expect(vi.getTimerCount()).toBe(0);

    open('sys-logs');
    await vi.advanceTimersByTimeAsync(0);
    expect(backendApi.systemLogs).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'new query' }));
    open('system');
    await vi.advanceTimersByTimeAsync(3000);
    expect(backendApi.systemLogs).toHaveBeenCalledTimes(3);
  });

  it('ignores late overview reads from a previous activation', async () => {
    let completePrevious: (value: { total_tokens: number }) => void = () => {};
    backendApi.usage.mockReturnValueOnce(new Promise((resolve) => { completePrevious = resolve; }));
    feature.bindSystemTab();
    open('system');
    await vi.advanceTimersByTimeAsync(8000);
    expect(backendApi.usage).toHaveBeenCalledTimes(1);

    open('sys-logs');
    open('system');
    await vi.advanceTimersByTimeAsync(0);
    expect(document.getElementById('sys-kpi-memory')?.textContent).toBe('35');
    completePrevious({ total_tokens: 101 });
    await vi.advanceTimersByTimeAsync(0);
    expect(document.getElementById('sys-kpi-memory')?.textContent).toBe('35');
    expect(backendApi.platforms).toHaveBeenCalledTimes(1);
  });

  it('drops a pending developer log capability check if the user moves away', async () => {
    isObservationDevLaunch.mockReturnValue(true);
    let completeCapabilities: (value: { available: boolean }) => void = () => {};
    backendApi.tracingCapabilities.mockReturnValueOnce(new Promise((resolve) => { completeCapabilities = resolve; }));
    feature.bindSystemTab();
    open('sys-logs');
    expect(backendApi.tracingCapabilities).toHaveBeenCalledTimes(1);
    open('system');
    completeCapabilities({ available: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(backendApi.tracingLogs).not.toHaveBeenCalled();
    expect(backendApi.systemLogs).not.toHaveBeenCalled();
  });
});
