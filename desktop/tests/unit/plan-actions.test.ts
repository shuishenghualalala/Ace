/** @vitest-environment happy-dom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi, type ChatChunk } from '../../src/ui/backend-client';
import {
  disconnectPlanReceipts, queryPlanAction, receivePlanCapability, receivePlanReceipt, reconnectPlanActions,
  resetPlanActions, submitLegacyPlanAction, submitPlanAction, supportsPlanReceipts, type PlanActionInput,
} from '../../src/ui/features/plan-actions';
import { chatOutbox } from '../../src/ui/features/chat-outbox';
import { state } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore } from '../../src/ui/stores/stores';

const input: PlanActionInput = {
  action: 'plan_approve', sessionId: 'plan-session', expectedVersion: 'version-one',
  plan: '# Plan', mode: 'agent', workspaceId: 'workspace-one',
};
const snapshot = {
  session_id: input.sessionId, version: 'version-two', active: false, awaiting_approval: false,
  has_plan: true, plan: input.plan!, plan_file: 'plans/plan.md', status: 'approved', phase: 'approved',
};
let send: ReturnType<typeof vi.fn>;
const capability = (enabled = true): ChatChunk => ({
  kind: 'send_receipt_capability', sequence: 0, is_final: false,
  body: { version: 1, store_id: 'a'.repeat(32), ...(enabled ? { plan_receipts: 1 } : {}) },
});
function receipt(status: string, body: Record<string, unknown> = {}): ChatChunk {
  const command = send.mock.calls.find(([frame]) => frame.action === input.action)?.[0];
  return {
    kind: 'interaction_receipt', session_id: input.sessionId, request_id: command.request_id, sequence: 0, is_final: false,
    body: { type: 'plan', submission_id: command.submission_id, status, ...body },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  __resetAllStoresForTest();
  resetPlanActions();
  authStore.set({ isLoggedIn: true, userInfo: { staffCode: 'owner-one' } });
  send = vi.fn(async () => true);
  state.socket = {
    send, planApprove: vi.fn(async () => true), planUpdate: vi.fn(async () => true),
    planReject: vi.fn(async () => true), planRejectAndExit: vi.fn(async () => true),
  } as unknown as typeof state.socket;
  receivePlanCapability(capability());
});
afterEach(() => {
  resetPlanActions();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('versioned Plan decisions', () => {
  it('requires the explicit Plan feature flag and preserves original protocol fallback', () => {
    receivePlanCapability(capability(false));
    expect(supportsPlanReceipts('version-one')).toBe(false);
    receivePlanCapability(capability());
    expect(supportsPlanReceipts('version-one')).toBe(true);
    expect(supportsPlanReceipts(undefined)).toBe(false);
  });

  it('submits once with captured version and confirms only an exact receipt', () => {
    const notify = vi.fn();
    submitPlanAction(input, notify);
    submitPlanAction(input, notify);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({
      action: 'plan_approve', expected_version: 'version-one', plan: '# Plan',
      session_id: input.sessionId, submission_id: expect.any(String), request_id: expect.any(String),
    }));
    const frame = receipt('applied', { plan_state: snapshot, current_version: 'version-two', execution_request_id: 'exec-one' });
    receivePlanReceipt({ ...frame, request_id: 'wrong-request' });
    expect(notify).toHaveBeenLastCalledWith({ status: 'confirming' });
    receivePlanReceipt(frame);
    expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({
      status: 'applied', planState: snapshot, currentVersion: 'version-two', executionRequestId: 'exec-one',
    }));
    receivePlanReceipt(frame);
    expect(notify.mock.calls.filter(([notice]) => notice.status === 'applied')).toHaveLength(1);
  });

  it('queries a lost acknowledgement and only repeats an explicitly unknown command', async () => {
    const notify = vi.fn();
    submitPlanAction(input, notify);
    const command = send.mock.calls[0]![0];
    await vi.advanceTimersByTimeAsync(2000);
    expect(send).toHaveBeenLastCalledWith({
      action: 'plan_receipt_query', session_id: command.session_id,
      expected_version: command.expected_version, request_id: command.request_id, submission_id: command.submission_id,
    });
    receivePlanReceipt(receipt('unknown'));
    expect(send.mock.calls.filter(([frame]) => frame.action === input.action)).toHaveLength(1);
    submitPlanAction(input, notify);
    expect(send).toHaveBeenLastCalledWith(command);
  });

  it('retains stale plan state and does not turn expiry into an execution', () => {
    const notify = vi.fn();
    submitPlanAction(input, notify);
    receivePlanReceipt(receipt('expired', { code: 'stale_plan', current_version: 'version-two', plan_state: snapshot }));
    expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'expired', code: 'stale_plan', planState: snapshot }));
    expect(send.mock.calls.filter(([frame]) => frame.action === input.action)).toHaveLength(1);
  });

  it('does not query or repeat a captured decision against a replacement backing store', () => {
    const identity = vi.spyOn(chatOutbox, 'transportIdentity').mockReturnValue({ scope: 'owner', storeId: 'a'.repeat(32) });
    const notify = vi.fn();
    submitPlanAction(input, notify);
    identity.mockReturnValue({ scope: 'owner', storeId: 'b'.repeat(32) });
    reconnectPlanActions();
    expect(send).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenLastCalledWith({ status: 'uncertain' });
  });

  it('does not replay after the new connection omits Plan receipt support', () => {
    const notify = vi.fn();
    submitPlanAction(input, notify);
    disconnectPlanReceipts();
    receivePlanCapability(capability(false));
    reconnectPlanActions();
    expect(send).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenLastCalledWith({ status: 'uncertain' });
  });

  it('keeps an unfulfilled send bounded and provides status-only rechecking', async () => {
    send.mockImplementation(() => new Promise(() => {}));
    const notify = vi.fn();
    submitPlanAction(input, notify);
    await vi.advanceTimersByTimeAsync(4001);
    expect(notify).toHaveBeenLastCalledWith({ status: 'uncertain' });
    queryPlanAction(input.sessionId);
    expect(send.mock.calls.filter(([frame]) => frame.action === input.action)).toHaveLength(1);
  });

  it('never sends a versioned draft through a legacy action when negotiation is delayed', async () => {
    disconnectPlanReceipts();
    const notify = vi.fn();
    submitPlanAction(input, notify);
    await submitLegacyPlanAction(input, vi.fn(), notify);
    expect(notify).toHaveBeenLastCalledWith({ status: 'unavailable' });
    expect(state.socket?.planApprove).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    receivePlanCapability(capability());
    submitPlanAction(input, notify);
    expect(send).toHaveBeenCalledWith(expect.objectContaining({ expected_version: input.expectedVersion }));
  });

  const legacy = { ...input, expectedVersion: undefined };
  const review = { ...snapshot, version: undefined, active: true, phase: 'review', status: 'pending' };
  it('requires the authoritative legacy plan state and the submitted execution request', async () => {
    receivePlanCapability(capability(false));
    vi.spyOn(backendApi, 'sessionPlan').mockResolvedValueOnce(review).mockResolvedValue({ ...snapshot, version: undefined });
    vi.spyOn(backendApi, 'sessionStatus').mockImplementation(async () => ({
      session_id: input.sessionId, live: 'running', queue_depth: 0, last_status: '', last_error: '',
      active_request_id: vi.mocked(state.socket!.planApprove).mock.calls[0]?.[3],
    }));
    const notify = vi.fn();
    await submitLegacyPlanAction(legacy, vi.fn(), notify);
    expect(state.socket?.planApprove).toHaveBeenCalledOnce();
    expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'confirmed' }));
    expect(notify.mock.lastCall?.[0]).not.toHaveProperty('executionRequestId');
  });

  it('refreshes an unversioned card when the current server returns a version', async () => {
    disconnectPlanReceipts();
    vi.spyOn(backendApi, 'sessionPlan').mockResolvedValue(snapshot);
    const notify = vi.fn();
    await submitLegacyPlanAction(legacy, vi.fn(), notify);
    expect(notify).toHaveBeenLastCalledWith({ status: 'expired', planState: snapshot });
    expect(state.socket?.planApprove).not.toHaveBeenCalled();
  });

  it('keeps failed feedback unconfirmed even after the legacy state becomes revising', async () => {
    receivePlanCapability(capability(false));
    const revised = { ...review, phase: 'revising', status: 'revising' };
    vi.spyOn(backendApi, 'sessionPlan').mockResolvedValueOnce(review).mockResolvedValue(revised);
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      session_id: input.sessionId, live: 'idle', queue_depth: 0, last_status: '', last_error: '',
    });
    const history = vi.spyOn(backendApi, 'historyWindow').mockResolvedValue({ items: [], has_more: false, next_before: null });
    const notify = vi.fn();
    const feedback = vi.fn(async (_requestId: string) => false);
    await submitLegacyPlanAction({ ...legacy, action: 'plan_reject', feedback: 'actual feedback' }, feedback, notify);
    expect(notify).toHaveBeenLastCalledWith({ status: 'unavailable' });
    queryPlanAction(input.sessionId);
    await vi.advanceTimersByTimeAsync(2000);
    expect(notify).toHaveBeenLastCalledWith({ status: 'uncertain' });
    history.mockResolvedValue({ items: [{ role: 'user', content: 'actual feedback', request_id: feedback.mock.calls[0][0] }], has_more: false, next_before: null });
    queryPlanAction(input.sessionId);
    await vi.advanceTimersByTimeAsync(1);
    expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'confirmed' }));
    expect(feedback).toHaveBeenCalledTimes(1);
  });
});
