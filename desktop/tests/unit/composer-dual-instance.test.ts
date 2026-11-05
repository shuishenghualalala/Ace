/**
 * @vitest-environment happy-dom
 *
 * 双 Composer 实例隔离单测（对话面板多实例化重构，计划步骤 8）：
 * 主对话 + Wiki 问答面板的 Composer 同页共存时，各自按自己的 getSessionId
 * 渲染待发队列、按自己的 PanelAttachments adapter 计算 hasDraft，互不干扰。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Attachment } from '../../src/ui/backend-client';
import { createComposerView } from '../../src/ui/features/composer-view';
import type { PanelAttachments } from '../../src/ui/features/attachments';
import { __resetAllStoresForTest, authStore, sessionStore, uiStore } from '../../src/ui/stores/stores';
import { enqueuePending, getPendingQueue } from '../../src/ui/state';
import { syncRunningIntroSlot } from '../../src/ui/features/running-intro';

/** 内存态 PanelAttachments stub：行为对齐 wiki-agent 的 per-KB adapter。 */
function stubAttachments(initial: Attachment[] = []): PanelAttachments {
  let list = [...initial];
  const listeners = new Set<() => void>();
  const notifyListeners = (): void => {
    for (const cb of listeners) cb();
  };
  return {
    list: () => [...list],
    add: async () => {},
    remove: (id) => {
      list = list.filter((item) => item.id !== id);
      notifyListeners();
    },
    takeForSend: () => {
      const taken = [...list];
      list = [];
      notifyListeners();
      return taken;
    },
    subscribe: (cb) => {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
  };
}

function createPanelComposer(host: HTMLElement, sessionId: string, attachments: PanelAttachments) {
  return createComposerView(host, {
    submit: vi.fn(),
    stop: vi.fn(),
    cancelEdit: vi.fn(),
    editQueueItem: vi.fn(),
    steerQueueItem: vi.fn(),
    getSessionId: () => sessionId,
    attachments,
    primary: sessionId === 'sid-a',
  });
}

beforeEach(() => {
  __resetAllStoresForTest();
  // Composer 未登录/离线会禁用输入与发送；测试默认已登录且在线。
  authStore.set({ isLoggedIn: true });
  uiStore.set({ backendConnected: true });
  document.body.innerHTML = '<div id="host-a"></div><div id="host-b"></div>';
});

describe('双 Composer 实例隔离', () => {
  it('两个 Composer 按各自 sessionId 渲染待发队列', () => {
    const hostA = document.getElementById('host-a')!;
    const hostB = document.getElementById('host-b')!;
    const viewA = createPanelComposer(hostA, 'sid-a', stubAttachments());
    const viewB = createPanelComposer(hostB, 'sid-b', stubAttachments());

    enqueuePending('sid-a', { id: 'q-a', query: '只属于A的待发', attachments: [] });
    viewA.renderQueue();
    viewB.renderQueue();

    expect(hostA.textContent).toContain('只属于A的待发');
    expect(hostB.textContent).not.toContain('只属于A的待发');

    // 队列操作回调按各自面板的 sessionId 派发：删 A 的队列不影响 B
    hostA.querySelector<HTMLElement>('[data-queue-remove]')!.click();
    expect(getPendingQueue('sid-a')).toHaveLength(0);

    enqueuePending('sid-b', { id: 'q-b', query: '只属于B的待发', attachments: [] });
    viewA.renderQueue();
    viewB.renderQueue();
    expect(hostB.textContent).toContain('只属于B的待发');
    expect(hostA.textContent).not.toContain('只属于B的待发');

    viewA.dispose();
    viewB.dispose();
  });

  it('hasDraft 的附件部分走各自的 PanelAttachments adapter', () => {
    const hostA = document.getElementById('host-a')!;
    const hostB = document.getElementById('host-b')!;
    const attachmentsA = stubAttachments([
      { id: 'att-1', name: 'a.png', path: '/tmp/a.png', type: 'image', size: 10 },
    ]);
    const viewA = createPanelComposer(hostA, 'sid-a', attachmentsA);
    const viewB = createPanelComposer(hostB, 'sid-b', stubAttachments());

    const sendA = hostA.querySelector<HTMLButtonElement>('[data-composer-send]')!;
    const sendB = hostB.querySelector<HTMLButtonElement>('[data-composer-send]')!;
    // A 有待发附件 → 可发送；B 无草稿 → 不可发送
    expect(sendA.disabled).toBe(false);
    expect(sendB.disabled).toBe(true);

    // A 取走附件发送后，A 回到无草稿态；B 不受影响
    attachmentsA.takeForSend();
    viewA.refresh();
    viewB.refresh();
    expect(sendA.disabled).toBe(true);
    expect(sendB.disabled).toBe(true);

    viewA.dispose();
    viewB.dispose();
  });

  it('primary 注册后 composer-scope 查询锚定主 Composer，dispose 后回退文档序', () => {
    const hostA = document.getElementById('host-a')!;
    const hostB = document.getElementById('host-b')!;
    const viewA = createPanelComposer(hostA, 'sid-a', stubAttachments());
    const viewB = createPanelComposer(hostB, 'sid-b', stubAttachments());

    return import('../../src/ui/features/composer-scope').then(({ getPrimaryComposerRoot }) => {
      expect(getPrimaryComposerRoot()).toBe(hostA.querySelector('[data-composer-view]'));
      viewB.dispose();
      expect(getPrimaryComposerRoot()).toBe(hostA.querySelector('[data-composer-view]'));
      viewA.dispose();
      expect(getPrimaryComposerRoot()).toBeNull();
    });
  });

  it('只给主 Composer 提供 Welcome 猛虎爪装饰锚点', () => {
    const hostA = document.getElementById('host-a')!;
    const hostB = document.getElementById('host-b')!;
    const viewA = createPanelComposer(hostA, 'sid-a', stubAttachments());
    const viewB = createPanelComposer(hostB, 'sid-b', stubAttachments());

    expect(hostA.querySelectorAll('.mw-composer__welcome-paw')).toHaveLength(2);
    expect(hostB.querySelector('.mw-composer__welcome-paws')).toBeNull();

    viewA.dispose();
    viewB.dispose();
  });

  it('running-intro 槽位按各自会话的 busy 状态独立显示', () => {
    const hostA = document.getElementById('host-a')!;
    const hostB = document.getElementById('host-b')!;
    const viewA = createPanelComposer(hostA, 'sid-a', stubAttachments());
    const viewB = createPanelComposer(hostB, 'sid-b', stubAttachments());
    const slotA = hostA.querySelector<HTMLElement>('.chat-running-intro')!;
    const slotB = hostB.querySelector<HTMLElement>('.chat-running-intro')!;

    // 仅 B（Wiki 侧）会话执行中：B 显示轮播文案，A 保持隐藏
    sessionStore.set({ busySessions: { 'sid-b': true } });
    syncRunningIntroSlot();
    expect(slotB.hidden).toBe(false);
    expect(slotB.querySelector('.running-intro__status')).not.toBeNull();
    expect(slotA.hidden).toBe(true);
    expect(slotA.firstElementChild).toBeNull();

    // busy 转移到 A：A 显示、B 收起
    sessionStore.set({ busySessions: { 'sid-a': true } });
    syncRunningIntroSlot();
    expect(slotA.hidden).toBe(false);
    expect(slotA.querySelector('.running-intro__status')).not.toBeNull();
    expect(slotB.hidden).toBe(true);

    // dispose 反注册后，B 的槽位不再被 sync 触及
    viewB.dispose();
    sessionStore.set({ busySessions: { 'sid-b': true } });
    syncRunningIntroSlot();
    expect(slotA.hidden).toBe(true);
    expect(slotB.isConnected).toBe(false);

    // 清掉 busy 停掉轮播定时器，避免跨测试泄漏
    sessionStore.set({ busySessions: {} });
    syncRunningIntroSlot();
    viewA.dispose();
  });
});
