export interface UiNavigationContribution<Id extends string, Context, Icon = string> {
  id: Id;
  label: string;
  icon: Icon;
  order: number;
  isAvailable?: (context: Context) => boolean;
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
