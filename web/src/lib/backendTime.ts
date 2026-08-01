/** 网关历史 API 的时间字段以 Unix 秒存储；前端 UI 统一用毫秒。 */

export function backendSecondsToMs(seconds: number | undefined | null): number | undefined {
  if (seconds == null || Number.isNaN(seconds)) return undefined;
  return Math.round(seconds * 1000);
}

export function backendDurationToMs(seconds: number | undefined | null): number {
  if (seconds == null || Number.isNaN(seconds)) return 0;
  return Math.round(seconds * 1000);
}
