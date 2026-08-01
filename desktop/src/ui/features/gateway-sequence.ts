/**
 * Gateway 单调序号：断线重连 replay 定位 + 客户端去重。
 *
 * 与 web/useChat.ts 的 lastGatewaySequenceRef 对齐；后端 connections.push_payload
 * 写入 gateway_sequence，subscribe 时携带 last_gateway_sequences 回放缺失帧。
 */

import type { ChatChunk } from '../backend-client';
import { logStream } from '../stream-debug';

const lastSeqBySession = new Map<string, number>();
const seenSeqBySession = new Map<string, Set<number>>();

/** 记录已处理的 gateway_sequence：最大值用于重连水位，seen 集合用于实时去重。 */
export function noteGatewaySequence(sessionId: string, chunk: ChatChunk): void {
  const seq = chunk.gateway_sequence;
  if (typeof seq !== 'number' || seq <= 0) return;
  const seen = seenGatewaySequencesFor(sessionId);
  seen.add(seq);
  trimSeenGatewaySequences(seen, seq);
  const prev = lastSeqBySession.get(sessionId) ?? 0;
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
  if (typeof seq !== 'number' || seq <= 0) return false;
  const seen = seenGatewaySequencesFor(sessionId);
  if (seen.has(seq)) {
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

/** 新回合开始时重置（与后端 clear_buffer 对齐）。 */
export function resetGatewaySequence(sessionId: string): void {
  lastSeqBySession.delete(sessionId);
  seenSeqBySession.delete(sessionId);
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
