/**
 * 折叠状态管理：回合级 + 工具卡片级，localStorage 持久化（跨重启保留）。
 *
 * 设计参考：hermes-agent/apps/desktop/src/store/tool-view.ts 的 `$toolDisclosureStates`——
 * 按「 disclosure ID 」记录用户的开/合选择，跨会话保留，不被流式重建冲掉。
 *
 * 与现有 state.userFoldedTurns / userUnfoldedTurns 的关系：
 *  - 回合级：仍由 state 那两个 Set 作为「当前会话内」的快速读路径；
 *    本模块在 setTurnFold 时同步写 state，并在 app 启动 / login 时把 localStorage
 *    灌回 state，保证 renderChat 的现有读路径不变。
 *  - 工具卡片级：完全在本模块内管理（in-memory Map + localStorage），不进 sessionStore，
 *    避免改动 sessionStore 形状波及 stores.ts / login.ts / state.ts 多个文件。
 */

const TURN_FOLD_KEY = 'crew.desktop.turnFold.v1';
const TOOL_FOLD_KEY = 'crew.desktop.toolFold.v1';
const MAX_ENTRIES = 240;

/** 内存缓存：turnId → 用户选择（true=展开，false=折叠）。 */
const turnFold = new Map<string, boolean>();
/** 内存缓存：toolKey → 用户选择。 */
const toolFold = new Map<string, boolean>();

let loaded = false;

/** 读取 localStorage 里的 JSON 对象，容错。 */
function loadRecord(key: string): Record<string, boolean> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const out: Record<string, boolean> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof k === 'string' && typeof v === 'boolean') out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

/** 写入 localStorage，截断到 MAX_ENTRIES 防止无限增长。 */
function persistRecord(key: string, map: Map<string, boolean>): void {
  if (typeof window === 'undefined') return;
  try {
    const entries = Array.from(map.entries()).slice(-MAX_ENTRIES);
    window.localStorage.setItem(key, JSON.stringify(Object.fromEntries(entries)));
  } catch {
    // 折叠偏好是本地 UI 状态，写失败忽略。
  }
}

/** 懒加载：首次访问时把 localStorage 灌进内存。 */
function ensureLoaded(): void {
  if (loaded) return;
  loaded = true;
  const t = loadRecord(TURN_FOLD_KEY);
  for (const [k, v] of Object.entries(t)) turnFold.set(k, v);
  const tc = loadRecord(TOOL_FOLD_KEY);
  for (const [k, v] of Object.entries(tc)) toolFold.set(k, v);
}

// ---------- 回合级 ----------

/** 读取回合折叠偏好；无偏好返回 null（让渲染走默认值）。 */
export function getTurnFold(turnId: string): boolean | null {
  ensureLoaded();
  return turnFold.has(turnId) ? turnFold.get(turnId)! : null;
}

/**
 * 记录用户对某回合的展开/折叠选择，并持久化。
 * 同时同步到 state 那两个 Set，让 renderChat 现有读路径（userPinnedOpen）继续工作。
 */
export function setTurnFold(
  turnId: string,
  open: boolean,
  stateSets: { unfolded: Set<string>; folded: Set<string> },
): void {
  ensureLoaded();
  turnFold.set(turnId, open);
  persistRecord(TURN_FOLD_KEY, turnFold);
  // 同步到 state 集合（applyFoldState 的逻辑）
  if (open) {
    stateSets.unfolded.add(turnId);
    stateSets.folded.delete(turnId);
  } else {
    stateSets.folded.add(turnId);
    stateSets.unfolded.delete(turnId);
  }
}

/**
 * 清除「仅展开」偏好（内存 + localStorage），不写入 folded。
 * 用于单测 / 显式丢弃展开 pin；运行时推理阶段临时展开走 ephemeralUnfoldedTurns。
 */
export function clearTurnUnfoldPreference(
  turnId: string,
  stateSets: { unfolded: Set<string>; folded: Set<string> },
): void {
  ensureLoaded();
  if (turnFold.get(turnId) === true) {
    turnFold.delete(turnId);
    persistRecord(TURN_FOLD_KEY, turnFold);
  }
  stateSets.unfolded.delete(turnId);
}

/**
 * 把 localStorage 里持久化的回合偏好灌进 state 集合。
 * 在 app 启动 / login 完成后调用一次，让历史回合的折叠选择在重启后仍生效。
 */
export function hydrateTurnFoldFromStorage(
  turnIds: Iterable<string>,
  stateSets: { unfolded: Set<string>; folded: Set<string> },
): void {
  ensureLoaded();
  for (const id of turnIds) {
    const v = turnFold.get(id);
    if (v === true) {
      stateSets.unfolded.add(id);
      stateSets.folded.delete(id);
    } else if (v === false) {
      stateSets.folded.add(id);
      stateSets.unfolded.delete(id);
    }
  }
}

// ---------- 工具卡片级 ----------

/**
 * 工具卡片折叠键：与 hermes 的 `tool-entry:${messageId}:${toolPartId}` 同构。
 * 这里用 message id + toolCallId，跨重启后 message id 仍由历史回放稳定生成。
 */
export function toolFoldKey(messageId: string, toolCallId: string): string {
  return `tool:${messageId}:${toolCallId}`;
}

/** 读取工具卡片折叠偏好；无偏好返回 null（让渲染走默认值）。 */
export function getToolFold(key: string): boolean | null {
  ensureLoaded();
  return toolFold.has(key) ? toolFold.get(key)! : null;
}

/** 记录用户对某工具卡片的展开/折叠选择，并持久化。 */
export function setToolFold(key: string, open: boolean): void {
  ensureLoaded();
  toolFold.set(key, open);
  persistRecord(TOOL_FOLD_KEY, toolFold);
}

// ---------- 退出 / 切账号时清理 ----------

/**
 * 清空内存缓存（不删 localStorage——localStorage 是跨账号共享的本地偏好，
 * 切账号时不需要清；如果未来需要按账号隔离，再扩展为按账号 namespace）。
 * 当前用于 logout 时让内存回到初始状态，避免下个账号看到上个账号的内存残留。
 */
export function clearFoldMemoryCache(): void {
  turnFold.clear();
  toolFold.clear();
  loaded = false;
}
