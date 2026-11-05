/**
 * Team/Kanban 看板中心调用门面。
 *
 * 目的：app.ts / session-controller.ts / chat-controller.ts 等中心调用方
 * 不直接依赖具体 board 模块，而是调用本门面。kanban-board / team-collaboration-board
 * 在各自 init 阶段把真实回调注册到门面，dispose 时注销；未注册时门面 no-op。
 */

interface KanbanBoardCallbacks {
  /** 拉取并渲染当前会话的 Dynamic Kanban 看板。 */
  refresh: (sessionId?: string | null) => Promise<void>;
  /** 节流刷新，避免流式 chunk 频繁触发后端请求。 */
  scheduleRefresh: (sessionId?: string | null) => void;
  /** 仅触发渲染（不拉后端）。 */
  render: () => void;
}

interface TeamBoardCallbacks {
  /** 在首次发送 Team 消息前预热真实成员身份。 */
  primeTeamIdentity: (sessionId: string) => Promise<void>;
}

let kanbanCallbacks: KanbanBoardCallbacks | null = null;
let teamCallbacks: TeamBoardCallbacks | null = null;

/** Dynamic Kanban 在 init 时注册真实回调；返回的 disposer 用于注销。 */
export function registerKanbanBoardCallbacks(callbacks: KanbanBoardCallbacks): () => void {
  kanbanCallbacks = callbacks;
  return () => {
    kanbanCallbacks = null;
  };
}

/** Team Collaboration 在 init 时注册真实回调；返回的 disposer 用于注销。 */
export function registerTeamBoardCallbacks(callbacks: TeamBoardCallbacks): () => void {
  teamCallbacks = callbacks;
  return () => {
    teamCallbacks = null;
  };
}

/** 刷新 Dynamic Kanban 看板；未注册时 no-op。 */
export async function refreshKanbanBoard(sessionId?: string | null): Promise<void> {
  await kanbanCallbacks?.refresh(sessionId);
}

/** 节流调度 Dynamic Kanban 刷新；未注册时 no-op。 */
export function scheduleRefreshKanbanBoard(sessionId?: string | null): void {
  kanbanCallbacks?.scheduleRefresh(sessionId);
}

/** 触发 Dynamic Kanban 渲染；未注册时 no-op。 */
export function renderKanbanBoard(): void {
  kanbanCallbacks?.render();
}

/** 预热 Team 成员身份；未注册时 no-op。 */
export async function primeTeamCollaborationIdentity(sessionId: string): Promise<void> {
  await teamCallbacks?.primeTeamIdentity(sessionId);
}
