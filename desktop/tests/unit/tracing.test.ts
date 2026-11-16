/** @vitest-environment happy-dom */
import { describe, expect, it, vi } from 'vitest';

import { BoundedObservationEventQueue, isObservationDevLaunch, setObservationDevLaunch } from '../../src/shared/observability';
import { TracingSaveExportArgs } from '../../src/shared/ipc-schemas';
import { commitDownloadedExport, type ExportFileOperations } from '../../src/main/tracing-export-file';
import { backendApi } from '../../src/ui/backend-client';
import { createTracingPageContribution, mergeTracingShortcutFilter } from '../../src/ui/features/tracing';
import type { ObservationEvent, ObservationTrace, ObservationTraceBundle } from '../../src/shared/observability';

function trace(traceId: string, index = 0): ObservationTrace {
  return {
    owner_scope: 'owner',
    trace_id: traceId,
    session_id: 'session',
    request_id: `request-${index}`,
    source: 'desktop',
    started_at_us: 1_000_000 + index,
    ended_at_us: 1_100_000 + index,
    status: 'succeeded',
    quality: 'complete',
    has_error_span: 0,
    summary: `Trace ${index}`,
    attributes: {},
    server_total_ms: 100,
  };
}

function bundle(item: ObservationTrace, events: ObservationEvent[] = []): ObservationTraceBundle {
  return { trace: item, spans: [], events, payloads: [] };
}

async function settle(): Promise<void> {
  await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
}

