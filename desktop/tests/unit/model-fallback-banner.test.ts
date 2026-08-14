// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest';

const stateMock = vi.hoisted(() => ({
  state: { config: null as unknown, activeSessionId: null, sessions: [] },
}));
const sessionModelMock = vi.hoisted(() => ({
  activeComposerModelId: vi.fn(() => ''),
  isExternalTeamSession: vi.fn(() => false),
}));
const openModelPaneMock = vi.hoisted(() => vi.fn());

vi.mock('../../src/ui/state', () => ({ state: stateMock.state, notify: vi.fn() }));
vi.mock('../../src/ui/features/session-model', () => sessionModelMock);
vi.mock('../../src/ui/features/model-tour', () => ({ openModelPane: openModelPaneMock }));

import {
  deriveModelFallbackVisible,
  renderModelFallbackBanner,
} from '../../src/ui/features/model-fallback-banner';

const configNoKey = {
  has_key: false,
  active_model_id: 'default',
  models: [
    { id: 'default', has_key: false },
    { id: 'real', has_key: true },
  ],
};

describe('deriveModelFallbackVisible', () => {
  it('config 缺失或外部会话时不显示', () => {
    expect(deriveModelFallbackVisible(null, '', false)).toBe(false);
    expect(deriveModelFallbackVisible(undefined, '', false)).toBe(false);
    expect(deriveModelFallbackVisible(configNoKey, 'default', true)).toBe(false);
  });

  it('全局 active 有 Key 时不显示', () => {
    expect(
      deriveModelFallbackVisible({ ...configNoKey, has_key: true }, 'default', false),
    ).toBe(false);
  });

  it('会话模型与全局 active 都无 Key 时显示', () => {
    expect(deriveModelFallbackVisible(configNoKey, '', false)).toBe(true);
    expect(deriveModelFallbackVisible(configNoKey, 'default', false)).toBe(true);
  });

  it('会话绑定了有 Key 的模型时不显示（真实 provider）', () => {
    expect(deriveModelFallbackVisible(configNoKey, 'real', false)).toBe(false);
  });

  it('会话模型无 Key 且全局也无 Key 时显示', () => {
    const config = {
      ...configNoKey,
      models: [...configNoKey.models, { id: 'nokey', has_key: false }],
    };
    expect(deriveModelFallbackVisible(config, 'nokey', false)).toBe(true);
  });

  it('会话模型 id 不在 config 列表（外部 Runtime 模型）时不显示', () => {
    expect(deriveModelFallbackVisible(configNoKey, 'ext-model-x', false)).toBe(false);
  });
});

describe('renderModelFallbackBanner', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div class="chat-composer"><div class="composer-edit-banner"></div></div>';
    sessionModelMock.activeComposerModelId.mockReturnValue('default');
    sessionModelMock.isExternalTeamSession.mockReturnValue(false);
    openModelPaneMock.mockClear();
  });

  it('演示模式时挂载横幅，点击按钮打开模型设置', () => {
    stateMock.state.config = configNoKey;
    renderModelFallbackBanner();
    const banner = document.getElementById('model-fallback-banner');
    expect(banner).toBeTruthy();
    expect(banner?.classList.contains('show')).toBe(true);
    expect(banner?.textContent).toContain('演示模式');
    banner?.querySelector<HTMLButtonElement>('[data-action="configure"]')?.click();
    expect(openModelPaneMock).toHaveBeenCalled();
  });

  it('恢复真实模型后横幅移除', () => {
    stateMock.state.config = configNoKey;
    renderModelFallbackBanner();
    expect(document.getElementById('model-fallback-banner')).toBeTruthy();
    stateMock.state.config = { ...configNoKey, has_key: true };
    renderModelFallbackBanner();
    expect(document.getElementById('model-fallback-banner')).toBeNull();
  });

  it('幂等：重复调用不产生重复横幅', () => {
    stateMock.state.config = configNoKey;
    renderModelFallbackBanner();
    renderModelFallbackBanner();
    expect(document.querySelectorAll('#model-fallback-banner').length).toBe(1);
  });
});
