import { api } from "../api";
import type { Chunk, Mode, PlanDecision, PlanDecisionNotice, PlanReview } from "../types";

type Snapshot = Awaited<ReturnType<typeof api.sessionPlan>>;
type Result = { notice?: PlanDecisionNotice; snapshot?: Snapshot };
type Action = Exclude<PlanDecision, "check">;
type Command = {
  action: Action;
  session_id: string;
  mode: Mode;
  workspace_id: string;
  request_id: string;
  submission_id: string;
  expected_version?: string;
};
type Pending = {
  review: PlanReview;
  command: Command;
  store?: string;
  notify(result: Result): void;
  timer?: ReturnType<typeof setTimeout>;
  checking?: boolean;
  notice: PlanDecisionNotice;
};

const id = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

export function samePlan(a: PlanReview, b: PlanReview) {
  return a.version === b.version && a.planFile === b.planFile && a.plan === b.plan;
}

export function reviewFromSnapshot(row: Partial<Snapshot> & { empty?: boolean }): PlanReview {
  const status = row.status as PlanReview["status"];
  return {
    plan: row.plan ?? "", planFile: row.plan_file ?? "", version: row.version, phase: row.phase,
    status: row.empty ? "empty" : status ?? (row.active === false ? "readonly" : row.awaiting_approval === false ? "editing" : "pending"),
    empty: row.empty,
  };
}

function isSnapshot(row: unknown, sid: string): row is Snapshot {
  if (!row || typeof row !== "object") return false;
  const data = row as Partial<Snapshot>;
  return data.session_id === sid && typeof data.plan === "string" && typeof data.plan_file === "string"
    && typeof data.active === "boolean" && typeof data.awaiting_approval === "boolean" && typeof data.has_plan === "boolean";
}

/** One socket lifetime owns the negotiated store and all of its outstanding decisions. */
export class WebPlanDecisions {
  private store?: string;
  private generation = 0;
  private pending = new Map<string, Pending>();
  private approvals = new Map<string, string>();
  private executionChecks = new Map<string, string>();

  constructor(private send: (frame: object) => boolean) {}

  private current(p: Pending) { return this.pending.get(p.command.session_id) === p; }

  private publish(p: Pending, notice: PlanDecisionNotice) {
    if (!this.current(p)) return;
    p.notice = notice;
    p.notify({ notice });
  }

  overlay(sid: string, review: PlanReview): PlanReview {
    const p = this.pending.get(sid);
    if (!p || !samePlan(p.review, review)) return review;
    return { ...p.review, decisionNotice: p.notice };
  }

  disconnect() {
    this.generation++;
    this.store = undefined;
    this.executionChecks.clear();
    for (const p of this.pending.values()) {
      clearTimeout(p.timer);
      this.publish(p, "uncertain");
    }
  }

  observeExecution(sid: string, requestId: string, final: boolean) {
    if (!requestId || this.approvals.get(sid) !== requestId) return false;
    if (final) this.approvals.delete(sid);
    return true;
  }

  forget(sid: string) {
    this.approvals.delete(sid);
    clearTimeout(this.pending.get(sid)?.timer);
    this.pending.delete(sid);
    this.executionChecks.delete(sid);
  }

  dispose() {
    for (const p of this.pending.values()) clearTimeout(p.timer);
    this.pending.clear();
    this.approvals.clear();
    this.disconnect();
  }

  resumeExecution(sid: string, snapshot: Snapshot) {
    if (!snapshot.version || !snapshot.execution_request_id || !["accepted", "running", "uncertain"].includes(snapshot.execution_status ?? "")) return;
    this.executionChecks.set(sid, snapshot.execution_request_id);
    if (this.store && this.send({ action: "send_receipt_query", session_id: sid, request_id: snapshot.execution_request_id })) {
      this.executionChecks.delete(sid);
    }
  }

  receive(c: Chunk): boolean {
    if (c.kind === "send_receipt_capability") {
      const store = c.body.store_id;
      this.store = c.body.version === 1 && c.body.plan_receipts === 1 && typeof store === "string" && /^[0-9a-f]{32}$/.test(store) ? store : undefined;
      if (this.store) {
        for (const p of this.pending.values()) if (p.store === this.store && p.notice !== "unknown") this.query(p);
        for (const [sid, requestId] of this.executionChecks) {
          if (this.send({ action: "send_receipt_query", session_id: sid, request_id: requestId })) this.executionChecks.delete(sid);
        }
      }
      return true;
    }
    if (c.kind === "send_receipt") return true;
    if (c.kind !== "interaction_receipt" || c.body.type !== "plan") return false;
    const sid = c.session_id;
    const p = sid ? this.pending.get(sid) : undefined;
    if (!sid || !p || !p.store || p.store !== this.store || c.is_final || c.gateway_sequence !== undefined
      || c.request_id !== p.command.request_id || c.body.submission_id !== p.command.submission_id) return true;
    const status = c.body.status;
    if (!["applied", "expired", "rejected", "unknown", "uncertain", "unavailable"].includes(status)) return true;
    clearTimeout(p.timer);
    const row = c.body.plan_state;
    const snapshot = isSnapshot(row, sid) && typeof row.version === "string" && row.version === c.body.current_version ? row : undefined;
    if ((status === "applied" && !snapshot) || status === "uncertain" || status === "unavailable") {
      this.publish(p, "uncertain");
    } else if (status === "unknown") {
      this.publish(p, "unknown");
    } else {
      this.pending.delete(sid);
      if (status !== "applied") this.approvals.delete(sid);
      p.notify({ snapshot, notice: status === "applied" ? undefined : status });
      if (status === "applied" && c.body.execution_request_id === p.command.request_id) {
        this.send({ action: "send_receipt_query", session_id: sid, request_id: p.command.request_id });
      }
    }
    return true;
  }

