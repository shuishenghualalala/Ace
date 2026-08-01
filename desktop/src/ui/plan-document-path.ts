/**
 * Plan 模式计划文档路径识别。
 * 这类文件属于蓝图（.crew/plans/.../plan_*.md），不应出现在「本次会话改动」列表里。
 */

/** 是否为计划文档路径（绝对或相对均可）。 */
export function isPlanDocumentPath(path: string): boolean {
  if (!path) return false;
  const n = path.replace(/\\/g, '/');
  if (/\/plans\/[^/]+\/[^/]+\/plan_[^/]+\.md$/i.test(n)) return true;
  if (/(^|\/)\.crew\/plans\//i.test(n) && /\/plan_[^/]+\.md$/i.test(n)) return true;
  if (/(^|\/)plans\/[^/]+\/[^/]+\/plan_[^/]+\.md$/i.test(n)) return true;
  // 宽松：文件名本身是 plan_*.md 且路径含 /plans/
  if (/\/plans\//i.test(n) && /\/plan_[^/]+\.md$/i.test(n)) return true;
  return false;
}
