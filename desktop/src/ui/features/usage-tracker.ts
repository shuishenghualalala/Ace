/**
 * 前端真实数据 · 使用统计跟踪器
 *
 * 数据来源：
 *   1. chat 完成事件 —— 在 index.ts 的 applyChunk('final'|'error') 分支里
 *      调 recordTurn()，把真实的会话/模型/用时/字符数 写进本地日志。
 *   2. 后端 /api/usage —— 返回 session_store 累计的 total_tokens，
 *      作为 Hero 上的"真实消耗 Tokens"主数值（这是后端真实聚合）。
 *
 * 为什么 token 数要估算：
 *   当前 WS 协议只传 text，不传 token usage（OpenAI Provider 内部拿到
 *   但没回灌给前端）。所以本地用字符数 / 估算系数近似。UI 上明确标注
 *   "估算"，并把后端 /api/usage 的真实 total_tokens 优先展示。
 *
 * 持久化：localStorage（容量上限 500 条，超出滚动删除最旧记录）
 */

import { state } from '../state';
import { backendApi } from '../backend-client';
import type { UsagePayload } from '../reducers/chat-reducer';
import { STORAGE_KEYS } from '../../shared/storage-keys';

// ─────────────────────────── 数据形状 ───────────────────────────

export interface UsageRecord {
  id: string;
  ts: number;             // unix seconds（回合完成时刻）
  sessionId: string;
  workspaceId: string;    // 派生：state.currentWorkspaceId
  model: string;          // 派生：完成时的 state.configModel
  requestModel?: string | undefined;  // 若与计费不同（预留字段，留 null）
  provider: string;       // 派生：完成时的 channel/executor（暂固定 "session_log"）
  inputChars: number;     // 用户消息字符数（回合起点到该 turn 之间的累积输入）
  outputChars: number;    // 助手回复字符数（含 thinking）
  inputTokens: number;    // 估算
  outputTokens: number;   // 估算
  cacheReadTokens: number;// Anthropic 模型自动估值（30% 命中假设），用户可改
  cacheWriteTokens: number;
  durationMs: number;     // 来自 state.messages[].turnDurationMs
  firstTokenMs?: number | undefined;  // 首字延迟（来自 tracker 内部时间戳）
  status: number;         // 200 / 4xx / 5xx
  source: string;         // 'session_log' / 'expert' / 'skills' ...
  edited: boolean;        // 是否被用户在编辑面板手动改过
  /** 本条 token 数字是否来自 Provider API 真实返回值（而非字符估算） */
  fromProvider: boolean;
}

export interface UsageSummary {
  /** 主数值：本回合计费总和（cache-normalized）。无记录时 fallback 到 backend。 */
  totalTokens: number;
  /** 后端 /api/usage 报告的 total_tokens（每个会话的当前上下文大小累加）。
   *  仅作参考，与 totalTokens 不同口径。 */
  backendTotalTokens: number;
  totalRequests: number;         // 本地记录数
  totalInput: number;            // 本地估算 token（所有回合 input 之和）
  totalOutput: number;
  totalCacheCreate: number;
  totalCacheRead: number;
  hitRate: number;               // 0..1
  successRate: number;           // 0..1
  backendReported: boolean;      // /api/usage 是否成功
  /** 是否有任意一条记录来自 Provider 真实 usage（而非字符估算） */
  hasProviderData: boolean;
}

export interface ProviderStat {
  provider: string;
  requests: number;
  input: number;
  output: number;
  cacheRead: number;
  successRate: number;
}

export interface ModelStat {
  model: string;
  requests: number;
  input: number;
  output: number;
  cacheRead: number;
  avgDurationMs: number;
  successRate: number;
}

export interface TrendPoint {
  ts: number;          // unix seconds（小时桶起点）
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
}

// ─────────────────────────── 存储 ───────────────────────────

const MAX_RECORDS = 500;

/**
 * localStorage key 按当前登录账号 staffCode 分桶，实现多账号隔离：
 * 同一台机器切换账号不会看到彼此的用量；登出时只清当前账号的桶。
 *
 * key 形如 `Crew.usage.records.v1:<staffCode>`。staffCode 缺失
 * （未登录 / 旧数据无 staffCode）时落 `__anonymous__` 匿名桶，绝不与
 * 真实账号混。基础 key 值与 shared/storage-keys.ts 的注册表保持一致。
 */
const ANON_BUCKET = '__anonymous__';

function storageKey(): string {
  const sc = state.userInfo?.staffCode?.trim();
  return `${STORAGE_KEYS.usageRecords}:${sc || ANON_BUCKET}`;
}

// 简易订阅：跟踪器被 recordTurn 后通知监听者刷新
type Listener = () => void;
const listeners = new Set<Listener>();

