/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';
import { state } from '../../src/ui/state';
import {
  __resetSessionModelBindingsForTest,
  activeComposerModelId,
  applySessionModelBinding,
  reconcileSessionModelsAfterDelete,
} from '../../src/ui/features/session-model';

beforeEach(() => {
  __resetAllStoresForTest();
  __resetSessionModelBindingsForTest();
  state.config = {
    active_model_id: 'default-model',
    models: [
      { id: 'default-model', name: 'Default', model: 'gpt-4o', has_key: true, loaded: true },
    ],
    model_profiles: [
      { id: 'default-model', name: 'Default', model: 'gpt-4o', has_key: true, loaded: true },
    ],
  };
  state.activeSessionId = 'sess-1';
  document.body.innerHTML = '<span id="chat-model-picker-inline-label"></span>';
});

describe('reconcileSessionModelsAfterDelete', () => {
  it('当前会话绑定已删模型时回退到默认模型并刷新 UI', () => {
    applySessionModelBinding('sess-1', {
      model_profile_id: 'minimax',
      model_label: 'minimax',
    });

    reconcileSessionModelsAfterDelete('minimax', 'default-model', ['sess-1']);

    expect(activeComposerModelId()).toBe('default-model');
    expect(document.getElementById('chat-model-picker-inline-label')?.textContent).toBe('Default');
  });
});
