/** Work API 使用 Unix 秒；视觉 fixture 和 DOM Date 使用毫秒。 */
export function epochMilliseconds(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.abs(value) < 1_000_000_000_000 ? value * 1000 : value;
}

/** 将毫秒时间戳规范为 Work API 使用的 Unix 秒。 */
export function epochSeconds(value: number): number {
  if (!Number.isFinite(value)) return value;
  return Math.abs(value) >= 1_000_000_000_000 ? value / 1000 : value;
}

/** 格式化「N 分钟前」相对时间；timestamp 可为秒或毫秒。 */
export function relativeTime(timestamp: number, now = Date.now()): string {
  if (!timestamp) return '—';
  const timeMs = epochMilliseconds(timestamp);
  const diff = epochMilliseconds(now) - timeMs;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return new Date(timeMs).toLocaleDateString('zh-CN');
}
