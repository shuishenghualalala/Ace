/**
 * 会话 busy / 后端 live 状态同步。
 *
 * 从 chat-controller 抽出，避免 index.ts 等与 chat-controller 形成循环依赖。
 * Composer 自行订阅会话状态；此处只刷新其余依赖 busy 的界面。
 */

import { resetGatewaySequence } from './gateway-sequence';
import {
  appendSessionMessage,
  ensureSessionBook,
  newMessageId,
  patchBook,
  setBusy,
  setSessionStatus,
  type SessionStatus,
} from '../state';
import { messageStore } from '../stores/stores';
import { syncRunningIntroSlot } from './running-intro';
import { sessionDisplayModelLabel, syncSessionModelAvailabilityUi } from './session-model';

export function newTurnRequestId(): string {
  const uuid = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
    : `${Date.now().toString(36)}`;
  return `req_${Date.now().toString(36)}_${uuid}`;
}

/** 写入 busy 并刷新依赖会话状态的界面。
 *  状态隔离：仅当 busy 真正变化时才同步 model / running-intro，避免流式期间
 *  高频重复调用造成无谓 UI 抖动。 */
export function applyBusyUi(sessionId: string, busy: boolean): void {
  const changed = setBusy(sessionId, busy);
  if (!changed) return;
  if (typeof document === 'undefined') return;
  syncSessionModelAvailabilityUi();
  syncRunningIntroSlot();
}

/**
 * 本地发送新 query 前打开回合：写入 request_id、清空上轮记账，并挂上乐观 assistant。
 *
 * 必须在 WS send 之前调用，避免首帧 delta/task 早于 patchBook 被 gate 丢弃。
 * 乐观 assistant 让「正在思考 · 已等待」从发送瞬间起算；首包 reducer 复用同一
 * assistantId patch，不新建气泡，也不覆盖 turnStartedAt。firstChunkAt 仍留空，
 * 由真实首包写入以保留 TTFT 指标语义。
 */
export function openTurnForRequest(sessionId: string, requestId: string): void {
  resetGatewaySequence(sessionId);
  ensureSessionBook(sessionId);
  const now = Date.now();
  const assistantId = newMessageId('m');
  appendSessionMessage(sessionId, {
    id: assistantId,
    role: 'assistant',
    content: '',
    timestamp: now,
    streaming: true,
    // 工具前不确定阶段：与 deltaReducer 首段一致，避免旁白触发「正式正文」自动折。
    segmentRole: 'process',
    turnStartedAt: now,
    model: sessionDisplayModelLabel(sessionId),
  });
  // 不清 pendingPlan：批准后的方案正文需留在 Plan Board；新 plan_review / 退出 Plan 再替换或清空。
  patchBook(sessionId, {
    turnSealed: false,
    activeRequestId: requestId,
    acceptingNewRequest: false,
    assistantId,
    firstChunkAt: null,
    hadTeamInternal: false,
    toolMap: new Map(),
    deltaSpans: [],
    legacyDeltaText: '',
  });
}

/**
 * WS send 失败时丢弃仍为空的乐观 assistant，避免留下「正在思考」僵尸气泡。
 * 若已有正文/thinking/工具，说明首包已到，交给正常 error/finalize 路径，不在此删除。
 */
export function discardEmptyOptimisticAssistant(sessionId: string): void {
  const book = ensureSessionBook(sessionId);
  const assistantId = book.assistantId;
  if (!assistantId) return;
  const messages = messageStore.get().messages[sessionId] ?? [];
  const msg = messages.find((m) => m.id === assistantId);
  if (!msg || msg.role !== 'assistant') return;
  const empty =
    !msg.content?.trim()
    && !msg.thinking?.trim()
    && !(msg.toolCalls && msg.toolCalls.length > 0);
  if (!empty) return;
  messageStore.set({
    messages: {
      ...messageStore.get().messages,
      [sessionId]: messages.filter((m) => m.id !== assistantId),
    },
  });
  patchBook(sessionId, {
    assistantId: null,
    firstChunkAt: null,
    toolMap: new Map(),
    deltaSpans: [],
    legacyDeltaText: '',
    turnSealed: true,
    acceptingNewRequest: false,
  });
}

