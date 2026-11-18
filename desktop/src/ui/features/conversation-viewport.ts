export interface ConversationViewportRow {
  key: string;
  sig: string;
  estimatedHeight: number;
  live: boolean;
}

export interface ConversationViewportAnchor {
  key: string;
  offset: number;
}

const VIRTUAL_ROW_THRESHOLD = 120;
const OVERSCAN_ROWS = 20;
const OVERSCAN_PX = 540;

interface RowMeasurement {
  height: number;
  sig: string;
}

/** Height prefix sums with logarithmic lookup and local updates during streaming. */
class RowExtents {
  private values = new Float64Array(16);
  private tree = new Float64Array(17);

  reserve(size: number): void {
    if (size <= this.values.length) return;
    let capacity = this.values.length;
    while (capacity < size) capacity *= 2;
    const values = new Float64Array(capacity);
    values.set(this.values);
    this.values = values;
    const tree = new Float64Array(capacity + 1);
    for (let index = 1; index <= capacity; index += 1) {
      tree[index] += values[index - 1];
      const parent = index + (index & -index);
      if (parent <= capacity) tree[parent] += tree[index];
    }
    this.tree = tree;
  }

  set(index: number, extent: number): boolean {
    const delta = extent - this.values[index];
    if (Math.abs(delta) < 0.25) return false;
    this.values[index] = extent;
    for (let node = index + 1; node < this.tree.length; node += node & -node) {
      this.tree[node] += delta;
    }
    return true;
  }

  sum(end: number): number {
    let result = 0;
    for (let node = end; node > 0; node -= node & -node) result += this.tree[node];
    return result;
  }

  indexAt(offset: number): number {
    let index = 0;
    for (let bit = this.values.length; bit > 0; bit >>>= 1) {
      const next = index + bit;
      if (next < this.tree.length && this.tree[next] <= offset) {
        offset -= this.tree[next];
        index = next;
      }
    }
    return index;
  }
}

/**
 * The full transcript is kept as lightweight render plans. Only selected plans
 * need DOM. Heights are keyed by row so front insertion preserves measurements.
 */
export class ConversationViewport<Row extends ConversationViewportRow> {
  readonly rows: Row[] = [];
  private readonly indices = new Map<string, number>();
  private readonly live = new Set<string>();
  private readonly measurements = new Map<string, RowMeasurement>();
  private readonly extents = new RowExtents();
  private gap = 0;
  private width = 0;

  get enabled(): boolean { return this.rows.length > VIRTUAL_ROW_THRESHOLD; }

  replaceFrom(start: number, rows: readonly Row[]): void {
    const previousLength = this.rows.length;
    this.extents.reserve(start + rows.length);
    const replacementKeys = new Set(rows.map((row) => row.key));
    for (let index = start; index < previousLength; index += 1) {
      const previous = this.rows[index];
      this.indices.delete(previous.key);
      this.live.delete(previous.key);
      if (!replacementKeys.has(previous.key)) this.measurements.delete(previous.key);
    }

    this.rows.length = start;
    for (const row of rows) {
      const index = this.rows.length;
      this.rows.push(row);
      this.indices.set(row.key, index);
      if (row.live) this.live.add(row.key);
      this.extents.set(index, this.extent(row));
    }
    for (let index = this.rows.length; index < previousLength; index += 1) this.extents.set(index, 0);
  }

  private extent(row: Row): number {
    const measurement = this.measurements.get(row.key);
    const measured = measurement && (measurement.height > 0 || row.sig === measurement.sig)
      ? measurement.height : row.estimatedHeight;
    return measured > 0 ? measured + this.gap : 0;
  }

  setGeometry(width: number, gap: number): boolean {
    if (width <= 0) return false;
    const widthChanged = this.width > 0 && Math.round(this.width / 32) !== Math.round(width / 32);
    if (widthChanged) {
      for (const [key, measurement] of this.measurements) {
        if (measurement.height > 0) this.measurements.delete(key);
      }
    }
    this.width = width;
    if (!widthChanged && Math.abs(gap - this.gap) < 0.25) return false;
    this.gap = gap;
    this.rows.forEach((row, index) => this.extents.set(index, this.extent(row)));
    return true;
  }

  measure(key: string, sig: string, height: number): boolean {
    const index = this.indices.get(key);
    if (index === undefined || this.rows[index].sig !== sig || !Number.isFinite(height) || height < 0) return false;
    this.measurements.set(key, { height, sig });
    return this.extents.set(index, height > 0 ? height + this.gap : 0);
  }

  rowForKey(key: string): Row | undefined {
    const index = this.indices.get(key);
    return index === undefined ? undefined : this.rows[index];
  }

  topOf(key: string): number | undefined {
    const index = this.indices.get(key);
    return index === undefined ? undefined : this.extents.sum(index);
  }

  get totalHeight(): number { return this.spacerHeight(0, this.rows.length); }

  spacerHeight(start: number, end: number): number {
    return Math.max(0, this.extents.sum(end) - this.extents.sum(start) - this.gap);
  }

  private indexAt(offset: number): number {
    return Math.min(this.rows.length - 1, this.extents.indexAt(Math.max(0, offset)));
  }

  anchorAt(offset: number): ConversationViewportAnchor | undefined {
    const index = this.indexAt(offset);
    const key = this.rows[index]?.key;
    return key === undefined ? undefined : { key, offset: this.extents.sum(index) - offset };
  }

  select(offset: number, height: number, followBottom: boolean, keepKeys: Iterable<string> = []): number[] {
    if (!this.enabled) return this.rows.map((_row, index) => index);
    const viewportHeight = Math.max(1, height);
    const top = followBottom ? Math.max(0, this.totalHeight - viewportHeight) : offset;
    const first = this.indexAt(top);
    const last = this.indexAt(top + viewportHeight);
    const start = Math.max(this.indexAt(top - OVERSCAN_PX), first - OVERSCAN_ROWS);
    const end = Math.min(this.indexAt(top + viewportHeight + OVERSCAN_PX), last + OVERSCAN_ROWS);
    const selected = new Set<number>();
    for (let index = start; index <= end; index += 1) selected.add(index);
    for (const keys of [this.live, keepKeys]) {
      for (const key of keys) {
        const index = this.indices.get(key);
        if (index !== undefined) selected.add(index);
      }
    }
    return [...selected].sort((a, b) => a - b);
  }
}
