import type { InspectorTabKey } from '../layouts/inspector-shell';

export interface InspectorSessionUi {
  tab: InspectorTabKey;
  expandedFiles: string[];
  expandedMessage: string | null;
}

/** Keeps transient Context/Files UI choices scoped to their owning session. */
export class InspectorSessionUiStore {
  readonly #sessions = new Map<string, InspectorSessionUi>();

  save(sessionId: string | null, state: InspectorSessionUi): void {
    if (!sessionId) return;
    this.#sessions.set(sessionId, {
      tab: state.tab,
      expandedFiles: [...state.expandedFiles],
      expandedMessage: state.expandedMessage,
    });
  }

  load(sessionId: string | null): InspectorSessionUi {
    const saved = sessionId ? this.#sessions.get(sessionId) : undefined;
    return saved
      ? {
          tab: saved.tab,
          expandedFiles: [...saved.expandedFiles],
          expandedMessage: saved.expandedMessage,
        }
      : { tab: 'context', expandedFiles: [], expandedMessage: null };
  }

  clear(sessionId: string): void {
    this.#sessions.delete(sessionId);
  }
}
