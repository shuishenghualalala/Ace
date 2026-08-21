/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const maybeStartModelTourOnceMock = vi.hoisted(() => vi.fn());
const stateMock = vi.hoisted(() => ({
  state: { config: null as unknown, configModel: '', activeSessionId: null, sessions: [] },
}));

vi.mock('../../src/ui/state', () => ({
  state: stateMock.state,
  $: vi.fn(() => null),
  $$: vi.fn(() => []),
  escapeHtml: (s: string) => s,
  notify: vi.fn(),
}));
vi.mock('../../src/ui/features/model-tour', () => ({
  maybeStartModelTourOnce: maybeStartModelTourOnceMock,
}));

import { backendApi } from '../../src/ui/backend-client';
import { loadConfig } from '../../src/ui/features/model-picker';

describe('loadConfig', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    maybeStartModelTourOnceMock.mockClear();
    stateMock.state.config = null;
  });

  it('配置加载成功后触发模型引导检查（首次无用户模型时弹 tour）', async () => {
    const config = {
      model: 'your-model-name',
      has_key: false,
      base_url: '',
      active_model_id: 'default',
      models: [{ id: 'default', name: 'Default', has_key: false }],
    };
    vi.spyOn(backendApi, 'config').mockResolvedValue(config as never);

    await loadConfig();

    expect(stateMock.state.config).toEqual(config);
    expect(maybeStartModelTourOnceMock).toHaveBeenCalledWith(config);
  });

  it('配置加载失败时不触发引导', async () => {
    vi.spyOn(backendApi, 'config').mockRejectedValue(new Error('down'));

    await loadConfig();

    expect(stateMock.state.config).toBeNull();
    expect(maybeStartModelTourOnceMock).not.toHaveBeenCalled();
  });
});
