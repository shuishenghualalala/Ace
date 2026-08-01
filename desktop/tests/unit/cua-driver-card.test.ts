// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest';
import { cuaDriverStatusText, cuaStepChipClass, cuaStepStatusText, renderCuaProgress } from '../../src/ui/features/settings-mcp';
import type { CuaDriverStatus, CuaSetupProgress } from '../../src/ui/backend-client';

function status(over: Partial<CuaDriverStatus>): CuaDriverStatus {
  return {
    ok: true,
    installed: false,
    binary: null,
    version: '',
    daemon_running: false,
    mcp_enabled: false,
    tools_registered: [],
    ...over,
  };
}

describe('cuaDriverStatusText', () => {
  it('not ok → 状态查询失败 + 重试', () => {
    const r = cuaDriverStatusText(status({ ok: false }));
    expect(r.desc).toBe('状态查询失败');
    expect(r.action).toBe('重试');
  });

  it('not installed → 一键安装', () => {
    const r = cuaDriverStatusText(status({}));
    expect(r.desc).toContain('未安装');
    expect(r.action).toBe('一键安装');
  });

  it('installed but daemon not running → 启动并安装', () => {
    const r = cuaDriverStatusText(status({ installed: true, version: '0.1.0' }));
    expect(r.desc).toContain('后台服务未运行');
    expect(r.action).toBe('启动并安装');
  });

  it('ready (daemon + mcp enabled, tools registered) → 重新安装 + 工具数', () => {
    const r = cuaDriverStatusText(
      status({ installed: true, version: '0.1.0', daemon_running: true, mcp_enabled: true, tools_registered: ['a', 'b'] }),
    );
    expect(r.desc).toContain('已就绪');
    expect(r.desc).toContain('2 个工具');
    expect(r.action).toBe('重新安装');
  });

  it('installed + mcp not enabled → 一键安装', () => {
    const r = cuaDriverStatusText(status({ installed: true, version: '0.1.0', daemon_running: true, mcp_enabled: false }));
    expect(r.action).toBe('一键安装');
  });
});

describe('cuaStepChipClass', () => {
  it('success → is-online', () => {
    expect(cuaStepChipClass('success')).toBe('is-online');
  });
  it('running → is-configured', () => {
    expect(cuaStepChipClass('running')).toBe('is-configured');
  });
  it('failed → is-error', () => {
    expect(cuaStepChipClass('failed')).toBe('is-error');
  });
  it('pending/skipped → empty', () => {
    expect(cuaStepChipClass('pending')).toBe('');
    expect(cuaStepChipClass('skipped')).toBe('');
  });
});

describe('renderCuaProgress', () => {
  it('renders backend-controlled text without interpreting HTML', () => {
    document.body.innerHTML = '<div id="cua-driver-progress"></div>';
    const progress = {
      status: 'running',
      steps: [{ name: 'install_binary', status: 'running', message: '<img src=x onerror=alert(1)>' }],
      log: ['<script>bad()</script>'],
      error: '<b>failed</b>',
    } as CuaSetupProgress;

    renderCuaProgress(progress);

    const box = document.querySelector('#cua-driver-progress') as HTMLElement;
    expect(box.querySelector('img')).toBeNull();
    expect(box.querySelector('script')).toBeNull();
    expect(box.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(box.textContent).toContain('<script>bad()</script>');
    expect(box.textContent).toContain('<b>failed</b>');
  });
});

describe('cuaStepStatusText', () => {
  it('maps known statuses to Chinese', () => {
    expect(cuaStepStatusText('success')).toBe('完成');
    expect(cuaStepStatusText('running')).toBe('进行中');
    expect(cuaStepStatusText('failed')).toBe('失败');
    expect(cuaStepStatusText('skipped')).toBe('跳过');
    expect(cuaStepStatusText('pending')).toBe('等待');
  });
  it('passes through unknown status', () => {
    expect(cuaStepStatusText('weird')).toBe('weird');
  });
});
