import { describe, it, expect } from 'vitest';
import {
  shouldShowTodoPanel,
  renderTodoProgressPanelHtml,
  todoCurrentStepIndex,
} from '../../src/ui/chat-render';
import type { TodoItem } from '../../src/ui/state';

describe('shouldShowTodoPanel', () => {
  it('returns false for empty todos', () => {
    expect(shouldShowTodoPanel([])).toBe(false);
  });

  it('returns false when all todos are completed', () => {
    const todos: TodoItem[] = [
      { id: '1', content: 'A', status: 'completed' },
      { id: '2', content: 'B', status: 'completed' },
    ];
    expect(shouldShowTodoPanel(todos)).toBe(false);
  });

  it('returns true when there is a pending todo', () => {
    const todos: TodoItem[] = [
      { id: '1', content: 'A', status: 'completed' },
      { id: '2', content: 'B', status: 'pending' },
    ];
    expect(shouldShowTodoPanel(todos)).toBe(true);
  });

  it('returns true when there is an in_progress todo', () => {
    const todos: TodoItem[] = [
      { id: '1', content: 'A', status: 'in_progress' },
    ];
    expect(shouldShowTodoPanel(todos)).toBe(true);
  });

  it('returns true when there is a cancelled todo', () => {
    const todos: TodoItem[] = [
      { id: '1', content: 'A', status: 'completed' },
      { id: '2', content: 'B', status: 'cancelled' },
    ];
    expect(shouldShowTodoPanel(todos)).toBe(true);
  });
});

describe('todoCurrentStepIndex', () => {
  it('prefers in_progress index', () => {
    const todos: TodoItem[] = [
      { id: '1', content: 'A', status: 'completed' },
      { id: '2', content: 'B', status: 'in_progress' },
      { id: '3', content: 'C', status: 'pending' },
    ];
    expect(todoCurrentStepIndex(todos)).toBe(2);
  });

  it('falls back to first pending', () => {
    const todos: TodoItem[] = [
      { id: '1', content: 'A', status: 'completed' },
      { id: '2', content: 'B', status: 'pending' },
    ];
    expect(todoCurrentStepIndex(todos)).toBe(2);
  });
});

describe('renderTodoProgressPanelHtml', () => {
  const todos: TodoItem[] = [
    { id: '1', content: 'Done step', status: 'completed' },
    { id: '2', content: 'Current step', status: 'in_progress' },
    { id: '3', content: 'Next step', status: 'pending' },
  ];

  it('collapsed: shows current title + step count, hides full list', () => {
    const html = renderTodoProgressPanelHtml(todos, false, 'todo:current');
    expect(html).toContain('Current step');
    expect(html).toContain('2/3');
    expect(html).not.toContain('desktop-todo-panel__body');
    expect(html).not.toContain('Next step');
    expect(html).toContain('aria-expanded="false"');
  });

  it('expanded: lists all todos', () => {
    const html = renderTodoProgressPanelHtml(todos, true, 'todo:current');
    expect(html).toContain('desktop-todo-panel__body');
    expect(html).toContain('Done step');
    expect(html).toContain('Next step');
    expect(html).toContain('aria-expanded="true"');
  });
});
