/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, authStore, workspaceStore } from '../../src/ui/stores/stores';
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
  // auth-gate 为真实登录判定：local 桌面会话由 initAuthFlow 置为已登录，测试模拟该态。
  authStore.set({ isLoggedIn: true });
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
      reuse_root: true,
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

  it('delegates folder identity once for 500 different spellings and preserves a hidden match', async () => {
    const roots = Array.from({ length: 500 }, (_, i) => ({
      id: String(i), name: 'folder', description: '', instructions: '', root_path: '/Projects/' + i,
    }));
    workspaceStore.set({ workspaces: roots });
    const info = vi.fn();
    window.Crew = {
      selectFolder: vi.fn(async () => ['/projects/499']), workspaceDirectoryInfo: info,
    } as unknown as typeof window.Crew;
    vi.mocked(backendApi.createWorkspace).mockResolvedValue({ ...roots[499]!, hidden: true });
    expect(await createWorkspaceFromFolderPicker(vi.fn())).toBe('499');
    expect(backendApi.createWorkspace).toHaveBeenCalledTimes(1);
    expect(backendApi.workspaces).toHaveBeenCalledTimes(1);
    expect(info).toHaveBeenCalledTimes(0);
    expect(workspaceStore.get().currentWorkspaceId).not.toBe('499');
  });

  it('用户取消选择时不创建', async () => {
    window.Crew = {
      selectFolder: vi.fn(async () => null),
    } as unknown as typeof window.Crew;

    await createWorkspaceFromFolderPicker(vi.fn());

    expect(backendApi.createWorkspace).not.toHaveBeenCalled();
  });
});
