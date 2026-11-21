import { expect, it } from "vitest";
import { groupMessagesIntoTurns } from "./chatTurns";

it("does not attach a known new request to the previous assistant reply before its history arrives", () => {
  const before = { id: "earlier", role: "assistant" as const, text: "Earlier reply", requestId: "normal" };
  const status = { id: "running", role: "status" as const, text: "Goal accepted", requestId: "goal-owned" };
  const progress = { id: "progress", role: "assistant" as const, text: "Work continued", requestId: "goal-owned" };
  expect(groupMessagesIntoTurns([before, status, progress])).toEqual([
    { kind: "agent", turnId: "earlier", messages: [before] },
    { kind: "agent", turnId: "running", messages: [status, progress] },
  ]);
});