function loadFromStorage(): UsageRecord[] {
  try {
    const raw = localStorage.getItem(storageKey());
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.slice(-MAX_RECORDS) : [];
  } catch {
    return [];
  }
}

function saveToStorage(records: UsageRecord[]): void {
  try {
    const trimmed = records.slice(-MAX_RECORDS);
    localStorage.setItem(storageKey(), JSON.stringify(trimmed));
  } catch {
    /* quota 写不下时静默 */
  }
}

let cache: UsageRecord[] | null = null;
let cachedKey: string | null = null;

function getAll(): UsageRecord[] {
  const key = storageKey();
  if (cache === null || cachedKey !== key) {
    cache = loadFromStorage();
    cachedKey = key;
  }
  return cache;
}

function setAll(next: UsageRecord[]): void {
  cache = next.slice(-MAX_RECORDS);
  cachedKey = storageKey();
  saveToStorage(cache);
  listeners.forEach((fn) => fn());
}

/**
 * 账号切换 / 登录态恢复时调用：丢弃按旧账号加载的内存缓存，
 * 下次读取按新 staffCode 重新 load。冷启动恢复、登录成功、切账号都会触发。
 * 极轻量（仅清两个变量），可无条件调用。
 */
export function resetForAccountChange(): void {
  cache = null;
  cachedKey = null;
  // 账号变了→可见数据变了，通知订阅者刷新（面板未打开时回调自行 short-circuit）。
  listeners.forEach((fn) => fn());
}

/**
 * 登出时清掉**当前账号**的本地用量记录（隐私：换人登录看不到上一人数据）。
 * 必须在 state.userInfo 被清空之前调用——它依赖 staffCode 解析 key。
 */
export function clearCurrentAccountRecords(): void {
  try {
    localStorage.removeItem(storageKey());
  } catch {
    /* ignore */
  }
  cache = null;
  cachedKey = null;
  listeners.forEach((fn) => fn());
}

// ─────────────────────────── Token 估算 ───────────────────────────
// 经验值（与各家 tokenizer 接近）：
//   - 英文 / ASCII：≈ 0.25 tokens/char（4 chars/token）
//   - 中文 / CJK：≈ 0.7 tokens/char（1.5 chars/token）
//   - 数字 / 标点：按 0.3 tokens/char 算（折中）
//   - 工具调用 JSON：单独按 0.3 折算（结构化文本里 token 偏多）
// 之前按 chars/3 估值，对短中文明显偏低（如"1"估成 1 token 实际是 ~0.5，
// 但"你"这种纯 CJK 是 ~0.6~0.7 token，整体偏小）。CJK-aware 估算后
// 中英文混合内容更接近 OpenAI/Claude tokenizer 的真实计数。
const CJK_RE = /[㐀-鿿豈-﫿]/g;

function estimateTokensFromText(text: string): number {
  if (!text) return 0;
  const cjk = (text.match(CJK_RE) ?? []).length;
  const total = text.length;
  const other = total - cjk;
  // 中文 0.7 / char；英文 0.25 / char
  return Math.max(0, Math.ceil(cjk * 0.7 + other * 0.25));
}

/**
 * Anthropic-style 模型具备 prompt cache（cache_read / cache_creation）能力。
 * 对这些模型，估算时给一个保守的"假设有 30% prompt 命中了缓存"的初值
 * （可在编辑面板手动覆盖）。OpenAI-style（Codex/Gemini）默认 0。
 *
 * 用户实际接到的是状态消息 session_log —— 后端 WS 协议目前不传 cache 字段，
 * 因此这里的 cache 数字是"占位估值"，面板编辑后才视为真实。
 */
function suggestCacheRead(model: string, inputTokens: number): number {
  if (!ANTHROPIC_FAMILY_RE.test(model)) return 0;
  if (inputTokens < 500) return 0;
  // 30% 命中率；命中部分按 cache_read 口径单独统计。
  return Math.round(inputTokens * 0.3);
}
const ANTHROPIC_FAMILY_RE = /claude|anthropic/i;

/**
 * 在 chat 回合完成时调用 —— 由 index.ts 的 applyChunk final/error 分支触发。
 *
 * 优先使用 Provider API 返回的真实 token 数（input.usage）；
 * 仅在 usage 缺失时 fallback 到 CJK-aware 字符估算。
 * cache 数字对 Anthropic 家族模型优先取真实值，无真实值时按 30% 假设给初值。
 */
