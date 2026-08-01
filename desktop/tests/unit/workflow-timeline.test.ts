/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildWorkflowTimelineHtml,
  refreshWorkflowTimelineDom,
} from '../../src/ui/features/workflow-timeline';
import { __resetAllStoresForTest, configStore, sessionStore } from '../../src/ui/stores/stores';
import type { DynamicKanbanStatus } from '../../src/ui/backend-client';

vi.mock('../../src/ui/features/chat-controller', () => ({
  applyChunk: vi.fn(),
  stopGeneration: vi.fn(),
}));

const workflowDefinition: NonNullable<DynamicKanbanStatus['workflow_definition']> = {
  summary: '深度研究',
  max_concurrent: 3,
  phases: [
    { id: 'plan', name: '制定研究计划', description: 'plan desc' },
    { id: 'research', name: '多源搜集与分析', description: 'research desc' },
    { id: 'write', name: '撰写研究报告', description: 'write desc' },
  ],
};

function makeStatus(overrides: Partial<DynamicKanbanStatus> = {}): DynamicKanbanStatus {
  return {
    workflow: { status: 'active' },
    workflow_definition: workflowDefinition,
    runtime_state: {
      workflow_id: 'wf_1',
      status: 'active',
      current_phase_id: 'research',
      completed_phase_ids: ['plan'],
    },
    board: { workflow_id: 'wf_1', tasks: [], dependencies: [], events: [] },
    ...overrides,
  };
}

describe('workflow-timeline', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
    document.body.innerHTML = '<div id="workflow-timeline-container"></div>';
    sessionStore.set({ activeSessionId: 'sid-1' });
    configStore.set({ mode: 'dynamic_kanban' });
  });

  it('renders empty hint when status has no workflow definition', () => {
    const html = buildWorkflowTimelineHtml(makeStatus({ workflow_definition: null }));
    expect(html).toContain('暂无阶段信息');
  });

  it('renders correct number of phases from workflow definition', () => {
    const html = buildWorkflowTimelineHtml(makeStatus());
    expect(html).toContain('制定研究计划');
    expect(html).toContain('多源搜集与分析');
    expect(html).toContain('撰写研究报告');
    // 每个 phase 对应一个 li，精确匹配 class 属性中的 base 类名
    const matches = html.match(/class="[^"]*workflow-timeline__phase[\s"]/g);
    expect(matches?.length).toBe(3);
  });

  it('highlights current phase based on runtime_state', () => {
    refreshWorkflowTimelineDom(makeStatus());
    const phases = document.querySelectorAll('.workflow-timeline__phase');
    expect(phases.length).toBe(3);
    expect(phases[0].classList.contains('workflow-timeline__phase--done')).toBe(true);
    expect(phases[1].classList.contains('workflow-timeline__phase--running')).toBe(true);
    expect(phases[2].classList.contains('workflow-timeline__phase--pending')).toBe(true);
  });

  it('disables pause/resume buttons according to workflow status', () => {
    const activeHtml = buildWorkflowTimelineHtml(makeStatus({ workflow: { status: 'active' } }));
    expect(activeHtml).toContain('workflow-timeline-pause');
    expect(activeHtml).not.toContain('id="workflow-timeline-pause" disabled');
    expect(activeHtml).toContain('id="workflow-timeline-resume" disabled');

    const pausedHtml = buildWorkflowTimelineHtml(makeStatus({ workflow: { status: 'paused' } }));
    expect(pausedHtml).toContain('id="workflow-timeline-pause" disabled');
    expect(pausedHtml).not.toContain('id="workflow-timeline-resume" disabled');
  });

  it('refreshWorkflowTimelineDom updates container html', () => {
    refreshWorkflowTimelineDom(makeStatus());
    const container = document.getElementById('workflow-timeline-container');
    expect(container?.textContent).toContain('制定研究计划');
    expect(container?.querySelectorAll('.workflow-timeline__phase').length).toBe(3);
  });

  it('renders call-level results under each phase', () => {
    const status = makeStatus({
      workflow_definition: {
        summary: '深度研究',
        max_concurrent: 3,
        phases: [{
          id: 'research',
          name: '多源搜集与分析',
          description: 'research desc',
          agent_calls: [{ id: 'gather', role: 'analyst' }],
        }],
      },
      runtime_state: {
        workflow_id: 'wf_1',
        status: 'active',
        current_phase_id: 'research',
        completed_phase_ids: [],
        phase_results: {
          research: {
            status: 'running',
            call_results: {
              gather: { status: 'done', role: 'analyst', text: '搜集到 10 条来源', artifacts: ['sources/references.md'] },
            },
          },
        },
      },
    });
    const html = buildWorkflowTimelineHtml(status);
    expect(html).toContain('analyst');
    expect(html).toContain('搜集到 10 条来源');
    expect(html).toContain('workflow-timeline__call--done');
  });
});
