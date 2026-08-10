/**
 * 侧栏历史列表的挂载 helper：重置 document.body 并挂一个 #history-list 容器。
 * renderWorkspaceHistory 依赖该 id 定位列表根节点。
 */
export function mountHistoryList(): HTMLElement {
  const list = document.createElement('div');
  list.id = 'history-list';
  document.body.innerHTML = '';
  document.body.appendChild(list);
  return list;
}
