/** @vitest-environment happy-dom */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { activateCollaborationPreview, renderCollaborationPreview, setPreviewTeam } from '../../src/ui/features/collaboration-preview';

describe('collaboration planning preview', () => {
  const sessionId = 'preview-test';
  beforeEach(() => {
    document.body.innerHTML = '';
    setPreviewTeam(sessionId, 'team-a');
    vi.restoreAllMocks();
  });

  it('clearly identifies the preview and displays work and dependencies', async () => {
    const result = { task: { task_id: 't', goal: '审查', team: { members: [{ member_id: 'a', name: 'kk' }] } },
      state: 'planned', error: '', plan: {
        work_units: [{ unit_id: 'u', objective: '检查代码', expected_outputs: ['审查报告'], completion_criteria: ['列出问题'], depends_on: [], required_tools: [] }],
        assignments: [{ unit_id: 'u', member_id: 'a', reason: '负责审查', responsibility_ref: '负责审查' }], decisions: [],
      } };
    const call = vi.spyOn(backendApi, 'createCollaborationPreview').mockResolvedValue({ ok: true, preview: result });
    const render = () => { document.body.innerHTML = renderCollaborationPreview(sessionId); activateCollaborationPreview(sessionId); };
    const handler = () => render();
    window.addEventListener('team-collaboration:updated', handler);
    render();
    document.querySelector<HTMLButtonElement>('[data-preview-toggle]')?.click();
    expect(document.body.textContent).toContain('预览不会执行任务');
    const input = document.querySelector<HTMLTextAreaElement>('[data-preview-goal]')!;
    input.value = '审查代码';
    input.dispatchEvent(new Event('input'));
    document.querySelector<HTMLButtonElement>('[data-preview-create]')?.click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('检查代码'));
    expect(document.body.textContent).toContain('负责人：kk');
    expect(document.body.textContent).toContain('无前置工作');
    expect(call).toHaveBeenCalledOnce();
    window.removeEventListener('team-collaboration:updated', handler);
  });
});
