/**
 * 流式 delta 按 gateway_sequence 重组 —— 根治「到达顺序 != 生成顺序」导致的正文串位/丢字。
 *
 * 背景：deltaReducer 原先用 `content = cur + text` 按**到达顺序**盲目拼接，隐含「分片到达顺序
 * 恒等于文本生成顺序」的假设。该假设在重连 replay（旧低序号帧在客户端已应用更高序号帧之后才
 * 到达）等场景下被打破，导致正文串位 / 丢字，靠 finalReducer 的兜底覆盖救回（而该覆盖对
 * builtin executor 的多步回合又会丢前言）。
 *
 * 本模块维护「每会话 × 每回合 assistant 消息」的 delta 片段缓冲，按 gateway_sequence（后端
 * push 时分配的会话级单调序号，见 crew/gateway/connections.py::push_payload）**升序**重组正文。
 * 这样无论分片以何种顺序到达，重组结果恒等于正确文本顺序——与到达顺序彻底解耦。
 *
 * 零依赖（不 import state/features），故 state.ts（会话删除）与 features 层（applyChunk /
 * loadBackendHistory）都可 import，无循环。reconstruct 是纯函数，可单测。
 *
 * 生命周期：回合封口（finalizeTurn）/ 用户停止（stopGeneration）/ 历史替换
 * （loadBackendHistory）/ 会话删除（removeSessionState）时清缓冲，防泄漏与串轮。
 * assistantId 每回合唯一（m-{now}-{seq}），跨回合天然隔离。
 */

/** 单回合的 delta 片段缓冲：gateway_sequence → 文本。同一回合内 seq 单调唯一（后端保证）。 */
type FragmentBuffer = Map<number, string>;

/** delta 帧区间（回合内 1-based 帧序号，见 crew/gateway/outbound.py / connections.py 合并逻辑）。 */
export interface DeltaRange {
  start: number;
  end: number;
}

/** 单回合缓冲：文本片段 + 各片段的帧区间（合并帧为 min..max，无区间信息的旧帧缺省）。 */
interface TurnBuffer {
  frags: FragmentBuffer;
  ranges: Map<number, DeltaRange>;
}

/** session → assistantId → 回合缓冲。模块级状态，跨 applyChunk 调用累积。 */
const buffers = new Map<string, Map<string, TurnBuffer>>();

function ensureTurn(sessionId: string, assistantId: string): TurnBuffer {
  let byAid = buffers.get(sessionId);
  if (!byAid) {
    byAid = new Map();
    buffers.set(sessionId, byAid);
  }
  let turn = byAid.get(assistantId);
  if (!turn) {
    turn = { frags: new Map(), ranges: new Map() };
    byAid.set(assistantId, turn);
  }
  return turn;
}

/**
 * 从 delta 帧 body 解析帧区间；无效（缺失/非数/<=0）时返回 null，调用方按无区间旧帧处理。
 * 与 chat-reducer 的 deltaRangeOf 同一有效性规则。
 */
export function parseDeltaRange(
  body: { delta_start?: number | string; delta_end?: number | string },
  sequenceFallback?: number,
): DeltaRange | null {
  const startRaw = body.delta_start ?? sequenceFallback;
  const endRaw = body.delta_end ?? sequenceFallback;
  const start = typeof startRaw === 'number' ? startRaw : Number(startRaw);
  const end = typeof endRaw === 'number' ? endRaw : Number(endRaw);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start <= 0 || end <= 0) return null;
  return { start: Math.min(start, end), end: Math.max(start, end) };
}

/** left 后缀与 right 前缀的最长重叠长度。 */
function suffixPrefixOverlap(left: string, right: string): number {
  const max = Math.min(left.length, right.length);
  for (let k = max; k > 0; k--) {
    if (left.endsWith(right.slice(0, k))) return k;
  }
  return 0;
}

/**
 * 纯函数：把「seq → text」片段按 seq 升序拼接成完整正文。可单测。
 *
 * delta 在一个回合内共享会话级单调 seq（中间夹带的 status/tool 帧占用别的 seq，不进本缓冲），
 * 故「按 seq 升序拼接 delta 片段」恒等于正确文本顺序——即便分片乱序到达、或 seq 非连续。
 *
 * 重复免疫（ranges 提供帧区间时）：live 投递经限流合并（一帧携带多帧文本 + 合并区间），而
 * 重连 replay 回放的是未合并单帧；被合并成员的 gateway_sequence 未在客户端登记，精确序号去重
 * 拦不住，同一文本会以「合并帧 + 单帧」各进一次缓冲。这里按帧区间去重：
 * - 区间已完全被覆盖 → 整段跳过；
 * - 区间头部与已发射部分重叠 → 剥离重复前缀（重叠段文本既是 out 后缀也是本片段前缀）再拼接。
 * 区间不重叠的相邻片段（即便文本巧合相同）照常全量拼接，不误伤合法重复文本。
 */
export function reconstruct(frags: FragmentBuffer, ranges?: Map<number, DeltaRange>): string {
  if (frags.size === 0) return '';
  const seqs = Array.from(frags.keys()).sort((a, b) => a - b);
  let out = '';
  let emittedEnd = 0;
  for (const s of seqs) {
    const text = frags.get(s) ?? '';
    const range = ranges?.get(s) ?? null;
    if (!range) {
      out += text;
      continue;
    }
    if (range.end <= emittedEnd) continue;
    if (range.start <= emittedEnd && out) {
      out += text.slice(suffixPrefixOverlap(out, text));
    } else {
      out += text;
    }
    emittedEnd = Math.max(emittedEnd, range.end);
  }
  return out;
}

/**
 * 记录一条 delta 片段并返回重组后的完整正文。
 * 调用方（applyChunk 的 delta 分支）用它**覆盖** reducer 算出的 `cur + text`（到达顺序拼接）。
 * 幂等：同一 seq 重复写入用相同 text 覆盖（去重层已防重复帧，这里是二次防御）。
 */
export function noteDelta(
  sessionId: string,
  assistantId: string,
  seq: number,
  text: string,
  range?: DeltaRange | null,
): string {
  const turn = ensureTurn(sessionId, assistantId);
  turn.frags.set(seq, text);
  if (range) turn.ranges.set(seq, range);
  return reconstruct(turn.frags, turn.ranges);
}

/** 清除指定回合（assistantId）的片段缓冲。回合封口（finalizeTurn）时调用。 */
export function resetAssistant(sessionId: string, assistantId: string): void {
  buffers.get(sessionId)?.delete(assistantId);
}

/** 清除指定会话的全部片段缓冲。用户停止（stopGeneration）/ 会话删除（removeSessionState）时调用。 */
export function resetSession(sessionId: string): void {
  buffers.delete(sessionId);
}

/**
 * 清除指定会话中「不在 keepIds 里」的回合缓冲。历史替换（loadBackendHistory）时调用——
 * 保留仍在 live 流式的尾巴（其 assistantId 在新消息列表里），清掉被替换掉的旧回合。
 */
export function resetSessionExcept(sessionId: string, keepIds: Set<string>): void {
  const byAid = buffers.get(sessionId);
  if (!byAid) return;
  for (const aid of Array.from(byAid.keys())) {
    if (!keepIds.has(aid)) byAid.delete(aid);
  }
}

/** 单测 / 诊断：读取某回合当前片段缓冲的拷贝。 */
export function peekFrags(sessionId: string, assistantId: string): Map<number, string> {
  return new Map(buffers.get(sessionId)?.get(assistantId)?.frags ?? []);
}
