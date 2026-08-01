import type { TodoItem } from "../types";

/**
 * 判断当前是否应该显示 todo 进度面板。
 * 规则：
 * - 没有 todo 时不显示；
 * - 全部 todo 都已 completed 时隐藏；
 * - 只要存在 pending / in_progress / cancelled 就显示。
 */
export function shouldShowTodoPanel(todos: TodoItem[]): boolean {
  if (todos.length === 0) return false;
  return !todos.every((todo) => todo.status === "completed");
}
