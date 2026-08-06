/**
 * @vitest-environment happy-dom
 *
 * cron-page 单测。
 * 覆盖 formatDuration / formatTimestamp / filteredJobs，以及任务页点击任务行后的滚动保持。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  formatDuration,
  formatTimestamp,
  filteredJobs,
  buildScheduleString,
  scheduleSummaryText,
  buildOnceScheduleFromDatetime,
  buildDailyScheduleFromTime,
  resolveScheduleFromCustomInputs,
  parseDateTimeInputs,
  formatDateInputValue,
  formatTimeInputValue,
  __setCronViewForTest,
  __resetCronListEventsForTest,
  bindCronTab,
  isRecurringJob,
  isRecurringSchedule,
  type FilterKey,
} from '../../src/ui/features/cron-page';
import { backendApi, type CronJob } from '../../src/ui/backend-client';
import { state } from '../../src/ui/state';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    cronJobs: vi.fn(async () => ({ jobs: [] })),
    cronJobDetail: vi.fn(async () => ({ runs: [] })),
    cronDeliveryTargets: vi.fn(async () => ({ targets: [] })),
  },
}));

const api = backendApi as unknown as {
  cronJobs: ReturnType<typeof vi.fn>;
  cronJobDetail: ReturnType<typeof vi.fn>;
  cronDeliveryTargets: ReturnType<typeof vi.fn>;
};

function makeJob(partial: Partial<CronJob>): CronJob {
  return {
    id: 'j1',
    name: 'n',
    kind: 'interval',
    interval_seconds: 60,
    trigger_type: 'interval',
    trigger_payload: {},
    schedule: '',
    schedule_summary: '',
    query: 'q',
    session_id: 's',
    workspace_id: 'w',
    enabled: true,
    last_status: '',
    next_run_at: 0,
    next_run_at_bj: '',
    last_run_at: 0,
    last_run_at_bj: '',
    created_at: 0,
    timezone: 'Asia/Shanghai',
    ...partial,
  };
}

describe('formatDuration', () => {
  it('zero / negative → dash', () => {
    expect(formatDuration(0)).toBe('—');
    expect(formatDuration(-5)).toBe('—');
  });
  it('seconds', () => {
    expect(formatDuration(45)).toBe('45 秒');
  });
  it('minutes', () => {
    expect(formatDuration(120)).toBe('2 分钟');
    expect(formatDuration(90)).toBe('1 分 30 秒');
  });
  it('hours', () => {
    expect(formatDuration(3600)).toBe('1 小时');
    expect(formatDuration(5400)).toBe('1 小时 30 分');
  });
  it('days', () => {
    expect(formatDuration(86400)).toBe('1 天');
    expect(formatDuration(90000)).toBe('1 天 1 小时');
  });
});

describe('formatTimestamp', () => {
  it('zero / negative → dash', () => {
    expect(formatTimestamp(0)).toBe('—');
  });
  it('future ts → "...后"', () => {
    const now = Math.floor(Date.now() / 1000);
    // 30 分钟后的时间戳
    const out = formatTimestamp(now + 30 * 60);
    expect(out).toContain('分钟');
    expect(out).toContain('后');
  });
  it('past ts → "...前"', () => {
    const now = Math.floor(Date.now() / 1000);
    const out = formatTimestamp(now - 2 * 3600);
    expect(out).toContain('小时');
    expect(out).toContain('前');
  });
  it('future opt adds "还有 " prefix', () => {
    const now = Math.floor(Date.now() / 1000);
    const out = formatTimestamp(now + 3600, { future: true });
    expect(out.startsWith('还有 ')).toBe(true);
  });
});

describe('buildScheduleString', () => {
  it('interval mode', () => {
    expect(buildScheduleString('interval', 30, 'm')).toBe('every 30m');
    expect(buildScheduleString('interval', 0, 'h')).toBe('every 1h');
  });
  it('once mode', () => {
    expect(buildScheduleString('once', 10, 'm')).toBe('10分钟后');
  });
});

describe('scheduleSummaryText', () => {
  it('maps presets', () => {
    expect(scheduleSummaryText('every 30m')).toBe('每 30 分钟');
    expect(scheduleSummaryText('30分钟后')).toBe('30 分钟后');
    expect(scheduleSummaryText('in 1800s')).toBe('一次性 · 30 分钟后');
    expect(scheduleSummaryText('每天9点30分')).toBe('每天9点30分');
  });
});

describe('datetime schedule helpers', () => {
  it('buildOnceScheduleFromDatetime', () => {
    const now = new Date('2026-06-29T02:00:00Z');
    const target = new Date('2026-06-29T02:30:00Z');
    expect(buildOnceScheduleFromDatetime(target, now)).toBe('in 1800s');
  });
  it('buildDailyScheduleFromTime', () => {
    expect(buildDailyScheduleFromTime(9, 0)).toBe('每天9点');
    expect(buildDailyScheduleFromTime(9, 30)).toBe('每天9点30分');
  });
  it('parseDateTimeInputs parses as Asia/Shanghai', () => {
    const d = parseDateTimeInputs('2026-06-29', '14:30');
    expect(d).not.toBeNull();
    expect(d!.toISOString()).toBe('2026-06-29T06:30:00.000Z');
  });
  it('formatDateInputValue / formatTimeInputValue returns Asia/Shanghai', () => {
    const d = new Date('2026-06-29T01:05:00Z'); // 北京时间 09:05
    expect(formatDateInputValue(d)).toBe('2026-06-29');
    expect(formatTimeInputValue(d)).toBe('09:05');
  });
  it('resolveScheduleFromCustomInputs uses Asia/Shanghai', () => {
    const now = new Date('2026-06-29T02:00:00Z'); // 北京时间 10:00
    const ok = resolveScheduleFromCustomInputs('once', '2026-06-29', '10:30', 'every 1h', now);
    expect(ok.ok).toBe(true);
    if (ok.ok) expect(ok.schedule).toBe('in 1800s');
    const daily = resolveScheduleFromCustomInputs('interval', '2026-06-29', '14:30', 'every 1d', now);
    expect(daily.ok).toBe(true);
    if (daily.ok) expect(daily.schedule).toBe('每天14点30分');
  });
});

describe('filteredJobs', () => {
  const jobs: CronJob[] = [
    makeJob({ id: 'a', name: '日报', query: 'news', kind: 'interval', enabled: true, deliver: 'feishu:x' }),
    makeJob({ id: 'b', name: '周报', query: 'weekly', kind: 'interval', enabled: false }),
    makeJob({ id: 'c', name: '一次性提醒', query: 'once', kind: 'once', enabled: true, session_id: 'sess-c' }),
    makeJob({ id: 'd', name: '每天九点简报', query: 'daily brief', kind: 'cron', enabled: true, trigger_type: 'cron', trigger_payload: { hour: 9, minute: 0 } }),
  ];

  it('filter all returns everything', () => {
    __setCronViewForTest({ jobs, filter: 'all' as FilterKey, search: '' });
    expect(filteredJobs()).toHaveLength(4);
  });

  it('filter enabled only', () => {
    __setCronViewForTest({ jobs, filter: 'enabled', search: '' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['a', 'c', 'd']);
  });

  it('filter disabled only', () => {
    __setCronViewForTest({ jobs, filter: 'disabled', search: '' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['b']);
  });

  it('filter interval / once by kind', () => {
    __setCronViewForTest({ jobs, filter: 'once', search: '' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['c']);
    __setCronViewForTest({ jobs, filter: 'interval', search: '' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['a', 'b', 'd']);
  });

  it('search matches name / query / session_id / deliver / id (case-insensitive)', () => {
    __setCronViewForTest({ jobs, filter: 'all', search: 'FEISHU' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['a']); // deliver=feishu:x
    __setCronViewForTest({ jobs, filter: 'all', search: 'sess-c' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['c']); // session_id
    __setCronViewForTest({ jobs, filter: 'all', search: '周报' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['b']); // name
  });

  it('search + filter combine', () => {
    __setCronViewForTest({ jobs, filter: 'enabled', search: 'news' });
    expect(filteredJobs().map((j) => j.id)).toEqual(['a']);
  });
});

describe('isRecurringJob', () => {
  it('treats interval and cron as recurring', () => {
    expect(isRecurringJob(makeJob({ kind: 'interval' }))).toBe(true);
    expect(isRecurringJob(makeJob({ kind: 'cron' }))).toBe(true);
  });
  it('does not treat once as recurring', () => {
    expect(isRecurringJob(makeJob({ kind: 'once' }))).toBe(false);
  });
});

describe('isRecurringSchedule', () => {
  it('recognizes english and chinese recurring schedules', () => {
    expect(isRecurringSchedule('every 30m')).toBe(true);
    expect(isRecurringSchedule('every 1d')).toBe(true);
    expect(isRecurringSchedule('每天9点')).toBe(true);
    expect(isRecurringSchedule('每周一8点')).toBe(true);
    expect(isRecurringSchedule('每小时')).toBe(true);
    expect(isRecurringSchedule('每10分钟')).toBe(true);
    expect(isRecurringSchedule('每30秒')).toBe(true);
  });
  it('rejects one-time schedules', () => {
    expect(isRecurringSchedule('in 30s')).toBe(false);
    expect(isRecurringSchedule('10分钟后')).toBe(false);
    expect(isRecurringSchedule('明天9点')).toBe(false);
  });
});

describe('任务页滚动保持', () => {
  const flush = () => new Promise((r) => setTimeout(r, 0));

  beforeEach(() => {
    document.body.innerHTML = '';
    __resetAllStoresForTest();
    __resetCronListEventsForTest();
    // 清掉 filteredJobs 用例遗留的 view 状态，避免列表被过滤为空。
    __setCronViewForTest({ jobs: [], filter: 'all', search: '' });
    vi.clearAllMocks();
    api.cronJobs.mockResolvedValue({ jobs: [] });
    api.cronJobDetail.mockResolvedValue({ runs: [] });
  });

  it('点击任务行后列表滚动位置保持，切换筛选后归零', async () => {
    state.backendConnected = true;
    api.cronJobs.mockResolvedValue({ jobs: [makeJob({ id: 'j1', name: '任务一' })] });
    document.body.innerHTML = '<button data-tab="cron"></button><div id="cron-page-root"></div>';
    const disposeCronTab = bindCronTab();
    document.querySelector('[data-tab="cron"]')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await flush();
    await flush();

    const scrollEl = () => document.querySelector<HTMLElement>('.cron-list__scroll');
    const list = scrollEl();
    expect(list).not.toBeNull();
    if (list) list.scrollTop = 321;

    document.querySelector('.cron-row')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await flush();
    await flush();
    expect(scrollEl()?.scrollTop).toBe(321);

    document.querySelector('[data-filter="enabled"]')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(scrollEl()?.scrollTop).toBe(0);
    disposeCronTab();
  });
});
