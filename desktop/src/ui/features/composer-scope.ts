/**
 * 主对话 Composer root 注册表。
 *
 * 多 Composer 实例共存（主对话 + Wiki 问答面板）后，原先「全局唯一 id」查询
 * （#chat-input / #chat-todo-slot 等）失去唯一性保证，统一改为
 * 「主 Composer root 内 scope 查询」。主 Composer 由 app.ts 以 primary: true
 * 创建时注册；测试或未注册场景回退 document 全量查询
 * （文档序第一个匹配 = 主对话 Composer，wiki 面板在其后）。
 */

let primaryComposerRoot: HTMLElement | null = null;

export function registerPrimaryComposerRoot(root: HTMLElement | null): void {
  primaryComposerRoot = root;
}

export function getPrimaryComposerRoot(): HTMLElement | null {
  return primaryComposerRoot;
}

/** 在主 Composer root 内查询；未注册或未命中时回退 document 查询（取文档序第一个）。 */
export function queryPrimaryComposer<T extends Element = HTMLElement>(selector: string): T | null {
  const scoped = primaryComposerRoot?.querySelector<T>(selector);
  if (scoped) return scoped;
  return document.querySelector<T>(selector);
}
