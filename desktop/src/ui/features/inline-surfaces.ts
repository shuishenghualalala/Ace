import type { ChatMessage } from '../chat-render';
import { parseVisual, type NativeVisual } from './native-visual';

export interface InlineSurfaces {
  visuals: NativeVisual[];
}

export function inlineSurfaces(messages: ChatMessage[], sessionId: string): InlineSurfaces {
  const visuals = new Map<string, NativeVisual>();
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
      }
    } catch { /* Other tool results are ordinary text. */ }
  }
  return { visuals: [...visuals.values()] };
}
