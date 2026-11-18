/**
 * Gateway 单调序号：断线重连 replay 定位 + 客户端去重。
 *
 * 与 web/useChat.ts 的 lastGatewaySequenceRef 对齐；后端 connections.push_payload
 * 写入 gateway_sequence，subscribe 时携带 last_gateway_sequences 回放缺失帧。
 */

import type { ChatChunk, GatewayCursor } from '../api/transport';
import { logStream } from '../stream-debug';
import { parseDeltaParts, parseDeltaRange } from '../stream-reassembly';

const lastSeqBySession = new Map<string, number>();
const seenSeqBySession = new Map<string, Set<number>>();
const epochBySession = new Map<string, string>();
const resyncCutBySession = new Map<string, GatewayCursor>();
const retiredEpochsBySession = new Map<string, Set<string>>();
const MAX_RETIRED_EPOCHS = 8;

export function getGatewayResyncCut(sessionId: string): Readonly<GatewayCursor> | undefined {
  return resyncCutBySession.get(sessionId);
}

function epochOf(chunk: ChatChunk): string | undefined {
  return typeof chunk.gateway_epoch === 'string' && chunk.gateway_epoch.trim()
    ? chunk.gateway_epoch : undefined;
}

/** Install an authoritative cut before consuming any live frames after a resync. */
export function acceptGatewayResync(sessionId: string, chunk: ChatChunk, allowSameCut = false): boolean {
  const epoch = epochOf(chunk);
  const sequence = chunk.gateway_sequence;
  if (!epoch || typeof sequence !== 'number' || !Number.isSafeInteger(sequence) || sequence < 0) return false;
  if (retiredEpochsBySession.get(sessionId)?.has(epoch)) return false;
  const cut = getGatewayResyncCut(sessionId);
  if (cut?.epoch === epoch && (sequence < cut.sequence || (!allowSameCut && sequence === cut.sequence))) {
    return false;
  }
  const previousEpoch = epochBySession.get(sessionId);
  if (previousEpoch && previousEpoch !== epoch) {
    const retired = retiredEpochsBySession.get(sessionId) ?? new Set<string>();
    retired.add(previousEpoch);
    if (retired.size > MAX_RETIRED_EPOCHS) retired.delete(retired.values().next().value!);
    retiredEpochsBySession.set(sessionId, retired);
  }
  epochBySession.set(sessionId, epoch);
  resyncCutBySession.set(sessionId, { epoch, sequence });
  lastSeqBySession.set(sessionId, sequence);
  seenSeqBySession.delete(sessionId);
  clearStreamActivity(sessionId);
  return true;
}

/** A post-cut aggregate may still carry source frames already included in the snapshot. */
export function trimGatewayDeltaAtCut(sessionId: string, chunk: ChatChunk): ChatChunk | null {
  const cut = getGatewayResyncCut(sessionId);
  if (chunk.kind !== 'delta' || !cut || epochOf(chunk) !== cut.epoch || !validSequence(chunk.gateway_sequence)) return chunk;
  if (chunk.gateway_sequence <= cut.sequence) return null;
  const parts = exactDeltaParts(chunk, chunk.gateway_sequence);
  if (!parts || parts[0]![0] > cut.sequence || typeof chunk.body.text !== 'string') return chunk;

  let start = 0;
  let trim = 0;
  while (start < parts.length && parts[start]![0] <= cut.sequence) {
    trim += parts[start]![1];
    start += 1;
  }
  const body: ChatChunk['body'] = {
    ...chunk.body,
    text: chunk.body.text.slice(trim),
    delta_parts: parts.slice(start),
  };
  delete body.delta_start;
  delete body.delta_end;
  return { ...chunk, body };
}

function validSequence(sequence: unknown): sequence is number {
  return typeof sequence === 'number' && Number.isSafeInteger(sequence) && sequence > 0;
}

function exactDeltaParts(chunk: ChatChunk, sequence: number) {
  return chunk.kind === 'delta' && typeof chunk.body?.text === 'string' && chunk.body.delta_parts !== undefined
    ? parseDeltaParts(sequence, chunk.body.text, chunk.body.delta_parts) : null;
}

export function getGatewayCursors(sessionIds: string[]): Record<string, GatewayCursor> {
  const cursors = new Map<string, GatewayCursor>();
  for (const sessionId of sessionIds) {
    const epoch = epochBySession.get(sessionId);
    const sequence = lastSeqBySession.get(sessionId);
    if (epoch && sequence !== undefined) cursors.set(sessionId, { epoch, sequence });
  }
  return Object.fromEntries(cursors);
}

