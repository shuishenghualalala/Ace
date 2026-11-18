import { InteractionCardIndex } from '../components/interaction-card';
import { interactiveOwnerKey } from './interactive-owner';

export interface ConversationDomChanges {
  added: readonly HTMLElement[];
  removed: readonly HTMLElement[];
  reset?: boolean;
  reordered?: boolean;
}

export interface LiveConversationTurn {
  root: HTMLElement;
  text: HTMLElement | null;
  thinking: HTMLElement | null;
  label: HTMLElement | null;
}

interface IndexedUnit {
  turns: Map<string, LiveConversationTurn>;
  teamId?: string;
}

class ConversationDomIndex {
  readonly interactions: InteractionCardIndex;
  readonly turns = new Map<string, LiveConversationTurn>();
  readonly teamLabels = new Map<string, HTMLElement>();
  private readonly units = new Map<HTMLElement, IndexedUnit>();

  constructor(readonly accountId: string, readonly sessionId: string | null, submitted: Set<string>) {
    this.interactions = new InteractionCardIndex(submitted);
  }

  update({ added, removed, reordered }: ConversationDomChanges): void {
    for (const unit of removed) {
      const previous = this.units.get(unit);
      if (!previous) continue;
      this.units.delete(unit);
      for (const [id, turn] of previous.turns) {
        if (this.turns.get(id) === turn) this.turns.delete(id);
      }
      if (previous.teamId) this.teamLabels.delete(previous.teamId);
    }
    for (const unit of added) {
      if (this.units.has(unit) || !unit.matches('.msg[data-streaming="true"]')) continue;
      const label = unit.querySelector<HTMLElement>('.msg__fold-label');
      const turns = new Map<string, LiveConversationTurn>();
      for (const marker of unit.querySelectorAll<HTMLElement>('[data-text-for], [data-thinking-for]')) {
        const id = marker.dataset.textFor ?? marker.dataset.thinkingFor;
        if (!id) continue;
        const turn = turns.get(id) ?? { root: unit, label, text: null, thinking: null };
        if (marker.dataset.textFor !== undefined) turn.text = marker;
        else turn.thinking = marker.querySelector<HTMLElement>('.process-timeline__thinking');
        turns.set(id, turn);
      }
      for (const [id, turn] of turns) this.turns.set(id, turn);

      const teamId = unit.classList.contains('team-internal') ? unit.dataset.messageId : undefined;
      if (teamId && label) this.teamLabels.set(teamId, label);
      if (turns.size || teamId) this.units.set(unit, { turns, ...(teamId ? { teamId } : {}) });
    }
    this.interactions.update(added, removed, reordered);
  }
}

const indexes = new WeakMap<HTMLElement, ConversationDomIndex>();
const submittedByScope = new Map<string, Set<string>>();

function currentIndex(container: HTMLElement, sessionId: string | null): ConversationDomIndex | undefined {
  const index = indexes.get(container);
  return index?.sessionId === sessionId && index.accountId === interactiveOwnerKey() ? index : undefined;
}

/** Publish mounted keyed units after applying adds/removes/moves, before render hooks. */
export function syncConversationDom(
  container: HTMLElement, sessionId: string | null, changes: ConversationDomChanges,
): void {
  let index = changes.reset ? undefined : currentIndex(container, sessionId);
  if (!index) {
    indexes.get(container)?.interactions.deactivate();
    const accountId = interactiveOwnerKey();
    const scope = JSON.stringify([accountId, sessionId]);
    const submitted = submittedByScope.get(scope) ?? new Set<string>();
    submittedByScope.set(scope, submitted);
    index = new ConversationDomIndex(accountId, sessionId, submitted);
    indexes.set(container, index);
  }
  index.update(changes);
  if (!index.accountId) index.interactions.deactivate();
}

export function disposeConversationDom(container: HTMLElement): void {
  indexes.get(container)?.interactions.deactivate();
  indexes.delete(container);
}

export function getLiveConversationTurn(
  container: HTMLElement, sessionId: string, messageId: string,
): LiveConversationTurn | undefined {
  const turn = currentIndex(container, sessionId)?.turns.get(messageId);
  return turn && container.contains(turn.root) ? turn : undefined;
}

export function getLiveTeamLabel(container: HTMLElement, sessionId: string, messageId: string): HTMLElement | undefined {
  const label = currentIndex(container, sessionId)?.teamLabels.get(messageId);
  return label && container.contains(label) ? label : undefined;
}

export function submitConversationInteraction(
  container: HTMLElement, sessionId: string, interactionId: string, origin: Element,
): boolean {
  const index = currentIndex(container, sessionId);
  return Boolean(index?.accountId && container.contains(origin) && index.interactions.submit(interactionId, origin));
}
