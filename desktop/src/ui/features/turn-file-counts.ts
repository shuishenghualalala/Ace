/**
 * 为本轮「已编辑文件」卡补全 +/- 行数。
 *
 * 实时路径通常已有准确计数；旧历史仅从 file_write 路径兜底时为 0。
 * 优先用会话 book.fileChanges；仍缺时按「新建文件」读盘统计行数（与看板 Files hydrate 一致）。
 * 存在性：ENOENT / 明确不存在 → 剔除幽灵；权限/其他读盘失败 → 保留项（计数可暂为 0），避免误删真实卡。
 * 存在性探测走 pathExists（静默），禁止用 readTextFile 探测以免主进程刷 ENOENT。
 */

import type { TurnFileChangeSummary } from '../chat-render';
import { buildDiffFromTexts, countDiffRows } from '../diff-lines';
import { filterExistingTurnFileChanges } from './history-mapping';
import { getBookFileChanges, type FileChange } from '../state';
import { messageStore } from '../stores/stores';

/** 是否仍有文件缺少有效增删计数（全 0）。 */
export function needsCountHydration(files: TurnFileChangeSummary[]): boolean {
  return files.some((f) => !f.binary && (f.added || 0) === 0 && (f.removed || 0) === 0);
}

/**
 * 用 book / 带 diff 的 FileChange 列表补全摘要计数。
 * 已有非零计数的项保持不变。
 */
export function mergeCountsFromFileChanges(
  files: TurnFileChangeSummary[],
  sources: FileChange[],
): TurnFileChangeSummary[] {
  if (!files.length || !sources.length) return files;
  const byPath = new Map(sources.map((f) => [f.path, f]));
  let changed = false;
  const out = files.map((f) => {
    if ((f.added || 0) > 0 || (f.removed || 0) > 0) return f;
    const src = byPath.get(f.path);
    if (!src) return f;
    let added = src.added || 0;
    let removed = src.removed || 0;
    if (added === 0 && removed === 0 && src.diff?.length) {
      const counted = countDiffRows(src.diff);
      added = counted.added;
      removed = counted.removed;
    }
    if (added === 0 && removed === 0) return f;
    changed = true;
    return { ...f, added, removed, status: src.status || f.status };
  });
  return changed ? out : files;
}

export type CountLinesResult =
  | { ok: true; added: number; removed: number }
  | { ok: false; reason: 'missing' | 'error' };

/**
 * 读盘后按「整文件新增」估算行数。
 * - missing：ENOENT / 空内容等「文件不在」→ 可剔幽灵
 * - error：权限或其他失败 → 保留卡，勿当幽灵删
 */
export async function countLinesAsNewFile(path: string): Promise<CountLinesResult> {
  if (!path || !window.Crew?.readTextFile) return { ok: false, reason: 'error' };
  try {
    const text = await window.Crew.readTextFile(path);
    if (typeof text !== 'string' || !text) return { ok: false, reason: 'missing' };
    const counted = countDiffRows(buildDiffFromTexts(null, text));
    return { ok: true, added: counted.added, removed: counted.removed };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err ?? '');
    // 主进程对不存在文件会抛；权限类错误保留条目
    if (/ENOENT|not found|does not exist|no such file/i.test(msg)) {
      return { ok: false, reason: 'missing' };
    }
    return { ok: false, reason: 'error' };
  }
}

/**
 * 扫描会话消息：剔除磁盘已不存在的幽灵路径，并为缺计数项补全 +/-。
 * @returns 是否改写了任意消息（调用方据此重绘）。
 */
export async function hydrateMissingTurnFileCounts(sessionId: string): Promise<boolean> {
  if (!sessionId) return false;
  const cur = messageStore.get().messages[sessionId] ?? [];
  if (cur.length === 0) return false;

  const book = getBookFileChanges(sessionId);
  let next = cur;
  let anyChanged = false;

  for (let i = 0; i < cur.length; i += 1) {
    const m = cur[i];
    if (m.role !== 'assistant' || !m.turnFileChanges?.length) continue;

    // Gateway 精确落库项保留生成当时的状态与 +/-。只有旧 tool_call 推断项才允许
    // 依据当前磁盘剔幽灵/补计数；否则后续轮删除文件会篡改前一轮历史卡。
    const persistedPaths = new Set(m.turnFileChangesPersistedPaths ?? []);
    const inferred = m.turnFileChanges.filter((file) => !persistedPaths.has(file.path));
    let files = await filterExistingTurnFileChanges(inferred);
    files = mergeCountsFromFileChanges(files, book);
    if (needsCountHydration(files)) {
      const patched: TurnFileChangeSummary[] = [];
      for (const f of files) {
        if (f.binary || (f.added || 0) > 0 || (f.removed || 0) > 0 || f.status === 'deleted') {
          patched.push(f);
          continue;
        }
        const counted = await countLinesAsNewFile(f.path);
        if (counted.ok && (counted.added > 0 || counted.removed > 0)) {
          patched.push({
            ...f,
            added: counted.added,
            removed: counted.removed,
            status: counted.removed === 0 ? 'added' : f.status,
          });
        } else if (!counted.ok && counted.reason === 'error') {
          // 权限/瞬时失败：保留原项，勿当幽灵剔除
          patched.push(f);
        }
        // missing → 剔除
      }
      files = patched;
    }

    const hydratedByPath = new Map(files.map((file) => [file.path, file]));
    files = m.turnFileChanges.flatMap((file) => {
      if (persistedPaths.has(file.path)) return [file];
      const hydrated = hydratedByPath.get(file.path);
      return hydrated ? [hydrated] : [];
    });

    const prev = m.turnFileChanges;
    const same =
      files.length === prev.length
      && files.every((f, idx) => {
        const p = prev[idx];
        return p
          && p.path === f.path
          && p.added === f.added
          && p.removed === f.removed
          && p.status === f.status;
      });
    if (same) continue;

    if (next === cur) next = cur.slice();
    next[i] = { ...m, turnFileChanges: files.length > 0 ? files : undefined };
    anyChanged = true;
  }

  if (anyChanged) {
    messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: next } });
  }
  return anyChanged;
}