describe('desktop observation contracts', () => {
  it('keeps the renderer event queue bounded and FIFO', () => {
    const queue = new BoundedObservationEventQueue(2);
    queue.push({ name: 'desktop.event', attributes: { id: 1 } });
    queue.push({ name: 'desktop.event', attributes: { id: 2 } });
    queue.push({ name: 'desktop.event', attributes: { id: 3 } });
    expect(queue.size).toBe(2);
    expect(queue.drain().map((item) => item.attributes?.id)).toEqual([2, 3]);
    expect(queue.size).toBe(0);
  });

  it('does not accept renderer-provided paths for export IPC', () => {
    expect(TracingSaveExportArgs.parse({ exportId: 'a'.repeat(32) })).toEqual({
      ok: true,
      value: { exportId: 'a'.repeat(32) },
    });
    expect(TracingSaveExportArgs.parse({ exportId: '../secret' }).ok).toBe(false);
    expect(TracingSaveExportArgs.parse({ exportId: 'a'.repeat(32), path: '/tmp/x' }).ok).toBe(true);
  });

  it('commits a Windows export with rollback-safe backup semantics', async () => {
    const files = new Set(['/tmp/existing.jsonl', '/tmp/download.tmp']);
    const operations: ExportFileOperations = {
      async stat(file) {
        if (!files.has(file)) throw Object.assign(new Error('missing'), { code: 'ENOENT' });
        return { isFile: () => true };
      },
      async rename(source, target) {
        if (!files.delete(source)) throw Object.assign(new Error('missing'), { code: 'ENOENT' });
        files.add(target);
      },
      async unlink(file) { files.delete(file); },
    };
    await commitDownloadedExport('/tmp/download.tmp', '/tmp/existing.jsonl', 'win32', operations);
    expect(files.has('/tmp/download.tmp')).toBe(false);
    expect(files.has('/tmp/existing.jsonl')).toBe(true);
    expect([...files].some((file) => file.endsWith('.bak'))).toBe(false);
  });

  it('restores the original Windows target when the final rename fails', async () => {
    const files = new Set(['/tmp/existing.jsonl', '/tmp/download.tmp']);
    let finalRenameAttempts = 0;
    const operations: ExportFileOperations = {
      async stat(file) {
        if (!files.has(file)) throw Object.assign(new Error('missing'), { code: 'ENOENT' });
        return { isFile: () => true };
      },
      async rename(source, target) {
        if (!files.has(source)) throw Object.assign(new Error('missing'), { code: 'ENOENT' });
        if (source === '/tmp/download.tmp') {
          finalRenameAttempts += 1;
          throw new Error('destination unavailable');
        }
        files.delete(source);
        files.add(target);
      },
      async unlink(file) { files.delete(file); },
    };
    await expect(commitDownloadedExport('/tmp/download.tmp', '/tmp/existing.jsonl', 'win32', operations))
      .rejects.toThrow('destination unavailable');
    expect(finalRenameAttempts).toBe(1);
    expect(files.has('/tmp/existing.jsonl')).toBe(true);
    expect([...files].some((file) => file.endsWith('.bak'))).toBe(false);
    expect(files.has('/tmp/download.tmp')).toBe(true);
  });

  it('keeps the dev launch gate explicit and resettable', () => {
    setObservationDevLaunch(false);
    expect(isObservationDevLaunch()).toBe(false);
    setObservationDevLaunch(true);
    expect(isObservationDevLaunch()).toBe(true);
    setObservationDevLaunch(false);
  });

  it('preserves unrelated filters while narrowing module shortcuts', () => {
    const base = { q: 'slow', status: 'failed', start_after_us: 10, operation: 'old' };
    expect(mergeTracingShortcutFilter(base, 'agent-loop')).toEqual({
      q: 'slow', status: 'failed', start_after_us: 10, module: 'agent', component: 'loop',
    });
    expect(mergeTracingShortcutFilter(base, 'wiki')).toEqual({
      q: 'slow', status: 'failed', start_after_us: 10, module: 'wiki',
    });
    expect(mergeTracingShortcutFilter(base, 'compact')).toEqual({
      q: 'slow', status: 'failed', start_after_us: 10, module: 'agent', component: 'compact',
    });
  });

  it('renders bounded trace pages and uses the recent-hour default filter', async () => {
    const root = document.createElement('div');
    root.id = 'tracing-page-root';
    const tab = document.createElement('div');
    tab.id = 'tracing-tab';
    tab.className = 'active';
    document.body.append(root, tab);
    const first = trace('a'.repeat(32), 1);
    const second = trace('b'.repeat(32), 2);
    const traces = vi.spyOn(backendApi, 'tracingTraces')
      .mockResolvedValueOnce({ items: [first], has_more: true, next_cursor: 'cursor-1' })
      .mockResolvedValueOnce({ items: [second], has_more: false, next_cursor: null });
    vi.spyOn(backendApi, 'tracingCapabilities').mockResolvedValue({ available: true });
    vi.spyOn(backendApi, 'tracingStatus').mockResolvedValue({ available: true, latest_ingest_seq: 1 });
    vi.spyOn(backendApi, 'tracingTrace').mockResolvedValue(bundle(first));
    vi.spyOn(backendApi, 'tracingSpans').mockResolvedValue({ items: [] });
    vi.spyOn(backendApi, 'tracingEvents').mockResolvedValue({ items: [] });
    const contribution = createTracingPageContribution();
    const controller = new AbortController();
    await contribution.activate({ signal: controller.signal });

    expect(traces.mock.calls[0]?.[0].start_after_us).toBeGreaterThan(Date.now() * 1_000 - 3_610_000_000);
    expect(root.querySelectorAll('[data-trace-id]')).toHaveLength(1);
    const loadMore = root.querySelector<HTMLButtonElement>('[data-tracing-action="load-more-traces"]');
    expect(loadMore).not.toBeNull();
    loadMore?.click();
    await settle();
    expect(traces.mock.calls[1]?.[2]).toBe('cursor-1');
    expect(root.querySelectorAll('[data-trace-id]')).toHaveLength(2);

    contribution.deactivate();
    root.remove();
    vi.restoreAllMocks();
  });

  it('shows structured log details, supports log pagination, and jumps to a trace', async () => {
    const root = document.createElement('div');
    root.id = 'tracing-page-root';
    const tab = document.createElement('div');
    tab.id = 'tracing-tab';
    tab.className = 'active';
    document.body.append(root, tab);
    const item = trace('c'.repeat(32), 3);
    const entry: ObservationEvent = {
      owner_scope: 'owner', ingest_seq: 20, event_id: 'event-1', trace_id: item.trace_id,
      span_id: '', name: 'desktop.error', occurred_at_us: 1_000_000, level: 'ERROR',
      source: 'desktop.renderer', module: 'desktop', component: 'renderer', operation: 'submit',
      feature_id: '', message: 'safe message', status: 'failed', attributes: { code: 'E_TEST' },
    };
    const logs = vi.spyOn(backendApi, 'tracingLogs')
      .mockResolvedValueOnce({ items: [entry], total: 1, has_more: true, next_after_seq: 20 })
      .mockResolvedValueOnce({ items: [], total: 1, has_more: false, next_after_seq: null });
    vi.spyOn(backendApi, 'tracingCapabilities').mockResolvedValue({ available: true });
    vi.spyOn(backendApi, 'tracingStatus').mockResolvedValue({ available: true, latest_ingest_seq: 2 });
    vi.spyOn(backendApi, 'tracingTraces').mockResolvedValue({ items: [item], has_more: false, next_cursor: null });
    const traceRequest = vi.spyOn(backendApi, 'tracingTrace').mockResolvedValue(bundle(item));
    vi.spyOn(backendApi, 'tracingSpans').mockResolvedValue({ items: [] });
    vi.spyOn(backendApi, 'tracingEvents').mockResolvedValue({ items: [] });
    const contribution = createTracingPageContribution();
    const controller = new AbortController();
    await contribution.activate({ signal: controller.signal });

    root.querySelector<HTMLButtonElement>('[data-tracing-action="toggle-view"]')?.click();
    await settle();
    expect(root.textContent).toContain('desktop/renderer');
    const loadMore = root.querySelector<HTMLButtonElement>('[data-tracing-action="load-more-logs"]');
    expect(loadMore).not.toBeNull();
    loadMore?.click();
    await settle();
    expect(logs.mock.calls.some((call) => call[0].after_seq === 20)).toBe(true);
    root.querySelector<HTMLButtonElement>('[data-event-id="event-1"]')?.click();
    expect(root.querySelector('.tracing-log-detail__structured')?.textContent).toContain('E_TEST');
    root.querySelector<HTMLButtonElement>('[data-tracing-action="jump-trace"]')?.click();
    await settle();
    expect(traceRequest).toHaveBeenCalledWith(item.trace_id, expect.anything());

    contribution.deactivate();
    root.remove();
    vi.restoreAllMocks();
  });

  it('aborts page queries without cancelling a durable export on navigation', async () => {
    const root = document.createElement('div');
    root.id = 'tracing-page-root';
    const tab = document.createElement('div');
    tab.id = 'tracing-tab';
    tab.className = 'active';
    document.body.append(root, tab);
    const item = trace('d'.repeat(32), 4);
    vi.spyOn(backendApi, 'tracingCapabilities').mockResolvedValue({ available: true });
    vi.spyOn(backendApi, 'tracingStatus').mockResolvedValue({ available: true, latest_ingest_seq: 1 });
    vi.spyOn(backendApi, 'tracingTraces').mockResolvedValue({ items: [item], has_more: false, next_cursor: null });
    vi.spyOn(backendApi, 'tracingTrace').mockResolvedValue(bundle(item));
    vi.spyOn(backendApi, 'tracingSpans').mockResolvedValue({ items: [] });
    vi.spyOn(backendApi, 'tracingEvents').mockResolvedValue({ items: [] });
    vi.spyOn(backendApi, 'tracingCreateExport').mockResolvedValue({
      export_id: 'export-1', owner_scope: 'owner', status: 'running', format: 'jsonl', count: 0, partial: false,
    });
    const cancel = vi.spyOn(backendApi, 'tracingCancelExport').mockResolvedValue({ ok: true });
    const contribution = createTracingPageContribution();
    const controller = new AbortController();
    await contribution.activate({ signal: controller.signal });
    root.querySelector<HTMLButtonElement>('[data-tracing-action="export"]')?.click();
    root.querySelector<HTMLButtonElement>('[data-tracing-action="export-submit"]')?.click();
    await settle();
    contribution.deactivate();
    await settle();
    expect(cancel).not.toHaveBeenCalled();
    root.remove();
    vi.restoreAllMocks();
  });
});
