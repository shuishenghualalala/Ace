/** Typed client for the developer-only observation APIs. */

import {
  gatewayFetch,
  getJSON,
} from './transport';
import type {
  ObservationCaptureProfile,
  ObservationEvent,
  ObservationFilter,
  ObservationPayload,
  ObservationSpan,
  ObservationTrace,
  ObservationTraceBundle,
} from '../../shared/observability';

export interface ObservationCapabilities {
  available: boolean;
  owner_scope?: string;
  capture_profile?: ObservationCaptureProfile;
  formats?: string[];
}

export interface ObservationStatus {
  available: boolean;
  schema_version?: number;
  traces?: number;
  events?: number;
  payloads?: number;
  latest_ingest_seq?: number;
  writer?: Record<string, unknown>;
}

export interface ObservationTracePage {
  items: ObservationTrace[];
  next_cursor?: string | null;
  has_more: boolean;
}

export interface ObservationExportJob {
  ok?: boolean;
  export_id: string;
  owner_scope: string;
  status: 'running' | 'completed' | 'partial' | 'cancelled' | 'failed' | 'interrupted' | 'expired';
  format: 'json' | 'jsonl' | 'csv';
  count: number;
  partial: boolean;
  partial_reason?: string;
  snapshot_ingest_seq?: number;
  bytes?: number;
  error?: string;
}

function query(params: object): string {
  const entries = Object.entries(params as Record<string, unknown>);
  const values = entries.filter(([, value]) => value !== undefined && value !== null && value !== '');
  const encoded = new URLSearchParams(values.map(([key, value]) => [key, String(value)]));
  const text = encoded.toString();
  return text ? `?${text}` : '';
}

function filterQuery(filter: object = {}): string {
  return query(filter);
}

type RequestSignal = AbortSignal | undefined;
function requestOptions(signal?: RequestSignal): RequestInit {
  return signal ? { signal } : {};
}

export const tracingApi = {
  tracingCapabilities: (signal?: RequestSignal) => getJSON<ObservationCapabilities>('/api/tracing/capabilities', requestOptions(signal)),
  tracingStatus: (signal?: RequestSignal) => getJSON<ObservationStatus>('/api/tracing/status', requestOptions(signal)),
  tracingTraces: (filter: ObservationFilter = {}, limit = 50, cursor?: string | null, signal?: RequestSignal) =>
    getJSON<ObservationTracePage>(`/api/tracing/traces${filterQuery({ ...filter, limit, cursor })}`, requestOptions(signal)),
  tracingTrace: (traceId: string, signal?: RequestSignal) =>
    getJSON<ObservationTraceBundle>(`/api/tracing/traces/${encodeURIComponent(traceId)}`, requestOptions(signal)),
  tracingSpans: (traceId: string, limit = 500, signal?: RequestSignal) =>
    getJSON<{ items: ObservationSpan[] }>(`/api/tracing/traces/${encodeURIComponent(traceId)}/spans${query({ limit })}`, requestOptions(signal)),
  tracingEvents: (traceId: string, limit = 500, afterSeq?: number, signal?: RequestSignal) =>
    getJSON<{ items: ObservationEvent[] }>(`/api/tracing/traces/${encodeURIComponent(traceId)}/events${query({ limit, after_seq: afterSeq })}`, requestOptions(signal)),
  tracingLogs: (params: {
    level?: string; q?: string; trace_id?: string; module?: string; component?: string;
    operation?: string; source?: string; start_after_us?: number; start_before_us?: number;
    limit?: number; after_seq?: number | null;
  } = {}, signal?: RequestSignal) =>
    getJSON<{ items: ObservationEvent[]; total: number; has_more?: boolean; next_after_seq?: number | null }>(
      `/api/tracing/logs${query(params)}`,
      requestOptions(signal),
    ),
  tracingPayload: (payloadId: string, signal?: RequestSignal) =>
    getJSON<ObservationPayload>(`/api/tracing/payloads/${encodeURIComponent(payloadId)}`, requestOptions(signal)),
  tracingClientEvent: (payload: Record<string, unknown>, signal?: RequestSignal) =>
    getJSON<{ ok: boolean }>('/api/tracing/client-events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      ...requestOptions(signal),
    }),
  tracingExportTrace: (traceId: string, format: 'json' | 'jsonl' | 'csv' = 'json', includePayloads = false, signal?: RequestSignal) =>
    gatewayFetch(`/api/tracing/traces/${encodeURIComponent(traceId)}/export${query({ format, include_payloads: includePayloads })}`, requestOptions(signal)),
  tracingCreateExport: (payload: { format: 'json' | 'jsonl' | 'csv'; filter?: ObservationFilter; include_payloads?: boolean }, signal?: RequestSignal) =>
    getJSON<ObservationExportJob>('/api/tracing/exports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      ...requestOptions(signal),
    }),
  tracingExportStatus: (exportId: string, signal?: RequestSignal) =>
    getJSON<ObservationExportJob>(`/api/tracing/exports/${encodeURIComponent(exportId)}`, requestOptions(signal)),
  tracingCancelExport: (exportId: string, signal?: RequestSignal) =>
    getJSON<{ ok: boolean }>(`/api/tracing/exports/${encodeURIComponent(exportId)}`, { method: 'DELETE', ...requestOptions(signal) }),
};

export type {
  ObservationEvent,
  ObservationFilter,
  ObservationPayload,
  ObservationSpan,
  ObservationTrace,
  ObservationTraceBundle,
};
