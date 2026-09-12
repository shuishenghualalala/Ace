/**
 * Feature Event Reducer Registry
 *
 * 桌面端业务事件统一入口：业务事件以 feature_event 帧的 (feature, event, version) 寻址，
 * 核心 Shell 只认插槽；Feature 注册自己的 reducer 消费对应事件，未注册的事件由 registry
 * console.warn 可诊断忽略。
 *
 * 注册时机说明：
 * - Wiki Agent 在 initWikiAgent() 内显式注册；
 * - Dynamic Kanban 在 initKanbanBoard() 内显式注册；
 * - Team Collaboration 在 initTeamCollaborationBoard() 内显式注册；
 * - 各 feature 的 dispose 函数负责撤销注册，registry 提供的 disposer 幂等。
 *
 * 安全行为：未知 feature/event 或版本不匹配时，dispatch 返回 null 并 console.warn 可诊断信息。
 */

import type { ChatMessage, ToolCallInfo } from '../chat-render';
import type { Bookkeeping } from '../state';

export interface MessageUpsert {
  op: 'append' | 'patch' | 'remove';
  messageId?: string;
  message?: ChatMessage;
  patch?: Partial<ChatMessage>;
}

export interface ToolUpsert {
  toolCallId: string;
  name: string;
  uiLabel?: string;
  args?: string;
  result?: string;
  status: ToolCallInfo['status'];
  startedAt: number;
  duration?: number;
  progressText?: string;
  progressHistory?: string[];
}

export type StatusHint = 'running' | 'queued' | 'idle' | 'error';

export interface PromptBreakdown {
  system?: number;
  reminder?: number;
  tools?: number;
}

export interface UsagePayload {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  cached_tokens?: number;
  prompt_breakdown?: PromptBreakdown;
}

export interface FeatureReducerResult {
  messageUpserts: MessageUpsert[];
  toolUpserts: ToolUpsert[];
  replaceBook: Bookkeeping | null;
  statusHint: StatusHint | undefined;
  queueHint: string | undefined;
  replaceMessages?: ChatMessage[];
  turn?: {
    status: number;
    turnDurationMs: number;
    firstTokenMs: number | undefined;
    assistantId: string | null;
    usage?: UsagePayload;
  };
  finalize: boolean;
}

export interface FeatureReducerContext {
  sessionId: string;
  messages: ChatMessage[];
  book: Bookkeeping;
  now: number;
  sequence: number;
}

export type FeatureEventReducer = (payload: unknown, ctx: FeatureReducerContext) => FeatureReducerResult;

export interface FeatureEventRegistration {
  feature: string;
  event: string;
  version: number;
  reducer: FeatureEventReducer;
}

export interface UniqueMessageIdContext {
  messages: ChatMessage[];
  now: number;
  sequence: number;
}

/**
 * 生成会话内唯一的消息 id。
 * 基础形式 `${prefix}-${now}-${sequence}` 在同毫秒连发帧（sequence 恒为 0 的
 * status/tool 等旁路帧）下会撞 id；撞 id 后 patch/render 按 id 匹配会命中
 * 第一条同名消息，表现为内容错位/乱序。已存在同名 id 时追加递增后缀避让。
 */
export function uniqueMessageId(ctx: UniqueMessageIdContext, prefix: string): string {
  const base = `${prefix}-${ctx.now}-${ctx.sequence}`;
  if (!ctx.messages.some((m) => m.id === base)) return base;
  let suffix = 1;
  while (ctx.messages.some((m) => m.id === `${base}-${suffix}`)) suffix += 1;
  return `${base}-${suffix}`;
}

export function emptyFeatureReducerResult(): FeatureReducerResult {
  return {
    messageUpserts: [],
    toolUpserts: [],
    replaceBook: null,
    statusHint: undefined,
    queueHint: undefined,
    finalize: false,
  };
}

export class FeatureEventReducerRegistry {
  private readonly entries = new Map<string, FeatureEventRegistration>();

  private static key(feature: string, event: string, version: number): string {
    return `${feature}:${event}:${version}`;
  }

  register(reg: FeatureEventRegistration): () => void {
    const key = FeatureEventReducerRegistry.key(reg.feature, reg.event, reg.version);
    if (this.entries.has(key)) {
      throw new Error(
        `Feature event reducer already registered: ${reg.feature}/${reg.event}@${reg.version}`,
      );
    }
    this.entries.set(key, reg);
    let disposed = false;
    return () => {
      if (disposed) return;
      disposed = true;
      if (this.entries.get(key) === reg) this.entries.delete(key);
    };
  }

  dispatch(
    feature: string,
    event: string,
    version: number,
    payload: unknown,
    ctx: FeatureReducerContext,
  ): FeatureReducerResult | null {
    const key = FeatureEventReducerRegistry.key(feature, event, version);
    const reg = this.entries.get(key);
    if (!reg) {
      console.warn(
        `[feature-event] unhandled event: feature=${feature} event=${event} version=${version}`,
      );
      return null;
    }
    return reg.reducer(payload, ctx);
  }
}

export const featureEventRegistry = new FeatureEventReducerRegistry();
