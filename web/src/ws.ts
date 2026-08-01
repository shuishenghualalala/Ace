import type { Chunk } from "./types";

/** 自动重连的对话 WebSocket。沿用后端 /ws 协议：发 {query,session_id,mode}，收 Chunk。 */
export class ChatSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private connectCount = 0; // 临时诊断：连接序号，排查断连来源

  constructor(
    private onChunk: (c: Chunk) => void,
    private onStatus: (open: boolean) => void,
    private onOpen?: () => void,
  ) {}

  connect() {
    this.connectCount += 1;
    const n = this.connectCount;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws.onopen = () => {
      this.onStatus(true);
      this.onOpen?.();
      console.log("[ws] connected", { n });
    };
    this.ws.onclose = (e) => {
      this.onStatus(false);
      // 临时诊断：close code/reason/wasClean 用来判断是哪层心跳触发的断连
      console.warn("[ws] closed", {
        n,
        code: e.code,
        reason: e.reason || "(empty)",
        wasClean: e.wasClean,
      });
      if (!this.closed) setTimeout(() => this.connect(), 1500);
    };
    this.ws.onerror = (e) => {
      console.warn("[ws] error", { n, error: e });
    };
    this.ws.onmessage = (e) => {
      const payload = JSON.parse(e.data);
      if (payload?.kind === "ping") {
        queueMicrotask(() => this.send({ kind: "pong" }));
        return;
      }
      // 临时诊断：排查过程时间线文本错乱/换行丢失
      const body = payload?.body || {};
      const textPreview = body.text ? String(body.text).slice(0, 60).replace(/\n/g, "\\n") : "";
      console.log(
        "[ws chunk]",
        payload.kind,
        `seq=${payload.gateway_sequence ?? "-"}`,
        `sid=${payload.session_id ?? "-"}`,
        `text=${textPreview}`,
        body.message ? `msg=${String(body.message).slice(0, 40)}` : "",
      );
      this.onChunk(payload as Chunk);
    };
  }

  send(payload: object): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  stop(sessionId: string): boolean {
    return this.send({ action: "stop", session_id: sessionId });
  }

  /** 订阅一个或多个会话，用于断线重连后恢复后台流式推送。 */
  subscribe(sessionIds: string[], lastGatewaySequences?: Record<string, number>): boolean {
    const sessions = Array.from(new Set(sessionIds.map((id) => id.trim()).filter(Boolean)));
    if (sessions.length === 0) return true;
    return this.send({
      action: "subscribe",
      session_id: sessions[0],
      sessions,
      last_gateway_sequences: lastGatewaySequences,
    });
  }

  /** 协作式中断：让当前回复在安全点优雅停止，保留已生成的历史。 */
  interrupt(sessionId: string): boolean {
    return this.send({ action: "interrupt", session_id: sessionId });
  }

  /** 实时引导：向运行中的回复注入补充指令，不打断当前生成。 */
  steer(sessionId: string, text: string): boolean {
    return this.send({ action: "steer", session_id: sessionId, text });
  }

  /** 进入 Plan 模式（只读探索→写计划→审批后执行）。 */
  planEnter(sessionId: string): boolean {
    return this.send({ action: "plan_enter", session_id: sessionId });
  }

  /** 批准计划：退出只读并自动起一轮执行。 */
  planApprove(sessionId: string, mode: string, workspaceId: string): boolean {
    return this.send({
      action: "plan_approve",
      session_id: sessionId,
      mode,
      workspace_id: workspaceId,
    });
  }

  /** 拒绝计划：保留 Plan 模式继续完善。 */
  planReject(sessionId: string): boolean {
    return this.send({ action: "plan_reject", session_id: sessionId });
  }

  /** 拒绝计划并退出 Plan 模式。 */
  planRejectAndExit(sessionId: string): boolean {
    return this.send({ action: "plan_reject_and_exit", session_id: sessionId });
  }

  /** 退出 Plan 模式，切回普通 Craft。 */
  planExit(sessionId: string): boolean {
    return this.send({ action: "plan_exit", session_id: sessionId });
  }

  /** 提交追问选择框的答案。 */
  followupAnswer(sessionId: string, questionId: string, answers: { question_id: string; answers: string[] }[]): boolean {
    return this.send({
      action: "followup_answer",
      session_id: sessionId,
      question_id: questionId,
      answers,
    });
  }

  /** 取消追问选择框：通知后端放弃等待。 */
  followupCancel(sessionId: string, questionId: string): boolean {
    return this.send({
      action: "followup_cancel",
      session_id: sessionId,
      question_id: questionId,
    });
  }

  dispose() {
    this.closed = true;
    this.ws?.close();
  }
}