/** 记录已处理的 gateway_sequence：最大值用于重连水位，seen 集合用于实时去重。 */
export function noteGatewaySequence(sessionId: string, chunk: ChatChunk): void {
  const seq = chunk.gateway_sequence;
  if (!validSequence(seq)) return;
  const epoch = epochOf(chunk);
  const currentEpoch = epochBySession.get(sessionId);
  if (epoch && epoch !== currentEpoch) {
    if (currentEpoch) return;
    resetGatewaySequence(sessionId);
    epochBySession.set(sessionId, epoch);
  }
  const seen = seenGatewaySequencesFor(sessionId);
  seen.add(seq);
  for (const [partSequence] of exactDeltaParts(chunk, seq) ?? []) seen.add(partSequence);
  const prev = lastSeqBySession.get(sessionId) ?? 0;
  trimSeenGatewaySequences(seen, Math.max(seq, prev));
  if (seq > prev) {
    lastSeqBySession.set(sessionId, seq);
  }
}

/**
 * 是否应丢弃该帧：只丢弃已经处理过的相同序号。
 * 较小序号的后到有效帧必须放行，否则 task progress 先到时会误丢 tool / todo_updated。
 * 无 gateway_sequence 的旧帧仍放行（向后兼容）。
 */
export function isDuplicateGatewayChunk(sessionId: string, chunk: ChatChunk): boolean {
  const seq = chunk.gateway_sequence;
  if (!validSequence(seq)) return false;
  const epoch = epochOf(chunk);
  const currentEpoch = epochBySession.get(sessionId);
  if (epoch && epoch !== currentEpoch) return Boolean(currentEpoch);
  const cut = getGatewayResyncCut(sessionId);
  if (cut && epoch === cut.epoch && seq <= cut.sequence) return true;
  const seen = seenGatewaySequencesFor(sessionId);
  if (chunk.kind === 'delta' && chunk.body && chunk.body.delta_parts === undefined && seen.has(seq)) {
    const range = parseDeltaRange(chunk.body, chunk.sequence);
    // An opaque aggregate can contain unseen members even when its final frame was seen.
    if (range && range.start < range.end) return false;
  }
  const duplicate = chunk.kind === 'delta' && chunk.body?.delta_parts !== undefined
    ? exactDeltaParts(chunk, seq)?.every(([partSequence]) =>
      seen.has(partSequence) || (cut && cut.epoch === epoch && partSequence <= cut.sequence)) === true
    : seen.has(seq);
  if (duplicate) {
    logStream('gateway-seq', 'drop-duplicate', { sessionId, seq, last: lastSeqBySession.get(sessionId) ?? 0, kind: chunk.kind });
    return true;
  }
  return false;
}

/** subscribe/resume 时上报各 session 最后收到的序号。 */
export function getLastGatewaySequences(sessionIds: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const sid of sessionIds) {
    const seq = lastSeqBySession.get(sid);
    if (seq != null && seq > 0) out[sid] = seq;
  }
  return out;
}

/** Modern cursors belong to the session for the lifetime of the gateway epoch. */
export function resetLegacyGatewaySequence(sessionId: string): void {
  if (!epochBySession.has(sessionId)) resetGatewaySequence(sessionId);
}

/** Clear a session's cursor and deduplication state. */
export function resetGatewaySequence(sessionId: string): void {
  lastSeqBySession.delete(sessionId);
  seenSeqBySession.delete(sessionId);
  epochBySession.delete(sessionId);
  resyncCutBySession.delete(sessionId);
  retiredEpochsBySession.delete(sessionId);
}

/** 单测 / 诊断：读取当前序号。 */
export function peekGatewaySequence(sessionId: string): number {
  return lastSeqBySession.get(sessionId) ?? 0;
}

function seenGatewaySequencesFor(sessionId: string): Set<number> {
  let seen = seenSeqBySession.get(sessionId);
  if (!seen) {
    seen = new Set<number>();
    seenSeqBySession.set(sessionId, seen);
  }
  return seen;
}

function trimSeenGatewaySequences(seen: Set<number>, latestSequence: number): void {
  if (seen.size <= 2000) return;
  const keepFrom = latestSequence - 1000;
  for (const sequence of seen) {
    if (sequence < keepFrom) seen.delete(sequence);
  }
}

const lastActivityAt = new Map<string, number>();

/** 收到有效 chunk 后更新活跃时间戳（watchdog 用）。 */
export function touchStreamActivity(sessionId: string, atMs: number = Date.now()): void {
  lastActivityAt.set(sessionId, atMs);
}

export function getLastStreamActivity(sessionId: string): number | undefined {
  return lastActivityAt.get(sessionId);
}

export function clearStreamActivity(sessionId: string): void {
  lastActivityAt.delete(sessionId);
}
