import { describe, expect, it } from "vitest";
import { formatProcessStartTime, processTimingLabel, processSummaryLabel } from "./processDisplay";

describe("process timing", () => {
  it("shows duration with start time only", () => {
    const start = new Date("2026-07-10T16:29:37+08:00").getTime();
    const message = {
      turnStartedAt: start,
      turnDurationMs: 9_000,
      timestamp: start + 9_000,
    };

    expect(formatProcessStartTime(message)).toBe("16:29:37");
    expect(processTimingLabel(message, 63_000)).toBe("1m 3s · 16:29:37");
  });

  it("keeps completed duration and range consistent", () => {
    const start = new Date("2026-07-10T16:29:37+08:00").getTime();
    const message = { turnStartedAt: start, turnDurationMs: 9_000 };

    expect(processTimingLabel(message, 9_000)).toBe("9s · 16:29:37");
  });

  it("uses one process summary rule for Crew and team agents", () => {
    expect(processSummaryLabel({ isStreaming: true, toolCount: 0, commandCount: 0, hasThinking: true })).toBe("思考中");
    expect(processSummaryLabel({ isStreaming: false, toolCount: 2, commandCount: 2, hasThinking: true })).toBe("运行 2 个命令");
  });
});
