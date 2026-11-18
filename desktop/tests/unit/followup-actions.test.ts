/** @vitest-environment happy-dom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import './helpers/mock-chat-controller-deps';
import {
  _resetTurnDurationTickerForTests, applyChunk, applyFollowupReceipt, respondToFollowup,
} from '../../src/ui/features/chat-controller';
import {
  disconnectFollowupReceipts, followupCardView, receiveFollowupCapability, reconnectFollowupDecisions,
  resetFollowupDecisions, submitFollowupDecision,
} from '../../src/ui/features/followup-actions';
import { bindFollowupCard, renderFollowupCardElement } from '../../src/ui/followup';
import {
  appendSessionMessage, ensureSessionBook, patchBook, setActiveSessionId, state, type PendingFollowup,
} from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, messageStore, sessionStore } from '../../src/ui/stores/stores';
import { chatOutbox } from '../../src/ui/features/chat-outbox';
import type { ChatChunk } from '../../src/ui/backend-client';

vi.mock('../../src/ui/features/workspaces', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/ui/features/workspaces')>(),
  refreshAllSessions: vi.fn(async () => undefined),
}));
vi.mock('../../src/ui/features/channel-sessions', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/ui/features/channel-sessions')>(),
  loadChannelSessions: vi.fn(async () => undefined),
}));

const sid = 'followup-session';
const requestId = 'request-followup';
const answers = [{ question_id: 'detail', answers: ['中文详细回答'] }];
let send = vi.fn<(value: unknown, scope?: string, storeId?: string) => Promise<boolean>>();

function pending(recordHistory = true): PendingFollowup {
  return {
    questionId: 'question-one', requestId, title: '补充说明', recordHistory,
    questions: [{
      id: 'detail', question: '请说明', inputMode: 'text', options: [],
      multiSelect: false, allowFreeText: true,
    }],
  };
}

function frame(kind: ChatChunk['kind'], body: Record<string, unknown>, request = requestId): ChatChunk {
  return { kind, body, session_id: sid, request_id: request, sequence: 0, is_final: false };
}

function emitReceipt(status: string, overrides: Record<string, unknown> = {}): void {
  const submission = send.mock.calls.find(([value]) => (value as { action?: string }).action === 'followup_answer')?.[0] as { submission_id: string } | undefined;
  expect(submission).toBeDefined();
  applyFollowupReceipt(frame('interaction_receipt', {
    type: 'followup', interaction_id: 'question-one', submission_id: submission!.submission_id,
    status, ...overrides,
  }));
}

function installQuestion(question = pending()): void {
  ensureSessionBook(sid);
  patchBook(sid, {
    activeRequestId: requestId, acceptingNewRequest: false, turnSealed: false, pendingFollowup: question,
  });
}

function mount(question = pending()): HTMLElement {
  const root = renderFollowupCardElement(question);
  document.body.append(root);
  bindFollowupCard(root, {
    onSubmit: (id, value) => respondToFollowup(sid, id, value),
    onCancel: (id) => respondToFollowup(sid, id),
  }, followupCardView(sid, question));
  return root;
}

beforeEach(() => {
  vi.useFakeTimers();
  __resetAllStoresForTest();
  resetFollowupDecisions();
  receiveFollowupCapability(frame('send_receipt_capability', { version: 1, followup_receipts: 1 }));
  authStore.set({ isLoggedIn: true, userInfo: { staffCode: 'owner-a' } });
  send = vi.fn(async () => true);
  state.socket = { send } as unknown as typeof state.socket;
  setActiveSessionId(sid);
  document.body.innerHTML = '<div id="chat-messages"></div>';
});

afterEach(() => {
  resetFollowupDecisions();
  _resetTurnDurationTickerForTests();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('followup confirmed submission', () => {
  it('retains a pending card and creates no user message on transport success', async () => {
    const question = pending();
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    await Promise.resolve();
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      action: 'followup_answer', session_id: sid, request_id: requestId, question_id: question.questionId,
      submission_id: expect.any(String), answers,
    }));
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBe(question);
    expect(messageStore.get().messages[sid] ?? []).toEqual([]);
    expect(followupCardView(sid, question)?.status).toBe('confirming');
  });

  it('projects one canonical answer before continuation and ignores the later direct receipt', () => {
    const question = pending();
    installQuestion(question);
    appendSessionMessage(sid, {
      id: 'before-answer', role: 'assistant', content: 'First half', streaming: false, timestamp: 1,
    });
    patchBook(sid, { assistantId: 'before-answer' });
    respondToFollowup(sid, question.questionId, answers);
    const resolution = frame('followup_question', {
      question_id: question.questionId, status: 'resolved',
      answer_history_id: 'followup:question-one', answer_text: '已选择：中文详细回答',
    });
    applyChunk(resolution);
    emitReceipt('applied', { answer_history_id: 'followup:question-one' });
    applyChunk(resolution);
    applyChunk(frame('delta', { text: 'Second half' }));
    const messages = messageStore.get().messages[sid]!;
    expect(messages.filter((message) => message.role === 'user')).toEqual([
      expect.objectContaining({
        id: 'history-followup%3Aquestion-one', historyId: 'followup:question-one',
        content: '已选择：中文详细回答', requestId,
      }),
    ]);
    expect(messages[0]?.content).toBe('First half');
    expect(messages.at(-1)?.content).toBe('Second half');
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
  });

  it('closes a permission only after confirmation and never adds a user answer', () => {
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBe(question);
    emitReceipt('applied');
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
    expect(messageStore.get().messages[sid]?.filter((message) => message.role === 'user') ?? []).toEqual([]);
  });

  it('queries on timeout then allows only explicit unknown retry of the exact decision', async () => {
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    await vi.advanceTimersByTimeAsync(2001);
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({
      action: 'followup_receipt_query', session_id: sid, request_id: requestId, question_id: question.questionId,
    }));
    emitReceipt('unknown');
    expect(followupCardView(sid, question)).toMatchObject({ status: 'unknown', locked: false });
    await vi.advanceTimersByTimeAsync(10000);
    expect(send.mock.calls.filter(([value]) => (value as { action?: string }).action === 'followup_answer')).toHaveLength(1);
    const first = send.mock.calls[0]![0];
    respondToFollowup(sid, question.questionId, answers);
    expect(send.mock.calls.at(-1)?.[0]).toEqual(first);
  });

  it.each(['unavailable', 'uncertain'])('retains and queries %s without repeating permission', async (status) => {
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    emitReceipt(status);
    respondToFollowup(sid, question.questionId, answers);
    reconnectFollowupDecisions();
    await Promise.resolve();
    expect(followupCardView(sid, question)?.locked).toBe(true);
    expect(send.mock.calls.filter(([value]) => (value as { action?: string }).action === 'followup_answer')).toHaveLength(1);
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'followup_receipt_query' }));
  });

  it('ignores wrong request, submission and owner receipts', () => {
    const question = pending();
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    const payload = send.mock.calls[0]![0] as { submission_id: string };
    const receipt = frame('interaction_receipt', {
      type: 'followup', interaction_id: question.questionId, submission_id: payload.submission_id, status: 'applied',
    }, 'other-request');
    applyFollowupReceipt(receipt);
    applyFollowupReceipt({ ...receipt, request_id: requestId, body: { ...receipt.body, submission_id: 'other' } });
    authStore.set({ userInfo: { staffCode: 'owner-b' } });
    applyFollowupReceipt({ ...receipt, request_id: requestId });
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBe(question);
  });

  it('keeps free text and options through remount and expired confirmation', () => {
    const question = pending();
    installQuestion(question);
    const first = mount(question);
    const input = first.querySelector<HTMLTextAreaElement>('textarea')!;
    input.value = '仍然可以复制的完整草稿';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    first.remove();
    const second = mount(question);
    expect(second.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe(input.value);
    second.querySelector<HTMLButtonElement>('.followup-card__submit')!.click();
    emitReceipt('expired');
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
    second.remove();
    const expired = followupCardView(sid);
    expect(expired).toMatchObject({ status: 'expired', locked: true });
    const root = renderFollowupCardElement(expired!.question);
    bindFollowupCard(root, { onSubmit: vi.fn(), onCancel: vi.fn() }, expired);
    expect(root.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe(input.value);
    expect(root.querySelector('[data-followup-submission-status="expired"]')).not.toBeNull();
    expect(root.querySelector<HTMLTextAreaElement>('textarea')?.readOnly).toBe(true);
  });

  it('does not let remounted callbacks submit to the prior active session', () => {
    const question = pending();
    installQuestion(question);
    const root = mount(question);
    const id = 'next-session';
    setActiveSessionId(id);
    const input = root.querySelector<HTMLTextAreaElement>('textarea')!;
    input.value = 'Original session response';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    root.querySelector<HTMLButtonElement>('.followup-card__submit')!.click();
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ session_id: sid }));
  });

  it('does not resend across an account change even when IDs are identical', () => {
    const question = pending();
    submitFollowupDecision(sid, question, answers, vi.fn());
    authStore.set({ userInfo: { staffCode: 'owner-b' } });
    reconnectFollowupDecisions();
    expect(send).toHaveBeenCalledTimes(1);
    expect(followupCardView(sid, question)?.status).toBeUndefined();
  });

  it('uses native scope and store fences and never queries the replacement store', () => {
    const original = { scope: 'owner-scope', storeId: 'a'.repeat(32) };
    const transport = vi.spyOn(chatOutbox, 'transportIdentity').mockReturnValue(original);
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'followup_answer' }),
      original.scope, original.storeId);
    transport.mockReturnValue({ ...original, storeId: 'b'.repeat(32) });
    reconnectFollowupDecisions();
    expect(send).toHaveBeenCalledTimes(1);
    expect(followupCardView(sid, question)).toMatchObject({ status: 'uncertain', locked: true });
  });

  it('retains cancellation until its exact receipt and creates no user text', () => {
    const question = pending();
    installQuestion(question);
    respondToFollowup(sid, question.questionId);
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBe(question);
    const submission = send.mock.calls[0]![0] as { submission_id: string };
    applyFollowupReceipt(frame('interaction_receipt', {
      type: 'followup', interaction_id: question.questionId,
      submission_id: submission.submission_id, status: 'cancelled',
    }));
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
    expect(messageStore.get().messages[sid] ?? []).toEqual([]);
  });

  it('rechecks uncertain status without repeating an answer', () => {
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    emitReceipt('uncertain');
    followupCardView(sid, question)?.recheck();
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'followup_receipt_query' }));
    expect(followupCardView(sid, question)?.status).toBe('confirming');
  });

  it('requires canonical repair when an applied receipt arrives without ordered answer projection', () => {
    const question = pending();
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    const submission = send.mock.calls[0]![0] as { submission_id: string };
    expect(applyFollowupReceipt(frame('interaction_receipt', {
      type: 'followup', interaction_id: question.questionId, submission_id: submission.submission_id,
      status: 'applied', answer_history_id: 'followup:question-one',
    }))).toEqual({ repairSessionId: sid });
    expect(messageStore.get().messages[sid] ?? []).toEqual([]);
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
  });

  it('rebinds mounted cards to the current handler while preserving selected options', () => {
    const question = pending();
    question.questions[0]!.inputMode = 'choice';
    question.questions[0]!.options = [{ label: '一个选项', value: 'option-a' }];
    const root = mount(question);
    const option = root.querySelector<HTMLInputElement>('input[value="option-a"]')!;
    option.checked = true;
    option.dispatchEvent(new Event('change', { bubbles: true }));
    root.remove();
    const reloaded = renderFollowupCardElement(question);
    const latestSubmit = vi.fn();
    bindFollowupCard(reloaded, { onSubmit: vi.fn(), onCancel: vi.fn() }, followupCardView(sid, question));
    bindFollowupCard(reloaded, { onSubmit: latestSubmit, onCancel: vi.fn() }, followupCardView(sid, question));
    expect(reloaded.querySelector<HTMLInputElement>('input[value="option-a"]')?.checked).toBe(true);
    reloaded.querySelector<HTMLButtonElement>('.followup-card__submit')!.click();
    expect(latestSubmit).toHaveBeenCalledWith(question.questionId, [{ question_id: 'detail', answers: ['option-a'] }]);
  });

  it('retains all in-flight decisions beyond the inactive draft cache size', () => {
    for (let index = 0; index < 40; index += 1) {
      submitFollowupDecision('session-' + index, pending(), answers, vi.fn());
    }
    expect(send).toHaveBeenCalledTimes(40);
    expect(followupCardView('session-0', pending())?.status).toBe('confirming');
    expect(followupCardView('session-39', pending())?.status).toBe('confirming');
  });

  it('keeps unresolved text available after the live prompt has disappeared without reoffering its action', () => {
    const question = pending();
    const root = mount(question);
    const input = root.querySelector<HTMLTextAreaElement>('textarea')!;
    input.value = '需要保留的未确认回答';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    submitFollowupDecision(sid, question, answers, vi.fn());
    emitReceipt('uncertain');
    const retained = followupCardView(sid);
    expect(retained).toMatchObject({ status: 'uncertain', locked: true, actionable: false });
    const remounted = renderFollowupCardElement(retained!.question);
    bindFollowupCard(remounted, { onSubmit: vi.fn(), onCancel: vi.fn() }, retained);
    expect(remounted.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe(input.value);
    expect(remounted.querySelector<HTMLButtonElement>('[data-action="submit"]')?.hidden).toBe(true);
    expect(remounted.querySelector<HTMLButtonElement>('[data-followup-recheck]')?.hidden).toBe(false);
  });

  it('reaches query-only uncertain status even if transport acknowledgements never return', async () => {
    send.mockImplementation(() => new Promise(() => {}));
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    await vi.advanceTimersByTimeAsync(4001);
    expect(followupCardView(sid, question)).toMatchObject({ status: 'uncertain', locked: true });
    expect(send.mock.calls.map(([value]) => (value as { action: string }).action))
      .toEqual(['followup_answer', 'followup_receipt_query']);
  });

  it('detaches an uncertain question on its final and keeps the text readonly', () => {
    const question = pending();
    question.questions[0]!.inputMode = 'text';
    installQuestion(question);
    const root = mount(question);
    const input = root.querySelector<HTMLTextAreaElement>('textarea')!;
    input.value = '保留这段已提交的回答';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    respondToFollowup(sid, question.questionId, answers);
    emitReceipt('uncertain');
    applyChunk({ ...frame('final', { text: 'The request has ended.' }), is_final: true });
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(true);
    const retained = followupCardView(sid);
    expect(retained).toMatchObject({ status: 'uncertain', locked: true, actionable: false });
    const card = renderFollowupCardElement(retained!.question);
    bindFollowupCard(card, { onSubmit: vi.fn(), onCancel: vi.fn() }, retained);
    expect(card.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe(input.value);
    expect(card.querySelector<HTMLTextAreaElement>('textarea')?.readOnly).toBe(true);
    const previous = send.mock.calls.length;
    respondToFollowup(sid, question.questionId, answers);
    expect(send).toHaveBeenCalledTimes(previous);
  });

  it('expires a same-request unanswered question when its request times out', () => {
    const question = pending();
    installQuestion(question);
    applyChunk({ ...frame('error', {
      message: 'Timed out', outcome: { status: 'stopped', reason: 'deadline' },
    }), is_final: true });
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
    expect(followupCardView(sid)).toMatchObject({ status: 'expired', locked: true, actionable: false });
  });

  it('preserves a pending question owned by a distinct external request', () => {
    const question = pending();
    question.requestId = 'external-request';
    installQuestion(question);
    applyChunk({ ...frame('final', { text: 'Parent complete.' }), is_final: true });
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBe(question);
  });

  it('uses canonical repair for an applied answer learned after the same request was sealed', () => {
    const question = pending();
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    emitReceipt('uncertain');
    applyChunk({ ...frame('final', { text: 'Complete' }), is_final: true });
    const submission = send.mock.calls[0]![0] as { submission_id: string };
    expect(applyFollowupReceipt(frame('interaction_receipt', {
      type: 'followup', interaction_id: question.questionId, submission_id: submission.submission_id,
      status: 'applied', answer_history_id: 'followup:question-one',
    }))).toEqual({ repairSessionId: sid });
  });

  it('uses legacy decisions without modern metadata when the Gateway advertises only send receipts', async () => {
    receiveFollowupCapability(frame('send_receipt_capability', { version: 1, store_id: 'a'.repeat(32) }));
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    expect(send).toHaveBeenCalledWith({
      action: 'followup_answer', session_id: sid, question_id: question.questionId, answers,
    });
    await vi.advanceTimersByTimeAsync(5000);
    reconnectFollowupDecisions();
    expect(send).toHaveBeenCalledTimes(1);
    expect(followupCardView(sid, question)).toMatchObject({
      status: 'uncertain', locked: true, canRecheck: false,
    });
    applyChunk(frame('followup_question', { question_id: question.questionId, status: 'resolved' }, ''));
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
  });

  it('does not query a previous modern decision until the new socket advertises followup receipts', () => {
    const question = pending(false);
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    disconnectFollowupReceipts();
    receiveFollowupCapability(frame('send_receipt_capability', { version: 1, store_id: 'a'.repeat(32) }));
    reconnectFollowupDecisions();
    expect(send).toHaveBeenCalledTimes(1);
    receiveFollowupCapability(frame('send_receipt_capability', { version: 1, followup_receipts: 1 }));
    reconnectFollowupDecisions();
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'followup_receipt_query' }));
  });

  it('keeps old protocol form until the exact legacy resolved event', () => {
    const question = pending();
    delete question.requestId;
    installQuestion(question);
    respondToFollowup(sid, question.questionId, answers);
    expect(send).toHaveBeenCalledWith({
      action: 'followup_answer', session_id: sid, question_id: question.questionId, answers,
    });
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBe(question);
    applyChunk(frame('followup_question', {
      question_id: 'unrelated', status: 'resolved', answer_text: 'unrelated',
    }, ''));
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBe(question);
    applyChunk(frame('followup_question', {
      question_id: question.questionId, status: 'resolved', answer_text: '已选择：中文详细回答',
    }, ''));
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
  });
});
