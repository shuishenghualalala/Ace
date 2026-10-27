import type { ChatMessage } from '../chat-render';
import type { InlineWidget } from './inline-widget';
import { parseVisual, type NativeVisual } from './native-visual';

export interface InlineSurfaces {
  visuals: NativeVisual[];
  widgets: InlineWidget[];
}

const bytes = (value: string): number => new TextEncoder().encode(value).length;

export function inlineSurfaces(messages: ChatMessage[], sessionId: string): InlineSurfaces {
  const visuals = new Map<string, NativeVisual>();
  const widgets = new Map<string, InlineWidget>();
  for (const message of messages) for (const tool of message.toolCalls ?? []) {
    if (tool.name !== 'Widget' || !tool.result) continue;
    try {
      const payload = JSON.parse(tool.result);
      const surface = payload.surface;
      if (payload.ok !== true || surface?.display !== 'inline'
        || surface.sessionId !== sessionId || surface.status !== 'ready') continue;
      if (surface.version === 2 && surface.mode === 'native') {
        const spec = parseVisual(surface.spec);
        if (spec) {
          const key = `native:${sessionId}:${tool.toolCallId}`;
          visuals.set(key, { key, sessionId, spec });
        }
      } else if (surface.version === 1 && surface.mode === 'widget'
        && typeof surface.widgetId === 'string' && /^widget_[0-9a-f]{12}$/.test(surface.widgetId)
        && typeof surface.title === 'string' && typeof surface.html === 'string'
        && bytes(surface.html) <= 128_000) {
        const key = `inline:${sessionId}:${tool.toolCallId}`;
        widgets.set(key, {
          key, sessionId, widgetId: surface.widgetId, title: surface.title,
          html: surface.html, resourceRevision: surface.resourceRevision || 0,
        });
      }
    } catch { /* Other tool results are ordinary text. */ }
  }
  return { visuals: [...visuals.values()], widgets: [...widgets.values()] };
}
