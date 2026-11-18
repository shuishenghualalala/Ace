/** A turn keeps its text and a bounded, reorderable suffix of Gateway fragments. */
export interface DeltaRange {
  start: number;
  end: number;
}

export type DeltaPart = readonly [sequence: number, utf16Length: number];

const MAX_DELTA_PARTS = 256;
const MAX_RECENT_FRAGMENTS = 512;

interface Fragment {
  sequence: number;
  text: string;
  range: DeltaRange | null;
}

interface TurnBuffer {
  prefix: string;
  sealedThrough: number;
  sealedRangeEnd: number;
  sealedOpaqueRangeEnd: number;
  recent: Fragment[];
  bySequence: Map<number, Fragment>;
  rangeEnd: number;
  opaqueRangeEnd: number;
  text: string;
}

export interface SessionReassembly {
  turns: Map<string, TurnBuffer>;
  latestDelta: number;
  toolBoundary: number;
}

const buffers = new Map<string, SessionReassembly>();

export function createSessionReassembly(): SessionReassembly {
  return { turns: new Map(), latestDelta: 0, toolBoundary: 0 };
}

function reassemblyFor(sessionId: string): SessionReassembly {
  let reassembly = buffers.get(sessionId);
  if (!reassembly) {
    reassembly = createSessionReassembly();
    buffers.set(sessionId, reassembly);
  }
  return reassembly;
}

export function parseDeltaRange(
  body: { delta_start?: number | string; delta_end?: number | string },
  sequenceFallback?: number,
): DeltaRange | null {
  const start = Number(body.delta_start ?? sequenceFallback);
  const end = Number(body.delta_end ?? sequenceFallback);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start <= 0 || end <= 0) return null;
  return { start: Math.min(start, end), end: Math.max(start, end) };
}

/** Wire offsets are UTF-16 lengths, including zero-length fragments. */
export function parseDeltaParts(
  gatewaySequence: number,
  text: string,
  parts: unknown,
): readonly DeltaPart[] | null {
  if (!Array.isArray(parts) || parts.length < 1 || parts.length > MAX_DELTA_PARTS) return null;
  let previous = 0;
  let end = 0;
  for (const part of parts) {
    if (!Array.isArray(part) || part.length !== 2) return null;
    const [sequence, length] = part;
    if (!Number.isSafeInteger(sequence) || sequence <= previous
      || !Number.isSafeInteger(length) || length < 0) return null;
    previous = sequence;
    end += length;
    if (end > text.length) return null;
  }
  return previous === gatewaySequence && end === text.length ? parts as readonly DeltaPart[] : null;
}

function splitDelta(text: string, parts: readonly DeltaPart[], range: DeltaRange | null): Fragment[] {
  let offset = 0;
  const contiguousRange = range && range.end - range.start + 1 === parts.length ? range : null;
  return parts.map(([sequence, length], index) => {
    const start = offset;
    offset += length;
    const position = contiguousRange ? contiguousRange.start + index : 0;
    return { sequence, text: text.slice(start, offset), range: position ? { start: position, end: position } : null };
  });
}

function isOpaque(range: DeltaRange | null): boolean {
  return !!range && range.start < range.end;
}

function containsRange(outer: DeltaRange, inner: DeltaRange): boolean {
  return outer.start <= inner.start && outer.end >= inner.end;
}

/** Legacy batches have known endpoints. Only replace or ignore a fully covered range. */
function canCover(outer: Fragment, inner: Fragment, outerRange: DeltaRange, innerRange: DeltaRange): boolean {
  if (!containsRange(outerRange, innerRange) || outer.text.length < inner.text.length) return false;
  if (outerRange.start === innerRange.start && !outer.text.startsWith(inner.text)) return false;
  if (outerRange.end === innerRange.end && !outer.text.endsWith(inner.text)) return false;
  return outerRange.start === innerRange.start || outerRange.end === innerRange.end
    || outer.text.includes(inner.text);
}

function insertRecent(turn: TurnBuffer, incoming: Fragment[]): Fragment[] | null {
  const indexed = new Map(turn.bySequence);
  let opaqueRangeEnd = turn.opaqueRangeEnd;
  for (const fragment of incoming) {
    const known = indexed.get(fragment.sequence);
    const range = fragment.range;
    const opaque = isOpaque(range);
    if (known && !opaque && !isOpaque(known.range)) {
      if (known.text !== fragment.text) return null;
      continue;
    }

    let covered = false;
    if (range && (opaque || range.start <= opaqueRangeEnd)) {
      for (const candidate of indexed.values()) {
        const other = candidate.range;
        if (!other || (!opaque && !isOpaque(other)) || other.end < range.start || range.end < other.start) continue;
        if (canCover(candidate, fragment, other, range)) {
          covered = true;
          break;
        }
        if (!canCover(fragment, candidate, range, other)) return null;
        indexed.delete(candidate.sequence);
      }
    }
    if (covered) continue;
    if (indexed.has(fragment.sequence)) return null;
    indexed.set(fragment.sequence, fragment);
    if (opaque && range) opaqueRangeEnd = Math.max(opaqueRangeEnd, range.end);
  }
  return Array.from(indexed.values()).sort((a, b) => a.sequence - b.sequence);
}

