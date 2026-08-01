import { useEffect, useMemo, useState } from "react";
import type { TodoItem } from "../types";

interface Props {
  todos: TodoItem[];
}

export default function TodoProgressPanel({ todos }: Props) {
  const [pinnedOpen, setPinnedOpen] = useState<boolean | null>(null);
  const total = todos.length;
  const done = todos.filter((todo) => todo.status === "completed").length;
  const current = todos.find((todo) => todo.status === "in_progress");
  const activeCount = todos.filter((todo) => todo.status === "pending" || todo.status === "in_progress").length;
  const allDone = total > 0 && done === total;
  const defaultOpen = !allDone && activeCount > 0;
  const open = pinnedOpen ?? defaultOpen;

  useEffect(() => {
    if (pinnedOpen === false) return;
    if (activeCount > 0) setPinnedOpen(null);
  }, [activeCount, pinnedOpen]);

  const title = current?.content || (allDone ? "所有步骤已完成" : "等待下一步");
  const rows = useMemo(() => todos.slice(0, 30), [todos]);

  if (total === 0) return null;

  return (
    <section className={"todo-panel" + (open ? " todo-panel--open" : "")} aria-label="任务进度">
      <button
        className="todo-panel__header"
        type="button"
        aria-expanded={open}
        onClick={() => setPinnedOpen((value) => !(value ?? defaultOpen))}
      >
        <span className="todo-panel__icon" aria-hidden="true">
          <ListIcon />
        </span>
        <span className="todo-panel__main">
          <span className="todo-panel__title">任务进度 {done}/{total}</span>
          <span className="todo-panel__current">{title}</span>
        </span>
        <span className="todo-panel__meter" aria-hidden="true">
          <span style={{ width: `${Math.round((done / total) * 100)}%` }} />
        </span>
        <span className={"todo-panel__caret" + (open ? " todo-panel__caret--open" : "")} aria-hidden="true">
          <CaretIcon />
        </span>
      </button>
      {open && (
        <div className="todo-panel__body">
          {rows.map((todo) => (
            <div key={`${todo.id}-${todo.content}`} className={`todo-panel__row todo-panel__row--${statusClass(todo.status)}`}>
              <span className="todo-panel__mark" aria-hidden="true">{markFor(todo.status)}</span>
              <span className="todo-panel__content">{todo.content || todo.id}</span>
              <span className="todo-panel__status">{labelFor(todo.status)}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function statusClass(status: string): string {
  if (status === "in_progress") return "active";
  if (status === "completed") return "done";
  if (status === "cancelled") return "cancelled";
  return "pending";
}

function markFor(status: string): string {
  if (status === "completed") return "✓";
  if (status === "in_progress") return "›";
  if (status === "cancelled") return "×";
  return "○";
}

function labelFor(status: string): string {
  if (status === "completed") return "完成";
  if (status === "in_progress") return "进行中";
  if (status === "cancelled") return "取消";
  return "待办";
}

function ListIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 6h13" />
      <path d="M8 12h13" />
      <path d="M8 18h13" />
      <path d="M3 6h.01" />
      <path d="M3 12h.01" />
      <path d="M3 18h.01" />
    </svg>
  );
}

function CaretIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="m9 18 6-6-6-6" />
    </svg>
  );
}
