/**
 * Wiki 详情面板的多 Tab 状态管理（纯逻辑）：
 * 每个打开过的页面 / Vault 文档（Home.md、index.md）都是一个可切换、可关闭的 Tab。
 */

export type WikiOpenTab =
  | { kind: "page"; id: string }
  | { kind: "doc"; name: "Home.md" | "index.md" };

/** Tab 的稳定标识，用于去重和激活态判定。 */
export function tabKey(tab: WikiOpenTab): string {
  return tab.kind === "page" ? `page:${tab.id}` : `doc:${tab.name}`;
}

/**
 * 打开一个 Tab：已存在（按 tabKey）则保持原顺序不变；
 * 不存在则追加到末尾。不修改入参。
 */
export function openTab(
  tabs: WikiOpenTab[],
  tab: WikiOpenTab,
): { tabs: WikiOpenTab[]; changed: boolean } {
  const key = tabKey(tab);
  if (tabs.some((t) => tabKey(t) === key)) {
    return { tabs, changed: false };
  }
  return { tabs: [...tabs, tab], changed: true };
}

/**
 * 关闭一个 Tab：返回剩余 tabs 和新的激活 key。
 * 若关闭的正是激活 Tab，激活相邻 Tab（优先右侧，其次左侧）；
 * 关闭非激活 Tab 时保持 activeKey 不变；没有剩余 Tab 时返回 null。
 */
export function closeTab(
  tabs: WikiOpenTab[],
  key: string,
  activeKey: string | null,
): { tabs: WikiOpenTab[]; nextActiveKey: string | null } {
  const index = tabs.findIndex((t) => tabKey(t) === key);
  if (index === -1) return { tabs, nextActiveKey: activeKey };
  const next = tabs.filter((_, i) => i !== index);
  if (key !== activeKey) return { tabs: next, nextActiveKey: activeKey };
  if (next.length === 0) return { tabs: next, nextActiveKey: null };
  // 优先激活右侧相邻 Tab；关闭的是最后一个时退到左侧。
  const neighbor = next[index] ?? next[index - 1];
  return { tabs: next, nextActiveKey: tabKey(neighbor) };
}
