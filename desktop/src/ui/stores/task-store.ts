/**
 * task-store：任务列表 + 任务看板开关
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { Task } from '../backend-client';

export interface KanbanBoardState {
  workflow?: Record<string, unknown>;
  tasks: unknown[];
  dependencies: unknown[];
  events: unknown[];
}

export interface TaskStoreState {
  tasks: Task[];
  taskBoardOpen: boolean;
  taskBoardWidth: number;
  kanbanBoard: KanbanBoardState | null;
}

export const taskStore: Store<TaskStoreState> = createStore<TaskStoreState>(
  {
    tasks: [],
    taskBoardOpen: false,
    taskBoardWidth: 320,
    kanbanBoard: null,
  },
  'task',
);
