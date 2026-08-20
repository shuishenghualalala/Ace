/**
 * @vitest-environment happy-dom
 *
 * Composer 模型 chip / 上下文环控制器的多实例隔离（composer 工具栏实例化重构）：
 * 主对话与 Wiki 问答面板各实例化一个控制器，label / 浮层 / 圆环各自跟随自己的
 * getSessionId，session:model-changed 按 detail.sessionId 过滤，互不串扰。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import { createComposerModelControl } from '../../src/ui/features/model-picker';
import { createContextRingController } from '../../src/ui/features/composer-context-ring';
import {
  applySessionModelBinding,
  __resetSessionModelBindingsForTest,
} from '../../src/ui/features/session-model';

const { mockSetSessionModel, mockSessionContext } = vi.hoisted(() => ({
  mockSetSessionModel: vi.fn(),
  mockSessionContext: vi.fn(),
}));

vi.mock('../../src/ui/backend-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/backend-client')>();
  return {
    ...actual,
    backendApi: {
      ...actual.backendApi,
      setSessionModel: mockSetSessionModel,
      sessionContext: mockSessionContext,
    },
  };
});

function seedConfig(): void {
  configStore.set({
    config: {
      model: 'glm-fast',
      has_key: true,
      base_url: '',
      active_model_id: 'glm-fast',
      models: [
        { id: 'glm-fast', name: 'GLM 快速', model: 'glm-4-flash', has_key: true, loaded: true },
        { id: 'minimax-m3', name: 'MiniMax M3', model: 'MiniMax-M3', has_key: true, loaded: true },
      ],
    },
  });
}

function mountChip(): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'mw-context-chip';
  const label = document.createElement('span');
  label.className = 'mw-context-chip__label';
  btn.appendChild(label);
  document.body.appendChild(btn);
  return btn;
}

function chipLabel(btn: HTMLElement): string {
  return btn.querySelector('.mw-context-chip__label')?.textContent ?? '';
}

function mountRing(): { btn: HTMLButtonElement; pct: HTMLElement; progress: SVGCircleElement } {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.hidden = true;
  const pct = document.createElement('span');
  const progress = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  btn.append(pct, progress);
  document.body.appendChild(btn);
  return { btn, pct, progress };
}

beforeEach(() => {
  __resetAllStoresForTest();
  __resetSessionModelBindingsForTest();
  document.body.innerHTML = '';
  mockSetSessionModel.mockReset();
  mockSessionContext.mockReset();
  mockSetSessionModel.mockImplementation(async (_sid: string, id: string) => ({
    model_profile_id: id,
    model_label: `模型-${id}`,
  }));
});

describe('createComposerModelControl', () => {
  it('双实例 label 各自跟随自己的会话，session:model-changed 按 sessionId 过滤', () => {
    seedConfig();
    const chipA = mountChip();
    const chipB = mountChip();
    applySessionModelBinding('sid-a', { model_profile_id: 'glm-fast', model_label: 'GLM 快速' });
    applySessionModelBinding('sid-b', { model_profile_id: 'minimax-m3', model_label: 'MiniMax M3' });
    createComposerModelControl(chipA, { getSessionId: () => 'sid-a' });
    createComposerModelControl(chipB, { getSessionId: () => 'sid-b' });
    expect(chipLabel(chipA)).toBe('GLM 快速');
    expect(chipLabel(chipB)).toBe('MiniMax M3');

    // 只改 sid-a 的绑定：只有 A 的 chip 刷新
    applySessionModelBinding('sid-a', { model_profile_id: 'minimax-m3', model_label: 'MiniMax M3' });
    expect(chipLabel(chipA)).toBe('MiniMax M3');
    expect(chipLabel(chipB)).toBe('MiniMax M3');

    applySessionModelBinding('sid-a', { model_profile_id: 'glm-fast', model_label: 'GLM 快速' });
    expect(chipLabel(chipA)).toBe('GLM 快速');
  });

  it('浮层选择走本会话的会话级接口（带实例 workspaceId）', async () => {
    seedConfig();
    applySessionModelBinding('sid-b', { model_profile_id: 'glm-fast', model_label: 'GLM 快速' });
    const chip = mountChip();
    createComposerModelControl(chip, { getSessionId: () => 'sid-b', workspaceId: 'wiki' });

    chip.click();
    const popover = document.querySelector('.composer-select-popover');
    expect(popover).not.toBeNull();
    expect(popover?.querySelector('.composer-select-item.is-selected')?.getAttribute('data-model-id')).toBe('glm-fast');

    popover?.querySelector<HTMLElement>('[data-model-id="minimax-m3"]')!.click();
    await vi.waitFor(() => {
      expect(mockSetSessionModel).toHaveBeenCalledWith('sid-b', 'minimax-m3', { workspace_id: 'wiki' });
    });
    await vi.waitFor(() => {
      expect(chipLabel(chip)).toBe('模型-minimax-m3');
    });
    expect(document.querySelector('.composer-select-popover')).toBeNull();
  });
});

describe('createContextRingController', () => {
  it('按注入的 getSessionId 拉取用量并渲染百分比', async () => {
    mockSessionContext.mockResolvedValue({ available: true, used_tokens: 50000, max_tokens: 100000, ratio: 0.5, source: 'provider' });
    const els = mountRing();
    const ring = createContextRingController(els, {
      getSessionId: () => 'sid-b',
      resolveWindow: () => 100000,
    });
    ring.refresh();
    await vi.waitFor(() => {
      expect(els.pct.textContent).toBe('50%');
    });
    expect(mockSessionContext).toHaveBeenCalledWith('sid-b');
    expect(els.btn.hidden).toBe(false);
    ring.dispose();
  });

  it('无会话时圆环隐藏且不拉取', () => {
    const els = mountRing();
    const ring = createContextRingController(els, {
      getSessionId: () => null,
      resolveWindow: () => 100000,
    });
    ring.refresh();
    expect(els.btn.hidden).toBe(true);
    expect(mockSessionContext).not.toHaveBeenCalled();
    ring.dispose();
  });
});

describe('createContextRingController 事件过滤与节流', () => {
  const flushMicrotasks = async (): Promise<void> => {
    await Promise.resolve();
    await Promise.resolve();
  };
  const fireMessagesChanged = (sessionId: string): void => {
    window.dispatchEvent(new CustomEvent('messages:changed', { detail: { sessionId } }));
  };

  beforeEach(() => {
    vi.useFakeTimers();
    mockSessionContext.mockResolvedValue({ available: true, used_tokens: 50000, max_tokens: 100000, ratio: 0.5, source: 'provider' });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('messages:changed 只响应本会话，其他会话的事件直接忽略', async () => {
    const els = mountRing();
    const ring = createContextRingController(els, {
      getSessionId: () => 'sid-b',
      resolveWindow: () => 100000,
    });
    ring.refresh();
    await flushMicrotasks();
    expect(mockSessionContext).toHaveBeenCalledTimes(1);

    // 其他会话的流式事件：不拉取，也不挂尾随定时器
    fireMessagesChanged('sid-other');
    await vi.advanceTimersByTimeAsync(5000);
    expect(mockSessionContext).toHaveBeenCalledTimes(1);

    // 本会话事件：距上次拉取已超 2s，立即拉取
    fireMessagesChanged('sid-b');
    await flushMicrotasks();
    expect(mockSessionContext).toHaveBeenCalledTimes(2);
    ring.dispose();
  });

  it('流式期间逐 chunk 事件被节流到 2s 一次，结束后尾随补齐最终用量', async () => {
    const els = mountRing();
    const ring = createContextRingController(els, {
      getSessionId: () => 'sid-b',
      resolveWindow: () => 100000,
    });
    // 模拟流式输出：每 100ms 一个 chunk 事件，持续 3s
    for (let i = 0; i < 30; i++) {
      fireMessagesChanged('sid-b');
      await vi.advanceTimersByTimeAsync(100);
    }
    const duringStream = mockSessionContext.mock.calls.length;
    expect(duringStream).toBeLessThanOrEqual(2); // 首次立即 + 2s 处一次，而非 30 次

    // 流结束后：尾随定时器补齐一次最终刷新
    await vi.advanceTimersByTimeAsync(2500);
    expect(mockSessionContext.mock.calls.length).toBe(duringStream + 1);

    // dispose 后不再有任何拉取
    ring.dispose();
    await vi.advanceTimersByTimeAsync(5000);
    expect(mockSessionContext.mock.calls.length).toBe(duringStream + 1);
  });

  it('isActive 为 false 时不拉取（切走的 tab 不为看不见的圆环白请求）', async () => {
    let active = false;
    const els = mountRing();
    const ring = createContextRingController(els, {
      getSessionId: () => 'sid-b',
      resolveWindow: () => 100000,
      isActive: () => active,
    });
    ring.refresh();
    await flushMicrotasks();
    expect(mockSessionContext).not.toHaveBeenCalled();
    expect(els.btn.hidden).toBe(true);

    active = true;
    ring.refresh();
    await flushMicrotasks();
    expect(mockSessionContext).toHaveBeenCalledTimes(1);
    ring.dispose();
  });

  it('session:model-changed 只响应本会话', async () => {
    const els = mountRing();
    const ring = createContextRingController(els, {
      getSessionId: () => 'sid-b',
      resolveWindow: () => 100000,
    });
    ring.refresh();
    await flushMicrotasks();
    expect(mockSessionContext).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new CustomEvent('session:model-changed', { detail: { sessionId: 'sid-other' } }));
    await flushMicrotasks();
    expect(mockSessionContext).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new CustomEvent('session:model-changed', { detail: { sessionId: 'sid-b' } }));
    await flushMicrotasks();
    expect(mockSessionContext).toHaveBeenCalledTimes(2);
    ring.dispose();
  });
});
