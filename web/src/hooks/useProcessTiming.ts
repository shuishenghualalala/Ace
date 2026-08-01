import { useEffect, useState } from "react";
import type { UiMessage } from "../types";
import { processTimingLabel } from "../lib/processDisplay";

type TimedMessage = Pick<UiMessage, "turnStartedAt" | "turnDurationMs" | "timestamp">;

export function useProcessTiming(message: TimedMessage | undefined, isStreaming: boolean) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!isStreaming || message?.turnStartedAt == null) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [isStreaming, message?.turnStartedAt]);

  const durationMs = isStreaming && message?.turnStartedAt != null
    ? Math.max(0, Date.now() - message.turnStartedAt)
    : Math.max(0, message?.turnDurationMs ?? 0);
  return {
    durationMs,
    label: message ? processTimingLabel(message, durationMs) : "",
  };
}
