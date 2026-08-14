/**
 * Dynamic Kanban 看板纯函数测试。
 *
 * 聚焦：
 * - buildKanbanInspectorHtml 输出不含 Mermaid，含工作空间按钮
 * - 运行时任务过滤（agent_turn 容器任务不应混入看板）
 * - scheduleRefreshKanbanBoard 节流行为
 *
 * @vitest-environment happy-dom
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildKanbanInspectorHtml,
  isRelevantRuntimeTask,
} from '../../src/ui/features/kanban-board';
import {
  setActiveSessionId,
  state,
} from '../../src/ui/state';
import type { Task } from '../../src/ui/backend-client';

describe('buildKanbanInspectorHtml', () => {
  beforeEach(() => {
    const sid = 'team-session-1';
    setActiveSessionId(sid);
    state.mode = 'dynamic_kanban';
  });

  afterEach(() => {
    setActiveSessionId(null);
    state.tasks = [];
  });

  it('检查器输出：不含 Mermaid 与工作目录按钮，包含 workflow timeline 容器', () => {
    const html = buildKanbanInspectorHtml();
    expect(html.toLowerCase()).not.toContain('mermaid');
    expect(html).not.toContain('flowchart');
    expect(html).not.toContain('.mermaid');
    expect(html).not.toContain('id="kanban-workspace"');
    expect(html).not.toContain('打开工作目录');
    expect(html).toContain('workflow-timeline-container');
  });

  it('运行时输出文件提供打开文件和打开文件夹动作', () => {
    state.tasks = [{
      id: 'task-file',
      task_id: 'task-file',
      kind: 'shell',
      title: '生成 PPT',
      status: 'completed',
      output_ref: 'C:\\Users\\demo\\.Crew\\plans\\demo.pptx',
    } as Task];

    const html = buildKanbanInspectorHtml();

    expect(html).toContain('data-kanban-open-path="C:\\Users\\demo\\.Crew\\plans\\demo.pptx"');
    expect(html).toContain('data-kanban-open-path="C:\\Users\\demo\\.Crew\\plans"');
  });
});

describe('isRelevantRuntimeTask', () => {
  it('过滤 agent_turn 容器任务', () => {
    const agentTurnTask = {
      id: 'task-1',
      task_id: 'task-1',
      kind: 'agent_turn',
      title: '帮我实现登录功能',
      status: 'running',
    } as Task;
    expect(isRelevantRuntimeTask(agentTurnTask)).toBe(false);
  });

  it('保留后台化任务', () => {
    const bgTask = {
      id: 'task-2',
      task_id: 'task-2',
      kind: 'shell',
      title: '后台脚本',
      status: 'running',
      backgrounded: true,
    } as Task;
    expect(isRelevantRuntimeTask(bgTask)).toBe(true);
  });

  it('保留 shell / subagent / team 任务', () => {
    for (const kind of ['shell', 'subagent', 'team']) {
      const task = {
        id: `task-${kind}`,
        task_id: `task-${kind}`,
        kind,
        title: `${kind} 任务`,
        status: 'running',
      } as Task;
      expect(isRelevantRuntimeTask(task)).toBe(true);
    }
  });
});

describe('scheduleRefreshKanbanBoard', () => {
  it('300ms 内多次调用只注册一次定时器', async () => {
    vi.useFakeTimers();
    const mod = await import('../../src/ui/features/kanban-board');
    const spy = vi.spyOn(window, 'setTimeout');

    mod.scheduleRefreshKanbanBoard();
    mod.scheduleRefreshKanbanBoard();
    mod.scheduleRefreshKanbanBoard();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenLastCalledWith(expect.any(Function), 300);

    spy.mockRestore();
    vi.useRealTimers();
  });
});
