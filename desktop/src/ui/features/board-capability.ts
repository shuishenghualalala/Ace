/**
 * Team / Dynamic Kanban 看板能力接线（capability-driven install/uninstall）。
 *
 * 后端 /api/config 返回的 feature_capabilities 决定两块看板是否安装：
 * - config 未加载（null）→ 视为可用，保持今日全量安装行为；
 * - 旧后端未返回 feature_capabilities 字段 → 同上（legacy-safe）；
 * - 否则按 featureId 查 available。
 * 与 wiki:config-change 同构：loadConfig() 成功/失败后广播事件，安装单元自行同步启停。
 */
import type { BackendConfig } from '../backend-client';
import { state } from '../state';
import { disposeKanbanBoard, initKanbanBoard } from './kanban-board';
import { disposeTeamCollaborationBoard, initTeamCollaborationBoard } from './team-collaboration-board';

export const TEAM_FEATURE_ID = 'product.team';
export const KANBAN_FEATURE_ID = 'product.dynamic-kanban';

function boardCapabilityEnabled(config: BackendConfig | null | undefined, featureId: string): boolean {
  if (config == null || config.feature_capabilities == null) return true;
  return Boolean(config.feature_capabilities[featureId]?.available);
}

export function teamBoardEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  return boardCapabilityEnabled(config, TEAM_FEATURE_ID);
}

export function kanbanBoardEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  return boardCapabilityEnabled(config, KANBAN_FEATURE_ID);
}

export function syncBoardCapabilityUi(): void {
  window.dispatchEvent(new CustomEvent('team-kanban:config-change'));
}

export function bindBoardCapability(onChange: (team: boolean, kanban: boolean) => void): () => void {
  const handler = (): void => onChange(teamBoardEnabled(), kanbanBoardEnabled());
  window.addEventListener('team-kanban:config-change', handler);
  handler();
  return () => window.removeEventListener('team-kanban:config-change', handler);
}

// 当前安装单元的 disposer；app.ts 以 registerDispose(disposeTeamKanbanBoards) 按名引用。
let installDisposer: (() => void) | null = null;

/** 显式卸载：撤销能力订阅并销毁两块看板（board init/dispose 均幂等，重复调用安全）。 */
export function disposeTeamKanbanBoards(): void {
  installDisposer?.();
  installDisposer = null;
}

/**
 * 安装 Team / Dynamic Kanban 两块看板：
 * - 立即 init 能力可用的看板；
 * - 订阅能力变化，enable → init、disable → dispose，两块看板独立启停；
 * - 返回的 disposer 与 disposeTeamKanbanBoards() 共享同一条清理路径，重复安装幂等。
 */
export function installTeamKanbanBoards(): () => void {
  if (installDisposer) return disposeTeamKanbanBoards;
  if (teamBoardEnabled()) initTeamCollaborationBoard();
  if (kanbanBoardEnabled()) initKanbanBoard();
  const unbind = bindBoardCapability((team, kanban) => {
    if (team) initTeamCollaborationBoard();
    else disposeTeamCollaborationBoard();
    if (kanban) initKanbanBoard();
    else disposeKanbanBoard();
  });
  installDisposer = (): void => {
    unbind();
    disposeTeamCollaborationBoard();
    disposeKanbanBoard();
  };
  return disposeTeamKanbanBoards;
}
