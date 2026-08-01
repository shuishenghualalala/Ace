/**
 * 流式 delta 按 gateway_sequence 重组，避免到达顺序与生成顺序不一致时正文串位或丢字。
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

/** session → assistantId → 片段缓冲。模块级状态，跨 applyChunk 调用累积。 */
const buffers = new Map<string, Map<string, FragmentBuffer>>();

function ensureFrags(sessionId: string, assistantId: string): FragmentBuffer {
  let byAid = buffers.get(sessionId);
  if (!byAid) {
    byAid = new Map();
    buffers.set(sessionId, byAid);
  }
  let frags = byAid.get(assistantId);
  if (!frags) {
    frags = new Map();
    byAid.set(assistantId, frags);
  }
  return frags;
}

/**
 * 纯函数：把「seq → text」片段按 seq 升序拼接成完整正文。可单测。
 *
 * delta 在一个回合内共享会话级单调 seq（中间夹带的 status/tool 帧占用别的 seq，不进本缓冲），
 * 故「按 seq 升序拼接 delta 片段」恒等于正确文本顺序——即便分片乱序到达、或 seq 非连续。
 */
export function reconstruct(frags: FragmentBuffer): string {
  if (frags.size === 0) return '';
  const seqs = Array.from(frags.keys()).sort((a, b) => a - b);
  let out = '';
  for (const s of seqs) out += frags.get(s);
  return out;
}

/**
 * 记录一条 delta 片段并返回重组后的完整正文。
 * 调用方（applyChunk 的 delta 分支）用它**覆盖** reducer 算出的 `cur + text`（到达顺序拼接）。
 * 幂等：同一 seq 重复写入用相同 text 覆盖（去重层已防重复帧，这里是二次防御）。
 */
export function noteDelta(sessionId: string, assistantId: string, seq: number, text: string): string {
  const frags = ensureFrags(sessionId, assistantId);
  frags.set(seq, text);
  return reconstruct(frags);
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
  return new Map(buffers.get(sessionId)?.get(assistantId) ?? []);
}
