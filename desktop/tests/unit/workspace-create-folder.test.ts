/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, workspaceStore } from '../../src/ui/stores/stores';
import { createWorkspaceFromFolderPicker } from '../../src/ui/features/workspaces';
import { backendApi } from '../../src/ui/backend-client';

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    createWorkspace: vi.fn(),
    workspaces: vi.fn(async () => []),
    sessions: vi.fn(async () => []),
  },
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: vi.fn(async () => true),
}));

beforeEach(() => {
  __resetAllStoresForTest();
  vi.clearAllMocks();
  document.body.innerHTML = '<div id="history-list"></div>';
  window.Crew = {
    selectFolder: vi.fn(async () => ['D:/Projects/MyApp']),
  } as unknown as typeof window.Crew;
});

describe('createWorkspaceFromFolderPicker', () => {
  it('选择文件夹后调用 createWorkspace 并展开侧栏项目', async () => {
    vi.mocked(backendApi.createWorkspace).mockResolvedValue({
      id: 'ws_new',
      name: 'MyApp',
      description: '',
      instructions: '',
      root_path: 'D:/Projects/MyApp',
    });
    vi.mocked(backendApi.workspaces).mockResolvedValue([
      { id: 'default', name: '对话', description: '', instructions: '' },
      { id: 'ws_new', name: 'MyApp', description: '', instructions: '', root_path: 'D:/Projects/MyApp' },
    ]);

    await createWorkspaceFromFolderPicker(vi.fn());

    expect(backendApi.createWorkspace).toHaveBeenCalledWith({
      name: 'MyApp',
      description: '',
      instructions: '',
      root_path: 'D:/Projects/MyApp',
    });
    expect(workspaceStore.get().currentWorkspaceId).toBe('ws_new');
    expect(workspaceStore.get().expandedWorkspaces.ws_new).not.toBe(false);
  });

  it('同目录已存在时不再创建', async () => {
    workspaceStore.set({
      workspaces: [
        { id: 'ws-old', name: 'test', description: '', instructions: '', root_path: 'D:/Projects/MyApp' },
      ],
    });

    await createWorkspaceFromFolderPicker(vi.fn());

    expect(backendApi.createWorkspace).not.toHaveBeenCalled();
    expect(workspaceStore.get().currentWorkspaceId).toBe('ws-old');
  });

  it('用户取消选择时不创建', async () => {
    window.Crew = {
      selectFolder: vi.fn(async () => null),
    } as unknown as typeof window.Crew;

    await createWorkspaceFromFolderPicker(vi.fn());

    expect(backendApi.createWorkspace).not.toHaveBeenCalled();
  });
});