/** 用户提交追问 / 澄清 / 批准 plan 后：乐观恢复「生成中」，直到首帧 streaming 到达。
 *
 * 与 openTurnForRequest 同构：若当前无 live streaming assistant，挂一条 process 占位，
 * 使批准/追问路径也有「正在思考」反馈；保留 pendingPlan 供 Plan Board。
 */
export function resumeSessionGeneration(sessionId: string, requestId: string | null = null): void {
  ensureSessionBook(sessionId);
  const book = ensureSessionBook(sessionId);
  const activeRequestId = requestId ?? book.activeRequestId;
  const messages = messageStore.get().messages[sessionId] ?? [];
  const current = book.assistantId
    ? messages.find((m) => m.id === book.assistantId)
    : undefined;
  const hasLiveAssistant = Boolean(current?.streaming && book.assistantId);

  if (!hasLiveAssistant) {
    const now = Date.now();
    const assistantId = newMessageId('m');
    appendSessionMessage(sessionId, {
      id: assistantId,
      role: 'assistant',
      content: '',
      timestamp: now,
      streaming: true,
      segmentRole: 'process',
      turnStartedAt: now,
      model: sessionDisplayModelLabel(sessionId),
    });
    patchBook(sessionId, {
      turnSealed: false,
      activeRequestId,
      acceptingNewRequest: activeRequestId === null,
      assistantId,
      firstChunkAt: null,
      hadTeamInternal: false,
      toolMap: new Map(),
      deltaSpans: [],
      legacyDeltaText: '',
    });
  } else {
    patchBook(sessionId, {
      turnSealed: false,
      activeRequestId,
      acceptingNewRequest: activeRequestId === null,
      hadTeamInternal: false,
    });
  }
  applyBusyUi(sessionId, true);
  setSessionStatus(sessionId, 'running');
}

/**
 * 「后端已 idle」回调：由 chat-controller 注册，用于兜底消费本地待发队列。
 *
 * 正常路径下本地队列在收到 final 帧时（finalizeTurn → consumePending）派出；
 * 若 final 帧丢失（登出围栏吞帧、重连后 request_id 门拦丢弃），后端已空闲而
 * 本地仍显示「正在排队…」。状态同步拉到 live=idle 是权威信号，借此触发兜底。
 */
let backendIdleHandler: ((sessionId: string) => void) | null = null;

export function setBackendIdleHandler(fn: ((sessionId: string) => void) | null): void {
  backendIdleHandler = fn;
}

/**
 * 用后端 session status API 的 live 字段对齐 busy + turnSealed。
 * 用于历史回填、重连，避免 status=running 但 busy=false 的假 idle。
 */
export function syncSessionLiveFromBackend(
  sessionId: string,
  live: string | undefined,
  lastStatus?: string,
  activeRequestId?: string | null,
): void {
  const book = ensureSessionBook(sessionId);
  if (live === 'running' || live === 'queued') {
    const isSameSealedTurn =
      book.turnSealed
      && !!book.activeRequestId
      && activeRequestId === book.activeRequestId;
    const isWaitingForUser =
      !!book.pendingFollowup
      || book.pendingPlan?.status === 'pending';

    if (isSameSealedTurn || isWaitingForUser) {
      patchBook(sessionId, { turnSealed: true, acceptingNewRequest: false });
      applyBusyUi(sessionId, false);
      setSessionStatus(sessionId, lastStatus === 'failed' ? 'error' : 'idle');
      return;
    }

    const localInFlight = !book.turnSealed && Boolean(book.activeRequestId);
    const nextRequestId = localInFlight
      ? book.activeRequestId
      : (activeRequestId ?? (book.turnSealed ? null : book.activeRequestId));
    patchBook(sessionId, {
      turnSealed: false,
      activeRequestId: nextRequestId,
      acceptingNewRequest: nextRequestId === null,
    });
    applyBusyUi(sessionId, true);
    setSessionStatus(sessionId, live as SessionStatus);
    return;
  }
  patchBook(sessionId, { turnSealed: true, acceptingNewRequest: false });
  applyBusyUi(sessionId, false);
  const st: SessionStatus = lastStatus === 'failed' ? 'error' : 'idle';
  setSessionStatus(sessionId, st);
  backendIdleHandler?.(sessionId);
}
