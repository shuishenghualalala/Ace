import type { PlanReviewStatus } from '../chat-render';

export interface PlanBoardUiState {
  sessionId: string | null;
  mode: 'preview' | 'edit';
  draft: string | null;
  otherOpen: boolean;
  otherText: string;
  busy: boolean;
}

export function createPlanBoardUiState(): PlanBoardUiState {
  return {
    sessionId: null,
    mode: 'preview',
    draft: null,
    otherOpen: false,
    otherText: '',
    busy: false,
  };
}

export function syncPlanBoardUiSession(
  state: PlanBoardUiState,
  sessionId: string | null,
): void {
  if (state.sessionId === sessionId) return;
  Object.assign(state, createPlanBoardUiState(), { sessionId });
}

export function planStatusLabel(status: PlanReviewStatus): string {
  switch (status) {
    case 'pending': return '等待审批';
    case 'editing':
    case 'revising': return '继续修改中';
    case 'approved':
    case 'readonly': return '已批准';
    case 'rejected': return '已拒绝';
    case 'cancelled': return '已取消';
    case 'empty': return '计划为空';
    default: return status;
  }
}

export function isPlanActionable(status: PlanReviewStatus): boolean {
  return status === 'pending'
    || status === 'editing'
    || status === 'revising'
    || status === 'empty';
}
