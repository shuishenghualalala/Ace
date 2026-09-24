/** Shared, DOM/Node-free contracts for the developer observation surface. */

export type ObservationCaptureProfile = 'metadata' | 'content_redacted' | string;

export interface ObservationTrace {
  owner_scope: string;
  trace_id: string;
  session_id: string;
  request_id: string;
  source: string;
  started_at_us: number;
  ended_at_us?: number | null;
  status: string;
  quality: string;
  has_error_span: number;
  summary: string;
  attributes: Record<string, unknown>;
  server_total_ms?: number | null;
}

export interface ObservationSpan {
  owner_scope: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  name: string;
  kind: string;
  module: string;
  component: string;
  operation: string;
  feature_id: string;
  started_at_us: number;
  ended_at_us?: number | null;
  duration_ms?: number | null;
  status: string;
  attempt?: number | null;
  attributes: Record<string, unknown>;
  error_type?: string;
  error_message?: string;
}

export interface ObservationEvent {
  owner_scope: string;
  ingest_seq: number;
  event_id: string;
  trace_id: string;
  span_id: string;
  name: string;
  occurred_at_us: number;
  level: string;
  source?: string;
  module: string;
  component: string;
  operation: string;
  feature_id: string;
  message: string;
  status: string;
  attributes: Record<string, unknown>;
}

export interface ObservationPayload {
  owner_scope: string;
  payload_id: string;
  trace_id: string;
  span_id: string;
  stage: string;
  capture_state: string;
  content: unknown;
  redacted_paths: string[];
  truncated_reason: string;
  observed_size?: number | null;
  stored_size?: number | null;
  attributes: Record<string, unknown>;
  created_at_us: number;
}

export interface ObservationTraceBundle {
  trace: ObservationTrace;
  spans: ObservationSpan[];
  events: ObservationEvent[];
  payloads?: ObservationPayload[];
}

export interface ObservationFilter {
  trace_id?: string;
  start_after_us?: number;
  start_before_us?: number;
  status?: string;
  source?: string;
  module?: string;
  component?: string;
  operation?: string;
  feature_id?: string;
  provider?: string;
  model?: string;
  tool?: string;
  service?: string;
  q?: string;
  session_id?: string;
  request_id?: string;
  min_duration_ms?: number;
  min_server_total_ms?: number;
  has_error_span?: boolean;
}

export interface ObservationClientContext {
  trace_id?: string;
  request_id?: string;
  session_id?: string;
  message_id?: string;
  workspace_id?: string;
}

export interface ObservationClientEvent {
  name: 'user.submitted' | 'user.presented' | 'desktop.error' | 'desktop.event';
  trace_id?: string;
  request_id?: string;
  session_id?: string;
  message_id?: string;
  workspace_id?: string;
  attributes?: Record<string, unknown>;
  presentation?: unknown;
}

/** Small FIFO used by renderer boundaries; it never grows with a long session. */
export class BoundedObservationEventQueue {
  private readonly items: ObservationClientEvent[] = [];

  constructor(private readonly capacity = 64) {}

  push(item: ObservationClientEvent): void {
    this.items.push(item);
    while (this.items.length > Math.max(1, this.capacity)) this.items.shift();
  }

  drain(limit = this.items.length): ObservationClientEvent[] {
    return this.items.splice(0, Math.max(0, Math.min(limit, this.items.length)));
  }

  get size(): number { return this.items.length; }
}

let devLaunch = false;

export function isObservationDevLaunch(): boolean { return devLaunch; }

export function setObservationDevLaunch(value: boolean): void { devLaunch = value; }
