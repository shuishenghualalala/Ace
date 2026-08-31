/** 网关历史 API 的时间字段以 Unix 秒存储；前端 UI 统一用毫秒。 */

export function backendSecondsToMs(seconds: number | undefined | null): number | undefined {
  if (seconds == null || Number.isNaN(seconds)) return undefined;
  return Math.round(seconds * 1000);
}

export function backendDurationToMs(seconds: number | undefined | null): number {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return 0;
  const now = Date.now();
  if (
    (seconds >= 1_000_000_000 && seconds <= now / 1000 + 366 * 24 * 60 * 60)
    || (seconds >= 1_000_000_000_000 && seconds <= now + 366 * 24 * 60 * 60 * 1000)
  ) return 0;
  return Math.round(seconds * 1000);
}
