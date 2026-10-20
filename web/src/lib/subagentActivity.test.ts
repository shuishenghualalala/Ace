import { describe, expect, it } from "vitest";
import type { SubagentListResponse, UiMessage } from "../types";
import {
  mergeSubagentActivities,
  normalizeSubagentItems,
  statusFromSubagentRecord,
} from "./subagentActivity";

const message = (overrides: Partial<UiMessage>): UiMessage => ({
  id: "message-1",
  role: "status",
  text: "子智能体状态",
  ...overrides,
});

describe("subagent activity projection", () => {
  it("normalizes both session-map and session-scoped item shapes", () => {
    expect(normalizeSubagentItems({ parent: [{ subagent_id: "child-1" }] })).toEqual([
      { subagent_id: "child-1" },
    ]);
    expect(normalizeSubagentItems([{ subagent_id: "child-2" }])).toEqual([
      { subagent_id: "child-2" },
    ]);
  });

  it("maps terminal stop reasons before falling back to settled status", () => {
    expect(statusFromSubagentRecord({ status: "settled", stop_reason: "timed_out" })).toBe("timed_out");
    expect(statusFromSubagentRecord({ status: "settled", stop_reason: "error" })).toBe("error");
    expect(statusFromSubagentRecord({ status: "settled", stop_reason: "completed" })).toBe("completed");
    expect(statusFromSubagentRecord({ state: "running" })).toBe("running");
  });

  it("lets live feature lifecycle state override a stale API run", () => {
    const response: SubagentListResponse = {
      ok: true,
      items: [{ subagent_id: "child-1", label: "API label", state: "running", provider: "spawn" }],
      runs: [
        { subagent_id: "child-1", label: "Old label", status: "settled", stop_reason: "completed", created_at: 1 },
        { subagent_id: "child-2", label: "Verifier", status: "settled", stop_reason: "error", created_at: 2 },
      ],
    };
    const activities = mergeSubagentActivities(response, [
      message({
        id: "started-1",
        subagentId: "child-1",
        subagentState: "started",
        subagentLabel: "Live label",
        timestamp: 3,
      }),
    ]);

    expect(activities[0]).toMatchObject({ id: "child-1", label: "Live label", status: "running" });
    expect(activities[1]).toMatchObject({ id: "child-2", label: "Verifier", status: "error" });
  });

  it("keeps a lifecycle-only child visible after its settled event", () => {
    const activities = mergeSubagentActivities(null, [
      message({
        subagentId: "child-only",
        subagentState: "settled",
        subagentLabel: "No API snapshot",
        subagentStopReason: "timed_out",
      }),
    ]);

    expect(activities).toEqual([
      expect.objectContaining({
        id: "child-only",
        label: "No API snapshot",
        status: "timed_out",
      }),
    ]);
  });
});