  submit(action: PlanDecision, review: PlanReview, sid: string, mode: Mode, workspaceId: string, notify: Pending["notify"]) {
    let old = this.pending.get(sid);
    if (old?.store && this.store && old.store !== this.store && !samePlan(old.review, review)) {
      this.forget(sid);
      old = undefined;
    }
    if (action === "check") {
      if (old) this.query(old);
      return;
    }
    if (old && old.notice !== "unknown") {
      this.query(old);
      return;
    }
    if (review.version && !this.store) { notify({ notice: "unavailable" }); return; }
    const previous = old && old.store === this.store && old.command.action === action && samePlan(old.review, review)
      && old.command.mode === mode && old.command.workspace_id === workspaceId ? old.command : undefined;
    const p: Pending = {
      review, notify, notice: "confirming", store: review.version ? this.store : undefined,
      command: previous ?? {
        action, session_id: sid, mode, workspace_id: workspaceId, request_id: "plan_" + id(), submission_id: id(), expected_version: review.version,
      },
    };
    this.pending.set(sid, p);
    this.publish(p, "confirming");
    if (!review.version) { void this.submitLegacy(p); return; }
    if (!this.send(p.command)) {
      this.pending.delete(sid);
      notify({ notice: "unavailable" });
      return;
    }
    if (action === "plan_approve") this.approvals.set(sid, p.command.request_id);
    p.timer = setTimeout(() => this.query(p), 2000);
  }

  private query(p: Pending) {
    if (!this.current(p)) return;
    if (!p.store) { void this.checkLegacy(p); return; }
    clearTimeout(p.timer);
    const { session_id, request_id, submission_id, expected_version } = p.command;
    if (p.store !== this.store || !this.send({ action: "plan_receipt_query", session_id, request_id, submission_id, expected_version })) {
      this.publish(p, "uncertain");
      return;
    }
    this.publish(p, "confirming");
    p.timer = setTimeout(() => this.publish(p, "uncertain"), 2000);
  }

  private async submitLegacy(p: Pending) {
    const generation = this.generation;
    const { action, session_id, mode, workspace_id, request_id } = p.command;
    p.checking = true;
    const row = await api.sessionPlan(session_id, AbortSignal.timeout(6000)).catch(() => undefined);
    if (!this.current(p)) return;
    p.checking = false;
    if (generation !== this.generation || !row || !isSnapshot(row, session_id)) {
      this.pending.delete(session_id);
      p.notify({ notice: "unavailable" });
      return;
    }
    if (row.version || !row.active || !row.awaiting_approval || !samePlan(p.review, reviewFromSnapshot(row))) {
      this.pending.delete(session_id);
      p.notify({ notice: "expired", snapshot: row });
      return;
    }
    if (!this.send({ action, session_id, mode, workspace_id, request_id })) {
      this.pending.delete(session_id);
      p.notify({ notice: "unavailable" });
      return;
    }
    if (action === "plan_approve") this.approvals.set(session_id, request_id);
    await this.checkLegacy(p);
  }

  private async checkLegacy(p: Pending) {
    if (p.checking || !this.current(p)) return;
    p.checking = true;
    this.publish(p, "confirming");
    const { action, session_id, request_id } = p.command;
    const phase = { plan_approve: "approved", plan_reject: "revising", plan_reject_and_exit: "rejected" }[action];
    const signal = AbortSignal.timeout(6000);
    try {
      for (let i = 0; i < 10 && !signal.aborted && this.current(p); i++) {
        const row = await api.sessionPlan(session_id, signal).catch(() => undefined);
        if (row && isSnapshot(row, session_id) && !row.version && samePlan(p.review, reviewFromSnapshot(row))
          && (row.phase === phase || row.status === phase || (!row.phase && row.status === "readonly" && phase === "approved"))) {
          let confirmed = action !== "plan_approve";
          if (!confirmed) {
            const status = await api.sessionStatus(session_id, signal).catch(() => undefined);
            confirmed = status?.active_request_id === request_id;
            if (!confirmed) {
              const history = await api.historyTail(session_id, signal).catch(() => undefined);
              confirmed = history?.items.some((item) => item.role === "user" && item.request_id === request_id) ?? false;
            }
          }
          if (confirmed && this.current(p)) {
            this.pending.delete(session_id);
            p.notify({ snapshot: row });
            return;
          }
        }
        if (i < 9) await new Promise((resolve) => setTimeout(resolve, 150));
      }
      this.publish(p, "uncertain");
    } finally { p.checking = false; }
  }
}
