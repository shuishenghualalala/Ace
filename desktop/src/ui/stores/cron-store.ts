/**
 * cron-store：定时任务列表 + 视图状态（scope/detail/删除确认）
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { CronJob } from '../backend-client';

export type CronJobScope = 'all' | 'current';

export interface CronJobStoreState {
  cronJobs: CronJob[];
  cronJobScope: CronJobScope;
  cronJobDetailId: string | null;
  cronDeleteConfirmId: string | null;
}

export const cronStore: Store<CronJobStoreState> = createStore<CronJobStoreState>(
  {
    cronJobs: [],
    cronJobScope: 'all',
    cronJobDetailId: null,
    cronDeleteConfirmId: null,
  },
  'cron',
);
