import type { PlanReviewStatus } from '../chat-render';

export interface PlanBoardUiState {
  sessionId: string | null;
  mode: 'preview' | 'edit';
  draft: string | null;
  otherOpen: boolean;
  otherText: string;
  busy: boolean;
  basePlan: string;
  baseVersion: string | undefined;
  latestPlan: string;
  latestVersion: string | undefined;
  revision: number;
  stale: boolean;
  submissionStatus: string | undefined;
}

export function createPlanBoardUiState(): PlanBoardUiState {
  return {
    sessionId: null,
    mode: 'preview',
    draft: null,
    otherOpen: false,
    otherText: '',
    busy: false,
    basePlan: '',
    baseVersion: undefined,
    latestPlan: '',
    latestVersion: undefined,
    revision: 0,
    stale: false,
    submissionStatus: undefined,
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

export function syncPlanDocument(state: PlanBoardUiState, plan: string, version: string | undefined): void {
  state.latestPlan = plan;
  state.latestVersion = version;
  const changed = version !== state.baseVersion || plan !== state.basePlan;
  const edited = (state.draft !== null && state.draft !== state.basePlan) || Boolean(state.otherText);
  if (changed && edited) {
    state.stale = true;
    return;
  }
  if (changed) {
    state.revision += 1;
    state.basePlan = plan;
    state.baseVersion = version;
    state.draft = null;
    state.stale = false;
    if (!state.busy) state.submissionStatus = undefined;
  }
}

export function editPlanDraft(state: PlanBoardUiState, value: string): void {
  if (state.draft === value) return;
  state.draft = value;
  state.revision += 1;
}

export function editPlanFeedback(state: PlanBoardUiState, value: string): void {
  if (state.otherText === value) return;
  state.otherText = value;
  state.revision += 1;
}

export function adoptCurrentPlan(state: PlanBoardUiState): void {
  state.basePlan = state.latestPlan;
  state.baseVersion = state.latestVersion;
  state.draft = null;
  state.revision += 1;
  state.stale = false;
  state.submissionStatus = undefined;
}
