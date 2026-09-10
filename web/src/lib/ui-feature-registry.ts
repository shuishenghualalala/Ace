export interface UiNavigationContribution<Id extends string, Context, Icon = string> {
  id: Id;
  label: string;
  icon: Icon;
  order: number;
  isAvailable?: (context: Context) => boolean;
}

export interface UiPageContribution<Id extends string, Context, Page> {
  id: Id;
  isAvailable?: (context: Context) => boolean;
  render: (context: Context) => Page;
}

export class UiPageRegistry<Id extends string, Context, Page> {
  private readonly entries = new Map<Id, UiPageContribution<Id, Context, Page>>();

  register(entry: UiPageContribution<Id, Context, Page>): () => void {
    if (this.entries.has(entry.id)) throw new Error(`UI page contribution already registered: ${entry.id}`);
    this.entries.set(entry.id, entry);
    let disposed = false;
    return () => {
      if (disposed) return;
      disposed = true;
      if (this.entries.get(entry.id) === entry) this.entries.delete(entry.id);
    };
  }

  project(id: Id, context: Context): Page | null {
    const entry = this.entries.get(id);
    return entry && (entry.isAvailable?.(context) ?? true) ? entry.render(context) : null;
  }
}

/** Host-owned registry for deterministic, reversible UI feature contributions. */
export class UiFeatureRegistry<Id extends string, Context, Icon = string> {
  private readonly entries = new Map<Id, UiNavigationContribution<Id, Context, Icon>>();

  register(contribution: UiNavigationContribution<Id, Context, Icon>): () => void {
    if (this.entries.has(contribution.id)) {
      throw new Error(`UI navigation contribution already registered: ${contribution.id}`);
    }
    if (!Number.isFinite(contribution.order)) {
      throw new Error(`UI navigation contribution has invalid order: ${contribution.id}`);
    }
    this.entries.set(contribution.id, contribution);
    let disposed = false;
    return () => {
      if (disposed) return;
      disposed = true;
      // A stale disposer must never unregister a newer contribution that reused
      // the same id after this registration was released.
      if (this.entries.get(contribution.id) === contribution) {
        this.entries.delete(contribution.id);
      }
    };
  }

  unregister(id: Id): boolean {
    return this.entries.delete(id);
  }

  project(context: Context): UiNavigationContribution<Id, Context, Icon>[] {
    return Array.from(this.entries.values())
      .filter((entry) => entry.isAvailable?.(context) ?? true)
      .sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
  }
}