function indexFragment(turn: TurnBuffer, fragment: Fragment): void {
  turn.bySequence.set(fragment.sequence, fragment);
  if (fragment.range) {
    turn.rangeEnd = Math.max(turn.rangeEnd, fragment.range.end);
    if (isOpaque(fragment.range)) turn.opaqueRangeEnd = Math.max(turn.opaqueRangeEnd, fragment.range.end);
  }
}

function trimRecent(turn: TurnBuffer): void {
  if (turn.recent.length <= MAX_RECENT_FRAGMENTS) return;
  const sealed = turn.recent.splice(0, turn.recent.length - MAX_RECENT_FRAGMENTS / 2);
  for (const fragment of sealed) {
    turn.prefix += fragment.text;
    turn.sealedThrough = fragment.sequence;
    turn.sealedRangeEnd = Math.max(turn.sealedRangeEnd, fragment.range?.end ?? 0);
    if (fragment.range && isOpaque(fragment.range)) {
      turn.sealedOpaqueRangeEnd = Math.max(turn.sealedOpaqueRangeEnd, fragment.range.end);
    }
    turn.bySequence.delete(fragment.sequence);
  }
}

/** Tool completions can change the destination message for later deltas. */
export function noteSessionDeltaBoundary(reassembly: SessionReassembly, sequence: number): boolean {
  if (sequence < reassembly.latestDelta || sequence < reassembly.toolBoundary) return false;
  reassembly.toolBoundary = sequence;
  return true;
}

export function noteDeltaBoundary(sessionId: string, sequence: number): boolean {
  return noteSessionDeltaBoundary(reassemblyFor(sessionId), sequence);
}

export function noteDelta(
  sessionId: string,
  assistantId: string,
  sequence: number,
  text: string,
  range: DeltaRange | null = null,
  rawParts?: unknown,
): string | null {
  return noteSessionDelta(reassemblyFor(sessionId), assistantId, sequence, text, range, rawParts);
}

/** Returns null when the previously accepted text cannot be reconciled exactly. */
export function noteSessionDelta(
  reassembly: SessionReassembly,
  assistantId: string,
  sequence: number,
  text: string,
  range: DeltaRange | null = null,
  rawParts?: unknown,
): string | null {
  let incoming: Fragment[];
  if (rawParts === undefined) {
    if (!Number.isSafeInteger(sequence) || sequence <= 0) return null;
    incoming = [{ sequence, text, range }];
  } else {
    const parts = parseDeltaParts(sequence, text, rawParts);
    if (!parts) return null;
    incoming = splitDelta(text, parts, range);
  }
  if (incoming[0].sequence <= reassembly.toolBoundary) return null;

  const existing = reassembly.turns.get(assistantId);
  const turn: TurnBuffer = existing ?? {
    prefix: '', sealedThrough: 0, sealedRangeEnd: 0, sealedOpaqueRangeEnd: 0,
    recent: [], bySequence: new Map(),
    rangeEnd: 0, opaqueRangeEnd: 0, text: '',
  };
  const first = incoming[0];
  if (first.sequence <= turn.sealedThrough || incoming.some((fragment) => fragment.range
    && fragment.range.start <= (isOpaque(fragment.range) ? turn.sealedRangeEnd : turn.sealedOpaqueRangeEnd))) {
    return null;
  }

  // Gateway sequence is authoritative for raw and boundary-aware deltas. The
  // compatibility check only matters when a legacy batch has no part offsets.
  const mayOverlapOpaque = incoming.some((fragment) => fragment.range
    && fragment.range.start <= (isOpaque(fragment.range) ? turn.rangeEnd : turn.opaqueRangeEnd));
  if (first.sequence > (turn.recent.at(-1)?.sequence ?? 0) && !mayOverlapOpaque) {
    for (const fragment of incoming) {
      turn.recent.push(fragment);
      indexFragment(turn, fragment);
    }
    turn.text += text;
  } else {
    const ordered = insertRecent(turn, incoming);
    if (!ordered) return null;
    turn.recent = ordered;
    turn.bySequence.clear();
    turn.rangeEnd = turn.sealedRangeEnd;
    turn.opaqueRangeEnd = turn.sealedOpaqueRangeEnd;
    for (const fragment of ordered) indexFragment(turn, fragment);
    turn.text = turn.prefix + ordered.map((fragment) => fragment.text).join('');
  }
  trimRecent(turn);
  reassembly.latestDelta = Math.max(reassembly.latestDelta, sequence);
  if (!existing) reassembly.turns.set(assistantId, turn);
  return turn.text;
}

export function installSessionReassembly(sessionId: string, reassembly: SessionReassembly): void {
  buffers.set(sessionId, reassembly);
}

export function resetAssistant(sessionId: string, assistantId: string): void {
  buffers.get(sessionId)?.turns.delete(assistantId);
}

export function resetSession(sessionId: string): void {
  buffers.delete(sessionId);
}

export function resetSessionExcept(sessionId: string, keepIds: Set<string>): void {
  const turns = buffers.get(sessionId)?.turns;
  if (!turns) return;
  for (const id of turns.keys()) {
    if (!keepIds.has(id)) turns.delete(id);
  }
}

/** Diagnostic view of the bounded replay window. */
export function peekFrags(sessionId: string, assistantId: string): Map<number, string> {
  const recent = buffers.get(sessionId)?.turns.get(assistantId)?.recent ?? [];
  return new Map(recent.map(({ sequence, text }) => [sequence, text]));
}
