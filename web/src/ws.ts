import type { Chunk } from "./types";

const WS_PROTOCOL_VERSION = 1;
const PROTOCOL_NONCE_RE = /^[A-Za-z0-9._~-]{16,128}$/;

function secureProtocolNonce(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

export class ClientProtocolIdentity {
  private clientSequence = 0;

  constructor(private readonly nonceFactory: () => string = secureProtocolNonce) {}

  reset(): void {
    this.clientSequence = 0;
  }

  encode(payload: object): string {
    if (
      payload === null
      || Array.isArray(payload)
      || typeof payload !== "object"
      || this.clientSequence >= Number.MAX_SAFE_INTEGER
    ) {
      throw new Error("invalid WebSocket protocol frame");
    }
    const nonce = this.nonceFactory();
    if (!PROTOCOL_NONCE_RE.test(nonce)) {
      throw new Error("invalid WebSocket protocol nonce");
    }
    this.clientSequence += 1;
    return JSON.stringify({
      ...payload,
      protocol_version: WS_PROTOCOL_VERSION,
      client_sequence: this.clientSequence,
      nonce,
    });
  }
}

/** 自动重连的对话 WebSocket。沿用后端 /ws 协议：发 {query,session_id,mode}，收 Chunk。 */
export class ChatSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private connectCount = 0; // 临时诊断：连接序号，排查断连来源
  private readonly protocolIdentity = new ClientProtocolIdentity();

  constructor(
    private onChunk: (c: Chunk) => void,
    private onStatus: (open: boolean) => void,
    private onOpen?: () => void,
  ) {}

  connect() {
    this.connectCount += 1;
    const n = this.connectCount;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.protocolIdentity.reset();
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
      this.onChunk(payload as Chunk);
    };
  }

  send(payload: object): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(this.protocolIdentity.encode(payload));
        return true;
      } catch {
        // A failed enqueue may have consumed a sequence locally. Close the
        // socket so reconnect starts a fresh sequence instead of continuing
        // with a gap the Gateway must reject.
        try {
          this.ws.close(4002, "Protocol identity failed");
        } catch {
          // onclose/reconnect remains the only recovery path.
        }
      }
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
