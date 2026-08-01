/**
 * renderChat 使用的纯逻辑 keyed 增量渲染 diff。
 *
 * 每帧渲染目标被建模成有序的 render unit 列表，跨帧维护 Map<key, node>，
 * 只重写真正变化的单元，避免长会话在每个流式分片中重建整棵 DOM 树。
 *
 * 这里只放 **可单测的纯逻辑** —— 不碰 DOM、不读 state、无模块级副作用。
 * diffRenderUnits(prev, next) → DiffOp[]，由 chat-controller 侧应用到真实 DOM。
 *
 * 设计原则（SPEC）：
 *  - chat 单元顺序是 append-mostly（消息只追加；编辑是 truncate-then-append；回合极少乱序），
 *    所以算法简单且正确即可：按 key 配对，sig 相同→reuse，sig 不同→patch，新 key→append，
 *    prev 中不再存在的 key→remove。move（同 key 位置变化）按 remove+append 处理（保守、正确）。
 *  - **确定性**：相同 prev/next 永远产生相同的 op 列表。
 *  - 顺序：apply 阶段用 appendChild-move 兜底保证最终 DOM 顺序 == next 顺序，
 *    因此 diff 只需声明「这个 key 还要不要、要不要重写」，不需要 emit 精确的 move op。
 */

/** 一个渲染单元的元数据（不含 build fn —— 纯逻辑层不关心 DOM 怎么造）。 */
export interface RenderUnit {
  /** 稳定 key：跨帧同一逻辑单元用同一 key（msg.id / turnId / '__gateway' / ...）。 */
  key: string;
  /** 内容签名：当 key 相同但 sig 变化时触发 patch（重建该单元）。
   *  必须在「该单元渲染输出会变」时也变化；偏细只多一次重建（安全），偏粗会 stale（bug）。 */
  sig: string;
}

/** diff 产出的操作。apply 侧（chat-controller）按此更新 DOM + Map<key,node>。 */
export type DiffOp =
  | { type: 'reuse'; key: string }
  | { type: 'patch'; key: string }
  | { type: 'append'; key: string }
  | { type: 'remove'; key: string };

/**
 * 计算从 prev → next 的最小操作序列。
 *
 * 算法：
 *  1. 建立 prev 的 key→RenderUnit 索引（同 key 取最后一个；正常情况 key 唯一）。
 *  2. 顺序遍历 next：
 *     - prev 没有 → append
 *     - prev 有 & sig 相等 → reuse
 *     - prev 有 & sig 不等 → patch
 *  3. 遍历 prev，凡 next 不含的 key → remove。
 *
 * 不 emit move：即便同 key 位置变了，apply 阶段会按 next 顺序 appendChild-move 重排，
 * 此时 sig 仍比对，所以 move 不会被误判成 patch（sig 没变就不会重建节点）。
 *
 * 返回顺序约定：先按 next 顺序输出 reuse/patch/append，再输出 remove（remove 顺序无关紧要，
 * apply 侧统一删除）。这个确定性顺序是单测可断言的。
 */
export function diffRenderUnits(prev: RenderUnit[], next: RenderUnit[]): DiffOp[] {
  const prevMap = new Map<string, RenderUnit>();
  for (const u of prev) prevMap.set(u.key, u);

  const nextKeys = new Set<string>();
  const ops: DiffOp[] = [];

  for (const unit of next) {
    // 同一 next 列表里重复 key：以最后一次为准（apply 侧也只保留一个节点）。
    nextKeys.add(unit.key);
    const p = prevMap.get(unit.key);
    if (p === undefined) {
      ops.push({ type: 'append', key: unit.key });
    } else if (p.sig === unit.sig) {
      ops.push({ type: 'reuse', key: unit.key });
    } else {
      ops.push({ type: 'patch', key: unit.key });
    }
  }

  for (const u of prev) {
    if (!nextKeys.has(u.key)) ops.push({ type: 'remove', key: u.key });
  }

  return ops;
}
