import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { Chunk } from "../types";
import { reviewFromSnapshot, WebPlanDecisions } from "./planDecisions";

const sid = "s";
const firstStore = "1".repeat(32);
const secondStore = "2".repeat(32);
const snapshot = {
  session_id: sid, plan: "# reviewed", plan_file: "plan.md", version: "v1", phase: "review", status: "pending",
  has_plan: true, active: true, awaiting_approval: true,
};
const draft = reviewFromSnapshot(snapshot);
const legacy = { ...snapshot, version: undefined };
const frame = (kind: Chunk["kind"], body: Chunk["body"], request_id?: string): Chunk =>
  ({ kind, body, session_id: sid, request_id, is_final: false, sequence: 0 });
const capability = (store_id: string) => frame("send_receipt_capability", { version: 1, plan_receipts: 1, store_id });
const send = vi.fn((_p: object) => true);
const notify = vi.fn();
let plans: WebPlanDecisions;

beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); plans = new WebPlanDecisions(send); });
afterEach(() => { plans.dispose(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("Web Plan decision delivery", () => {
  it("never downgrades a versioned review and correlates direct receipts with the original store and IDs", () => {
    plans.submit("plan_approve", draft, sid, "agent", "default", notify);
    expect(send).not.toHaveBeenCalled();
    expect(notify).toHaveBeenLastCalledWith({ notice: "unavailable" });
    plans.receive(capability(firstStore));
    plans.submit("plan_approve", draft, sid, "agent", "default", notify);
    const command = send.mock.calls[0][0] as { request_id: string; submission_id: string };
    expect(command).toMatchObject({ expected_version: "v1", action: "plan_approve" });
    const receipt = frame("interaction_receipt", {
      type: "plan", submission_id: command.submission_id, status: "applied", current_version: "v2",
      plan_state: { ...snapshot, version: "v2", phase: "approved", active: false }, execution_request_id: command.request_id,
    }, command.request_id);
    plans.receive({ ...receipt, gateway_sequence: 1 });
    plans.receive({ ...receipt, request_id: "other" });
    plans.receive({ ...receipt, body: { ...receipt.body, submission_id: "other" } });
    expect(notify).toHaveBeenLastCalledWith({ notice: "confirming" });
    plans.disconnect();
    plans.receive(capability(secondStore));
    plans.receive(receipt);
    expect(send).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenLastCalledWith({ notice: "uncertain" });
    plans.receive(capability(firstStore));
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ action: "plan_receipt_query", ...commandIds(command) }));
    plans.receive(receipt);
    expect(notify).toHaveBeenLastCalledWith({ snapshot: receipt.body.plan_state, notice: undefined });
    expect(send).toHaveBeenLastCalledWith({ action: "send_receipt_query", session_id: sid, request_id: command.request_id });
  });

  it("queries a lost decision and reuses its identifiers after an explicit unknown result", async () => {
    plans.receive(capability(firstStore));
    plans.submit("plan_reject", draft, sid, "agent", "default", notify);
    const command = send.mock.calls[0][0] as { request_id: string; submission_id: string };
    await vi.advanceTimersByTimeAsync(4000);
    expect(notify).toHaveBeenLastCalledWith({ notice: "uncertain" });
    plans.submit("check", draft, sid, "agent", "default", notify);
    plans.receive(frame("interaction_receipt", { type: "plan", status: "unknown", submission_id: command.submission_id }, command.request_id));
    expect(notify).toHaveBeenLastCalledWith({ notice: "unknown" });
    plans.submit("plan_reject", draft, sid, "agent", "default", notify);
    expect(send).toHaveBeenLastCalledWith(command);
  });

  it("expires a versionless card before sending if the server acquired a version or changed the file", async () => {
    vi.spyOn(api, "sessionPlan").mockResolvedValueOnce(snapshot).mockResolvedValueOnce({ ...legacy, plan_file: "other.md" });
    for (let i = 0; i < 2; i++) {
      plans.submit("plan_approve", reviewFromSnapshot(legacy), sid, "agent", "default", notify);
      await vi.advanceTimersByTimeAsync(0);
      expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({ notice: "expired" }));
      expect(send).not.toHaveBeenCalled();
    }
  });

  it("confirms a legacy approval only against the matching recorded user request", async () => {
    const approved = { ...legacy, phase: "approved", status: "approved", active: false, awaiting_approval: false };
    vi.spyOn(api, "sessionPlan").mockResolvedValueOnce(legacy).mockResolvedValue(approved);
    vi.spyOn(api, "sessionStatus").mockResolvedValue({ session_id: sid, active_request_id: "other", live: "running", last_status: "", last_error: "", queue_depth: 0 });
    const tail = vi.spyOn(api, "historyTail").mockResolvedValue({ items: [{ role: "user", request_id: "other" }] });
    const shown = reviewFromSnapshot(legacy);
    plans.submit("plan_approve", shown, sid, "agent", "default", notify);
    await vi.advanceTimersByTimeAsync(1800);
    expect(notify).toHaveBeenLastCalledWith({ notice: "uncertain" });
    expect(plans.overlay(sid, reviewFromSnapshot(approved))).toMatchObject({ status: "pending", decisionNotice: "uncertain" });
    const command = send.mock.calls[0][0] as { request_id: string };
    tail.mockResolvedValue({ items: [{ role: "assistant", request_id: command.request_id }] });
    plans.submit("check", shown, sid, "agent", "default", notify);
    await vi.advanceTimersByTimeAsync(1800);
    expect(notify).toHaveBeenLastCalledWith({ notice: "uncertain" });
    tail.mockResolvedValue({ items: [{ role: "user", request_id: command.request_id }] });
    plans.submit("check", shown, sid, "agent", "default", notify);
    await vi.advanceTimersByTimeAsync(0);
    expect(notify).toHaveBeenLastCalledWith({ snapshot: approved });
    expect(send).toHaveBeenCalledTimes(1);
  });
});

function commandIds(command: { request_id: string; submission_id: string }) {
  return { request_id: command.request_id, submission_id: command.submission_id };
}
