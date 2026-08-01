const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  running: '进行中',
  completed: '已完成',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
  timed_out: '已超时',
};

/** Keep terminal task outcomes distinct in every Desktop task projection. */
export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] || status;
}
