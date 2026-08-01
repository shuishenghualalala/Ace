/**
 * 通用时长格式化与运行中耗时 hook。
 *
 * 设计原则：
 * - 保持极简，只放与「耗时」相关的纯函数和 hook；
 * - 与 toolResult / 业务 UI 解耦，方便 ToolCallCard、AgentProcessTimeline 共用。
 */

import { useEffect, useState } from "react";

/** 把毫秒格式化成中文短时长。 */
export function formatDuration(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  if (s < 60) return `${s.toFixed(1)}秒`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem > 0 ? `${m}分${rem.toFixed(0)}秒` : `${m}分`;
}

/**
 * 对仍处于运行中的任务返回实时耗时（毫秒）。
 * 完成后返回最后一次已知的 duration；若都未提供则返回 undefined。
 */
export function useElapsed(params: {
  active: boolean;
  startedAt?: number;
  duration?: number;
  intervalMs?: number;
}): number | undefined {
  const { active, startedAt, duration, intervalMs = 200 } = params;
  // startedAt 非法（如 SSR 测试传 0）时退到 duration，避免显示超大时长。
  const liveBase = active && startedAt != null && startedAt > 0 ? Date.now() - startedAt : undefined;
  const base = liveBase ?? duration;
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!active || !startedAt || startedAt <= 0) return undefined;
    const id = setInterval(() => setTick((v) => v + 1), intervalMs);
    return () => clearInterval(id);
  }, [active, startedAt, intervalMs]);

  // tick 仅用于触发重渲染；实际值在每次渲染时从 Date.now() 重新计算。
  return base != null ? base : undefined;
}
