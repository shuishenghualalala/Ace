import { beforeEach, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { ensureNativeVisualCapability } from '../../src/ui/features/native-visual-capability';
vi.mock('../../src/ui/backend-client', () => ({ backendApi: {
  ensureSession: vi.fn(async () => ({})), getSessionAgentConfig: vi.fn(), setSessionAgentConfig: vi.fn(),
} }));
beforeEach(() => vi.clearAllMocks());
it('enables built-in desktop visuals while preserving model, other capabilities and session metadata', async () => {
  vi.mocked(backendApi.getSessionAgentConfig).mockResolvedValue({ executor: 'builtin', model_profile_id: 'selected', capability_profiles: ['existing'] });
  await ensureNativeVisualCapability('s', 'workspace', '原来的标题');
  expect(backendApi.setSessionAgentConfig).toHaveBeenCalledWith('s', {
    executor: 'builtin', model_profile_id: 'selected', capability_profiles: ['existing', 'blueprint.authoring'],
    workspace_id: 'workspace', title: '原来的标题',
  });
});
it('preserves external executors and avoids rewriting already enabled sessions', async () => {
  for (const config of [{ executor: 'external' as const }, { executor: 'builtin' as const, capability_profiles: ['sites.authoring'] }]) {
    vi.mocked(backendApi.getSessionAgentConfig).mockResolvedValue(config);
    await ensureNativeVisualCapability('s', 'w', '标题');
  }
  expect(backendApi.setSessionAgentConfig).not.toHaveBeenCalled();
});