export function recordTurn(input: {
  sessionId: string;
  inputText: string;
  outputText: string;
  durationMs: number;
  firstTokenMs?: number | undefined;
  status: number;
  source?: string;
  usage?: UsagePayload;
}): void {
  const sid = input.sessionId || 'unknown';
  const wsId = state.currentWorkspaceId || 'default';
  const model = state.configModel || 'unknown';

  let inTok: number;
  let outTok: number;
  let cacheRead: number;
  let cacheWrite: number;
  let fromProvider = false;

  if (input.usage) {
    // 有 Provider 真实数据 → 优先使用
    inTok = input.usage.prompt_tokens ?? estimateTokensFromText(input.inputText);
    outTok = input.usage.completion_tokens ?? estimateTokensFromText(input.outputText);
    cacheRead = input.usage.cache_read_input_tokens
      ?? input.usage.cached_tokens
      ?? suggestCacheRead(model, inTok);
    cacheWrite = input.usage.cache_creation_input_tokens ?? 0;
    fromProvider = true;
  } else {
    // 无真实数据 → fallback 到字符估算
    inTok = estimateTokensFromText(input.inputText);
    outTok = estimateTokensFromText(input.outputText);
    cacheRead = suggestCacheRead(model, inTok);
    cacheWrite = 0;
  }

  const rec: UsageRecord = {
    id: `${Date.now().toString(36)}-${crypto.randomUUID().slice(0, 8)}`,
    ts: Math.floor(Date.now() / 1000),
    sessionId: sid,
    workspaceId: wsId,
    model,
    provider: input.source === 'expert' ? 'expert' : 'session_log',
    inputChars: input.inputText.length,
    outputChars: input.outputText.length,
    inputTokens: inTok,
    outputTokens: outTok,
    cacheReadTokens: cacheRead,
    cacheWriteTokens: cacheWrite,
    durationMs: input.durationMs,
    firstTokenMs: input.firstTokenMs,
    status: input.status,
    source: input.source ?? 'session_log',
    edited: false,
    fromProvider,
  };
  const all = getAll();
  all.push(rec);
  setAll(all);
}

/**
 * 编辑一条记录 —— 由编辑面板调用。patch 里的字段会覆盖原值；
 * 标记 edited=true 以便面板区分自动估算 vs 人工修正。
 */
export function updateRecord(id: string, patch: Partial<Pick<UsageRecord,
  'inputTokens' | 'outputTokens' | 'cacheReadTokens' | 'cacheWriteTokens' | 'model'
>>): boolean {
  const all = getAll();
  const idx = all.findIndex((r) => r.id === id);
  if (idx < 0) return false;
  const cur = all[idx];
  const next: UsageRecord = {
    ...cur,
    inputTokens: patch.inputTokens ?? cur.inputTokens,
    outputTokens: patch.outputTokens ?? cur.outputTokens,
    cacheReadTokens: patch.cacheReadTokens ?? cur.cacheReadTokens,
    cacheWriteTokens: patch.cacheWriteTokens ?? cur.cacheWriteTokens,
    model: patch.model ?? cur.model,
    edited: true,
  };
  all[idx] = next;
  setAll(all);
  return true;
}

/** 删除单条记录 */
export function deleteRecord(id: string): boolean {
  const all = getAll();
  const idx = all.findIndex((r) => r.id === id);
  if (idx < 0) return false;
  all.splice(idx, 1);
  setAll(all);
  return true;
}

/** 清空本地统计（不影响后端数据） */
export function clearLocalRecords(): void {
  setAll([]);
}

// ─────────────────────────── 订阅 ───────────────────────────

export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ─────────────────────────── 派生视图 ───────────────────────────

/**
 * Hero 用 summary。
 *
 * 两个口径：
 *   - `totalTokens` = 本地所有回合的 (input + output + cache) 之和
 *     （与 CC Switch 的 realTotalTokens 一致：每个 LLM 调用真正消耗的 token）
 *     这是 Hero 主数值。
 *   - `backendTotalTokens` = 后端 /api/usage 的 total_tokens
 *     （每个 session 当前上下文大小累加，是另一回事：会话结束/N 回合后这个值
 *      不会继续涨，但实际计费 token 还在累加。两个数字共存，不互相替代。）
 *
 * 为什么这么分：
 *   - 之前把 backend 总数当主数值时，跟单行日志（1/5 token）差距巨大，
 *     用户会困惑"为啥上面 6 千多下面个位数"。
 *   - 把"按回合计费总和"放主数值，按行汇总就一致了；backend 数作为参考标签。
 */
