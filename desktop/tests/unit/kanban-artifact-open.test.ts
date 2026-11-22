// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { activateKanbanInspectorTab, buildKanbanInspectorHtml } from '../../src/ui/features/kanban-board';
import { setActiveSessionId, state } from '../../src/ui/state';
import type { Task } from '../../src/ui/backend-client';

const { openInspectorToTab } = vi.hoisted(() => ({ openInspectorToTab: vi.fn() }));
vi.mock('../../src/ui/features/inspector', () => ({ openInspectorToTab }));

const sid = 'kanban-session';
const artifact = 'C:\\Users\\demo\\.Crew\\plans\\demo.md';

function renderTaskArtifact(path: string): void {
  state.tasks = [{
    id: 'task-artifact',
    task_id: 'task-artifact',
    kind: 'shell',
    title: '生成产物',
    status: 'completed',
    output_ref: path,
  } as Task];
  document.body.innerHTML = `<div id="chat-inspector-body">${buildKanbanInspectorHtml()}</div>`;
  activateKanbanInspectorTab();
}

function fileButton(path: string): HTMLButtonElement {
  const match = [...document.querySelectorAll<HTMLButtonElement>('[data-kanban-open-path]')]
    .find((button) => button.dataset.kanbanOpenPath === path);
  if (!match) throw new Error(`missing file action for ${path}`);
  return match;
}

function folderButton(path: string): HTMLButtonElement {
  const match = [...document.querySelectorAll<HTMLButtonElement>('[data-kanban-open-folder]')]
    .find((button) => button.dataset.kanbanOpenFolder === path);
  if (!match) throw new Error(`missing folder action for ${path}`);
  return match;
}

beforeEach(() => {
  setActiveSessionId(sid);
  state.mode = 'dynamic_kanban';
  state.tasks = [];
  openInspectorToTab.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  setActiveSessionId(null);
  state.mode = 'agent';
  state.tasks = [];
  document.body.replaceChildren();
});

describe('task board artifact actions', () => {
  it('opens a supported text artifact in the Files preview without an OS open', async () => {
    const openPath = vi.fn(async () => '');
    vi.stubGlobal('Crew', { openPath });
    renderTaskArtifact(artifact);
    fileButton(artifact).click();
    await vi.waitFor(() => expect(openInspectorToTab).toHaveBeenCalledTimes(1));
    expect(openInspectorToTab).toHaveBeenCalledWith('files', {
      expandFilePath: artifact, filePaths: [artifact],
    });
    expect(openPath).not.toHaveBeenCalled();
  });

  it('opens a supported binary artifact in the Files preview', async () => {
    const path = 'C:\\Users\\demo\\.Crew\\plans\\report.pptx';
    const openPath = vi.fn(async () => '');
    vi.stubGlobal('Crew', { openPath });
    renderTaskArtifact(path);
    fileButton(path).click();
    await vi.waitFor(() => expect(openInspectorToTab).toHaveBeenCalledTimes(1));
    expect(openInspectorToTab).toHaveBeenCalledWith('files', {
      expandFilePath: path, filePaths: [path],
    });
    expect(openPath).not.toHaveBeenCalled();
  });

  it('falls back to the OS for formats the previewer does not handle', async () => {
    const path = 'C:\\Users\\demo\\.Crew\\plans\\archive.zip';
    const openPath = vi.fn(async () => '');
    vi.stubGlobal('Crew', { openPath });
    renderTaskArtifact(path);
    fileButton(path).click();
    await vi.waitFor(() => expect(openPath).toHaveBeenCalledTimes(1));
    expect(openPath).toHaveBeenCalledWith(path, undefined);
    expect(openInspectorToTab).not.toHaveBeenCalled();
  });

  it('opens the containing folder through the OS instead of the file preview', async () => {
    const folder = 'C:\\Users\\demo\\.Crew\\plans';
    const openPath = vi.fn(async () => '');
    vi.stubGlobal('Crew', { openPath });
    renderTaskArtifact(artifact);
    folderButton(folder).click();
    await vi.waitFor(() => expect(openPath).toHaveBeenCalledTimes(1));
    expect(openPath).toHaveBeenCalledWith(folder, undefined);
    expect(openInspectorToTab).not.toHaveBeenCalled();
  });

  it('drops the preview when the session changes before its panel loads', async () => {
    const openPath = vi.fn(async () => '');
    vi.stubGlobal('Crew', { openPath });
    renderTaskArtifact(artifact);
    fileButton(artifact).click();
    setActiveSessionId('another-session');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(openInspectorToTab).not.toHaveBeenCalled();
    expect(openPath).not.toHaveBeenCalled();
  });
});
