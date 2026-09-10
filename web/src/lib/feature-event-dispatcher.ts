import type {
  Chunk,
  SessionStatus,
  ToolCallInfo,
  TurnFileChangeSummary,
  UiMessage,
  WikiIngestProgress,
} from "../types";

/** 有序 delta 片段（用于聚合记账）。 */
export interface DeltaSpan {
  start: number;
  end: number;
  text: string;
}

/** 单会话聚合记账。 */
export interface Bookkeeping {
  toolMap: Map<string, ToolCallInfo>;
  assistantId: string | null;
  turnStartedAt: number | null;
  awaitingAssistantAfterTool: boolean;
  deltaSpans: DeltaSpan[];
  legacyDeltaText: string;
  hadTeamInternal: boolean;
  fileChanges: TurnFileChangeSummary[];
  fileChangeSignatures: Record<string, string>;
  prevTurnFileSignature: Record<string, string>;
}

/** Feature Event handler 执行上下文。 */
export interface FeatureEventContext {
  sessionId: string;
  now: number;
  startLocalTurn(): number;
  newId(): string;
  book: Bookkeeping;
  messages: UiMessage[];
}

/** Feature Event handler 返回的效果描述。 */
export interface FeatureEventEffect {
  /** 更新消息列表（纯函数，按会话应用）。 */
  messages?(prev: UiMessage[]): UiMessage[];
  /** 更新 Wiki ingest 进度。 */
  wikiProgress?: WikiIngestProgress;
  /** 触发 Wiki 数据变更广播。 */
  wikiChanged?: unknown[];
  /** 会话运行状态提示。 */
  statusHint?: SessionStatus;
  /** 队列提示文案。 */
  queueHint?: string;
  /** 标记本轮出现过 team_internal 帧。 */
  hadTeamInternal?: boolean;
}

export interface FeatureEventRegistration {
  feature: string;
  event: string;
  version: number;
  handler: FeatureEventHandler;
}

export type FeatureEventHandler = (
  payload: unknown,
  ctx: FeatureEventContext,
) => FeatureEventEffect | null | undefined;

/**
 * Feature Event 统一分派层。
 *
 * 与桌面端语义对齐：按 (feature, event, version) 注册 handler；
 * 重复注册冲突失败；disposer 幂等；未命中安全忽略并 console.warn。
 */
export class FeatureEventRegistry {
  private readonly entries = new Map<string, FeatureEventRegistration>();

  private static key(feature: string, event: string, version: number): string {
    return `${feature}:${event}:${version}`;
  }

  register(reg: FeatureEventRegistration): () => void {
    const key = FeatureEventRegistry.key(reg.feature, reg.event, reg.version);
    if (this.entries.has(key)) {
      throw new Error(
        `Feature event handler already registered: ${reg.feature}/${reg.event}@${reg.version}`,
      );
    }
    this.entries.set(key, reg);
    let disposed = false;
    return () => {
      if (disposed) return;
      disposed = true;
      // 幂等：只删除自己这个注册，避免误删同 key 的新注册。
      if (this.entries.get(key) === reg) this.entries.delete(key);
    };
  }

  dispatch(
    feature: string,
    event: string,
    version: number,
    payload: unknown,
    ctx: FeatureEventContext,
  ): FeatureEventEffect | null {
    const key = FeatureEventRegistry.key(feature, event, version);
    const reg = this.entries.get(key);
    if (!reg) {
      console.warn(
        `[feature-event] unhandled event: feature=${feature} event=${event} version=${version}`,
      );
      return null;
    }
    return reg.handler(payload, ctx) ?? null;
  }
}

export interface CompatFeatureEvent {
  feature: string;
  event: string;
  version: number;
  payload: unknown;
}

/** 旧业务帧映射到命名空间事件。 */
export function compatFeatureEvent(chunk: Chunk): CompatFeatureEvent | null {
  switch (chunk.kind) {
    case "wiki_cards":
      return { feature: "wiki", event: "cards", version: 1, payload: chunk.body };
    case "wiki_ingest_progress":
      return { feature: "wiki", event: "ingest_progress", version: 1, payload: chunk.body };
    case "wiki_changed":
      return { feature: "wiki", event: "changed", version: 1, payload: chunk.body };
    case "team_internal":
      return { feature: "team", event: "internal_message", version: 1, payload: chunk.body };
    case "workflow_progress":
      return { feature: "kanban", event: "workflow_progress", version: 1, payload: chunk.body };
    default:
      return null;
  }
}

export interface ParsedFeatureEvent {
  feature: string;
  event: string;
  version: number;
  payload: unknown;
}

/** 解析 feature_event 新帧体。 */
export function parseFeatureEventBody(body: unknown): ParsedFeatureEvent | null {
  if (!body || typeof body !== "object") return null;
  const record = body as Record<string, unknown>;
  const feature = String(record.feature || "").trim();
  const event = String(record.event || "").trim();
  const versionRaw = record.version;
  const version =
    typeof versionRaw === "number" && Number.isFinite(versionRaw) ? versionRaw : 1;
  if (!feature || !event) return null;
  return {
    feature,
    event,
    version,
    payload: record.payload ?? {},
  };
}