export async function getSummary(): Promise<UsageSummary> {
  const all = getAll();
  let backendTotalTokens = 0;
  let backendReported = false;
  try {
    const u = await backendApi.usage();
    backendTotalTokens = u.total_tokens ?? 0;
    backendReported = true;
  } catch {
    /* 网关未连 */
  }

  let input = 0, output = 0, cacheRead = 0, cacheWrite = 0, okCount = 0;
  let hasProviderData = false;
  for (const r of all) {
    input += r.inputTokens;
    output += r.outputTokens;
    cacheRead += r.cacheReadTokens;
    cacheWrite += r.cacheWriteTokens;
    if (r.status >= 200 && r.status < 300) okCount += 1;
    if (r.fromProvider) hasProviderData = true;
  }

  // 主数值 = 每回合实际计费（cache-normalized）的总和
  // 若用户从未产生过记录，再回退到后端 /api/usage 的数字保证面板不空白
  const totalTokens = all.length > 0
    ? input + output + cacheRead + cacheWrite
    : (backendReported ? backendTotalTokens : 0);

  const requests = all.length;
  const cacheable = input + cacheRead;
  const hitRate = cacheable > 0 ? cacheRead / cacheable : 0;

  return {
    totalTokens,
    totalRequests: requests,
    totalInput: input,
    totalOutput: output,
    totalCacheCreate: cacheWrite,
    totalCacheRead: cacheRead,
    hitRate,
    successRate: requests > 0 ? okCount / requests : 0,
    backendReported,
    backendTotalTokens,
    hasProviderData,
  };
}

export function getLogs(): UsageRecord[] {
  return getAll().slice();
}

export function getProviderStats(): ProviderStat[] {
  const map = new Map<string, ProviderStat>();
  for (const r of getAll()) {
    const cur = map.get(r.provider) ?? {
      provider: r.provider,
      requests: 0, input: 0, output: 0, cacheRead: 0,
      successRate: 0,
    };
    cur.requests += 1;
    cur.input += r.inputTokens;
    cur.output += r.outputTokens;
    cur.cacheRead += r.cacheReadTokens;
    if (r.status >= 200 && r.status < 300) cur.successRate += 1;
    map.set(r.provider, cur);
  }
  return Array.from(map.values()).map((s) => ({
    ...s,
    successRate: s.requests > 0 ? (s.successRate / s.requests) * 100 : 0,
  })).sort((a, b) => b.requests - a.requests);
}

export function getModelStats(): ModelStat[] {
  const map = new Map<string, ModelStat & { okCount: number; durationSum: number }>();
  for (const r of getAll()) {
    const cur = map.get(r.model) ?? {
      model: r.model,
      requests: 0, input: 0, output: 0, cacheRead: 0,
      avgDurationMs: 0,
      successRate: 0,
      okCount: 0, durationSum: 0,
    };
    cur.requests += 1;
    cur.input += r.inputTokens;
    cur.output += r.outputTokens;
    cur.cacheRead += r.cacheReadTokens;
    cur.durationSum += r.durationMs;
    if (r.status >= 200 && r.status < 300) cur.okCount += 1;
    map.set(r.model, cur);
  }
  return Array.from(map.values()).map((s) => ({
    model: s.model,
    requests: s.requests,
    input: s.input,
    output: s.output,
    cacheRead: s.cacheRead,
    avgDurationMs: s.requests > 0 ? s.durationSum / s.requests : 0,
    successRate: s.requests > 0 ? (s.okCount / s.requests) * 100 : 0,
  })).sort((a, b) => b.requests - a.requests);
}

/**
 * 24h 趋势，按小时桶聚合。
 * bucket 起点：ts - (ts % 3600)；跨度小时数由 hours 参数决定（24/168/720）。
 */
export function getTrend(hours = 24): TrendPoint[] {
  const now = Math.floor(Date.now() / 1000);
  const startHour = Math.floor(now / 3600) - (hours - 1);
  const buckets: TrendPoint[] = [];
  for (let h = 0; h < hours; h++) {
    buckets.push({
      ts: (startHour + h) * 3600,
      input: 0, output: 0, cacheRead: 0, cacheWrite: 0,
    });
  }
  const idxFor = (ts: number): number => {
    const hourIdx = Math.floor(ts / 3600) - startHour;
    return Math.max(0, Math.min(hours - 1, hourIdx));
  };
  for (const r of getAll()) {
    const i = idxFor(r.ts);
    const b = buckets[i];
    if (!b) continue;
    b.input += r.inputTokens;
    b.output += r.outputTokens;
    b.cacheRead += r.cacheReadTokens;
    b.cacheWrite += r.cacheWriteTokens;
  }
  return buckets;
}

/** 用于筛选下拉的派生列表：来自真实记录 */
export function getProviderOptions(): string[] {
  const set = new Set<string>();
  for (const r of getAll()) set.add(r.provider);
  return Array.from(set).sort();
}

export function getModelOptions(): string[] {
  const set = new Set<string>();
  for (const r of getAll()) set.add(r.model);
  return Array.from(set).sort();
}