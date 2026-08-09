/**
 * chat-controller 相关测试共享的依赖 mock。
 *
 * vi.mock 在模块求值时注册，因此本文件必须作为测试文件的**首个 import**
 * （早于 ../../src/ui/features/chat-controller 的引入），mock 才会先生效。
 * 各 mock 取四个 chat 测试文件所需导出的超集；文件特有的 mock
 * （workspaces / session-model 等）仍留在各自测试文件顶部。
 */
import { vi } from 'vitest';

vi.mock('../../src/ui/features/running-intro', () => ({
  setContextCompactionActive: vi.fn(),
  syncRunningIntroSlot: vi.fn(),
}));
vi.mock('../../src/ui/features/usage-tracker', () => ({ recordTurn: vi.fn() }));
vi.mock('../../src/ui/features/cron-page', () => ({ onAfterFinal: vi.fn() }));
vi.mock('../../src/ui/features/kanban-board', () => ({
  refreshKanbanBoard: vi.fn(async () => undefined),
  renderKanbanBoard: vi.fn(),
}));
vi.mock('../../src/ui/features/inspector', () => ({
  isInspectorOpen: vi.fn(() => false),
  openInspectorToTab: vi.fn(),
  refreshInspector: vi.fn(),
  refreshInspectorChrome: vi.fn(),
  resetPlanBoardDraft: vi.fn(),
  invalidateFileDiffCachePaths: vi.fn(),
  setUsageSnapshot: vi.fn(),
  revealPathInFolder: vi.fn(),
}));
vi.mock('../../src/ui/features/composer-toolbar', () => ({
  syncComposerModelLabel: vi.fn(),
  syncComposerWorkspaceLabel: vi.fn(),
}));
vi.mock('../../src/ui/features/model-picker', () => ({ syncModelUi: vi.fn() }));
vi.mock('../../src/ui/features/system-page', () => ({ renderSystemOverview: vi.fn() }));
vi.mock('../../src/ui/features/attachments', () => ({
  takeAttachmentsForSend: vi.fn(() => []),
  renderAttachmentPreview: vi.fn(),
}));
