/**
 * 后端历史与本地消息的增量合并（避免 running 时全量 replace 抹掉流式正文）。
 */

import type { ChatMessage } from '../chat-render';

export interface HistoryMergeOptions {
  live?: string;
  /** 本地是否存在 streaming 助手消息或在途回合 */
  preserveLocalTail?: boolean;
}

function sameMessageForHistoryPrefix(a: ChatMessage, b: ChatMessage): boolean {
  return (
    a.role === b.role
    && a.content === b.content
    && (a.thinking ?? '') === (b.thinking ?? '')
    && (a.segmentRole ?? '') === (b.segmentRole ?? '')
  );
}

function commonPrefixLength(local: ChatMessage[], remote: ChatMessage[]): number {
  const max = Math.min(local.length, remote.length);
  let i = 0;
  while (i < max && sameMessageForHistoryPrefix(local[i]!, remote[i]!)) i += 1;
  return i;
}

/**
 * 将后端历史与本地消息合并。
 * - idle/已结束：以后端为准全量替换。
 * - running/queued 且本地有在途内容：后端历史作基底，按共同前缀保留本地在途尾部。
 */
export function mergeBackendHistory(
  local: ChatMessage[],
  remote: ChatMessage[],
  opts: HistoryMergeOptions,
): ChatMessage[] {
  const live = opts.live ?? 'idle';
  const inFlight = live === 'running' || live === 'queued';
  const hasStreamingTail = local.some((m) => m.role === 'assistant' && m.streaming);
  const hasLocalTail = hasStreamingTail || Boolean(opts.preserveLocalTail);

  if (!inFlight || !hasLocalTail) {
    return remote;
  }

  if (local.length === 0) return remote;
  if (remote.length === 0) return local;

  const prefix = commonPrefixLength(local, remote);
  const localTail = local.slice(prefix);
  if (localTail.length === 0) return remote;

  // 后端历史可能已经补齐前缀外的一部分，避免把同内容消息重复追加。
  const remoteTail = remote.slice(prefix);
  const dedupedLocalTail = localTail.filter(
    (localMsg) => !remoteTail.some((remoteMsg) => sameMessageForHistoryPrefix(localMsg, remoteMsg)),
  );
  return [...remote, ...dedupedLocalTail];
}

/**
 * 触顶翻页前缀拼接（P1-6）：把更早的一页 older 头部拼到 current 之前。
 *
 * 窗口端点的互斥游标保证两页不重叠；这里只做兜底——历史在两次翻页之间被
 * rewind/fork 重写时，older 尾部可能与 current 头部重复，按「older 尾部 vs
 * current 头部」的同内容连续段裁掉重叠（正常翻页该长度恒为 0，零成本）。
 */
export function prependHistoryWindow(current: ChatMessage[], older: ChatMessage[]): ChatMessage[] {
  if (older.length === 0) return current;
  if (current.length === 0) return older;
  // 找最大 k 使 older 的后 k 条与 current 的前 k 条逐位同内容（正常翻页 k=0，零成本）
  let overlap = 0;
  for (let k = Math.min(older.length, current.length); k > 0; k -= 1) {
    let matched = true;
    for (let j = 0; j < k; j += 1) {
      if (!sameMessageForHistoryPrefix(older[older.length - k + j]!, current[j]!)) {
        matched = false;
        break;
      }
    }
    if (matched) {
      overlap = k;
      break;
    }
  }
  if (overlap === 0) return [...older, ...current];
  return [...older.slice(0, older.length - overlap), ...current];
}
