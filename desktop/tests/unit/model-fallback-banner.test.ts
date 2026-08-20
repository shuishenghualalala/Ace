// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest';

const sessionModelMock = vi.hoisted(() => ({
  sessionDemoMode: vi.fn((): boolean | null => null),
  isExternalTeamSession: vi.fn(() => false),
}));
const openModelPaneMock = vi.hoisted(() => vi.fn());

vi.mock('../../src/ui/features/session-model', () => sessionModelMock);
vi.mock('../../src/ui/features/model-tour', () => ({ openModelPane: openModelPaneMock }));

import {
  deriveModelFallbackVisible,
  renderModelFallbackBanner,
} from '../../src/ui/features/model-fallback-banner';

describe('deriveModelFallbackVisible', () => {
  it('服务端判定演示模式且非外部会话时显示', () => {
    expect(deriveModelFallbackVisible(true, false)).toBe(true);
  });

  it('外部 Team 会话不显示（服务端不会下发，前端仍防御）', () => {
    expect(deriveModelFallbackVisible(true, true)).toBe(false);
    expect(deriveModelFallbackVisible(false, true)).toBe(false);
  });

  it('非演示模式时不显示', () => {
    expect(deriveModelFallbackVisible(false, false)).toBe(false);
  });

  it('绑定未加载 / 草稿会话（判定未知）时不显示，宁缺毋误报', () => {
    expect(deriveModelFallbackVisible(null, false)).toBe(false);
    expect(deriveModelFallbackVisible(undefined, false)).toBe(false);
  });
});

describe('renderModelFallbackBanner', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div class="chat-composer"><div class="composer-edit-banner"></div></div>';
    sessionModelMock.sessionDemoMode.mockReturnValue(true);
    sessionModelMock.isExternalTeamSession.mockReturnValue(false);
    openModelPaneMock.mockClear();
  });

  it('演示模式时挂载横幅，点击按钮打开模型设置', () => {
    renderModelFallbackBanner();
    const banner = document.getElementById('model-fallback-banner');
    expect(banner).toBeTruthy();
    expect(banner?.classList.contains('show')).toBe(true);
    expect(banner?.textContent).toContain('演示模式');
    banner?.querySelector<HTMLButtonElement>('[data-action="configure"]')?.click();
    expect(openModelPaneMock).toHaveBeenCalled();
  });

  it('恢复真实模型后横幅移除', () => {
    renderModelFallbackBanner();
    expect(document.getElementById('model-fallback-banner')).toBeTruthy();
    sessionModelMock.sessionDemoMode.mockReturnValue(false);
    renderModelFallbackBanner();
    expect(document.getElementById('model-fallback-banner')).toBeNull();
  });

  it('demo_mode 未知（绑定未加载）时不挂载横幅', () => {
    sessionModelMock.sessionDemoMode.mockReturnValue(null);
    renderModelFallbackBanner();
    expect(document.getElementById('model-fallback-banner')).toBeNull();
  });

  it('外部 Team 会话不挂载横幅', () => {
    sessionModelMock.isExternalTeamSession.mockReturnValue(true);
    renderModelFallbackBanner();
    expect(document.getElementById('model-fallback-banner')).toBeNull();
  });

  it('幂等：重复调用不产生重复横幅', () => {
    renderModelFallbackBanner();
    renderModelFallbackBanner();
    expect(document.querySelectorAll('#model-fallback-banner').length).toBe(1);
  });

  it('会话模型绑定变化事件触发横幅重渲染', () => {
    sessionModelMock.sessionDemoMode.mockReturnValue(false);
    renderModelFallbackBanner();
    expect(document.getElementById('model-fallback-banner')).toBeNull();
    sessionModelMock.sessionDemoMode.mockReturnValue(true);
    window.dispatchEvent(new CustomEvent('session:model-changed', { detail: { sessionId: 's1' } }));
    expect(document.getElementById('model-fallback-banner')).toBeTruthy();
  });
});
