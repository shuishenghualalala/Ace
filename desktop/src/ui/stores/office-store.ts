/**
 * 办公快照单一数据源：邮件 / 待办 / 日程 / 会议 四份后台定时刷新快照。
 *
 * 与 work-store 同源思路：模块级缓存 + 加载方法 + 订阅。四份快照来自 GET
 * /api/{mail,todo,schedule,meeting}/latest（后台 MailTodoRefresher 只读缓存，不触发外部
 * 调用）。前端按 ≤ 后端刷新间隔轮询；断线重连重拉。单份失败保留旧值不覆盖、不阻塞其余。
 *
 * 不复制 fetch：所有请求经 officeApi（复用 gatewayFetch）。
 */

import { createStore, type Store } from '../reducers/store-bus';
import {
  officeApi,
  type OfficeSnapshot,
  type MailSearchData,
  type TodoData,
  type ScheduleData,
  type MeetingData,
} from '../backend-client';

export interface OfficeSnapshots {
  mail: OfficeSnapshot<MailSearchData> | null;
  todo: OfficeSnapshot<TodoData> | null;
  schedule: OfficeSnapshot<ScheduleData> | null;
  meeting: OfficeSnapshot<MeetingData> | null;
  loading: boolean;
  error: string | null;
}

export const officeStore: Store<OfficeSnapshots> = createStore<OfficeSnapshots>(
  {
    mail: null,
    todo: null,
    schedule: null,
    meeting: null,
    loading: false,
    error: null,
  },
  'office',
);

/** 单份快照拉取：失败返回 null（保留旧值），不抛、不阻塞其余三份。 */
async function loadOne<T>(
  label: string,
  fn: () => Promise<OfficeSnapshot<T>>,
): Promise<OfficeSnapshot<T> | null> {
  try {
    return await fn();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    officeStore.set({ error: `${label}: ${msg}` });
    return null;
  }
}

/** 并发拉取四份快照；失败项保留旧值。 */
export async function loadOfficeSnapshots(): Promise<void> {
  officeStore.set({ loading: true, error: null });
  const [mail, todo, schedule, meeting] = await Promise.all([
    loadOne('mail', () => officeApi.mailLatest()),
    loadOne('todo', () => officeApi.todoLatest()),
    loadOne('schedule', () => officeApi.scheduleLatest()),
    loadOne('meeting', () => officeApi.meetingLatest()),
  ]);
  officeStore.set({
    mail: mail ?? officeStore.get().mail,
    todo: todo ?? officeStore.get().todo,
    schedule: schedule ?? officeStore.get().schedule,
    meeting: meeting ?? officeStore.get().meeting,
    loading: false,
  });
}

const DEFAULT_POLL_MS = 60_000;
const MIN_POLL_MS = 10_000;
let pollTimer: ReturnType<typeof setInterval> | null = null;

/** 启动快照轮询（≤ 后端 300s 默认间隔）。幂等：重复调用不重复注册。 */
export function startOfficePolling(intervalMs = DEFAULT_POLL_MS): void {
  if (pollTimer !== null) return;
  void loadOfficeSnapshots();
  pollTimer = setInterval(() => {
    void loadOfficeSnapshots();
  }, Math.max(MIN_POLL_MS, intervalMs));
}

/** 停止快照轮询；保留已有快照以便快速切回。 */
export function stopOfficePolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

let statusUnsubscribe: (() => void) | null = null;
let wasConnected = false;

/**
 * 订阅后端状态跃迁：disconnected->connected 时立即重拉快照（断线重拉）。
 * 返回 dispose 函数，解除订阅并重置内部标记。
 */
export function connectOfficeRefetch(): () => void {
  if (statusUnsubscribe) return statusUnsubscribe;
  statusUnsubscribe =
    window.Crew?.onBackendStatus?.((status: { connected?: boolean } | undefined) => {
      const connected = !!status?.connected;
      if (connected && !wasConnected) {
        void loadOfficeSnapshots().catch(() => {
          /* 重连后首次拉取失败由下一次轮询重试 */
        });
      }
      wasConnected = connected;
    }) ?? null;
  return () => {
    statusUnsubscribe?.();
    statusUnsubscribe = null;
    wasConnected = false;
  };
}
