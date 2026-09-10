export interface PageLifecycleContext { signal: AbortSignal; }
export interface PageContribution {
  id: string;
  isAvailable?: () => boolean;
  activate(context: PageLifecycleContext): void | Promise<void>;
  deactivate(): void | Promise<void>;
}
interface ActivationRecord {
  contribution: PageContribution;
  controller: AbortController;
  cleaned: boolean;
}

export class PageRegistry {
  private readonly entries = new Map<string, PageContribution>();
  private active: ActivationRecord | null = null;
  private transition: Promise<void> = Promise.resolve();
  private cleanupChain: Promise<void> = Promise.resolve();
  private version = 0;
  private readonly pending = new Map<string, Promise<boolean>>();

  register(contribution: PageContribution): () => Promise<void> {
    if (this.entries.has(contribution.id)) throw new Error(`Page contribution already registered: ${contribution.id}`);
    this.entries.set(contribution.id, contribution);
    let result: Promise<void> | null = null;
    return () => {
      if (result) return result;
      if (this.entries.get(contribution.id) === contribution) this.entries.delete(contribution.id);
      result = this.active && this.active.contribution === contribution ? this.deactivate() : Promise.resolve();
      return result;
    };
  }

  activate(id: string): Promise<boolean> {
    const currentContribution = this.entries.get(id);
    if (this.active && this.active.contribution === currentContribution) return Promise.resolve(true);
    const pending = this.pending.get(id);
    if (pending) return pending;
    const version = ++this.version;
    const contribution = currentContribution;
    this.detachActive();
    const operation = this.transition.then(async () => {
      await this.cleanupChain;
      if (version !== this.version || !contribution || this.entries.get(id) !== contribution || contribution.isAvailable?.() === false) return;
      const record: ActivationRecord = { contribution, controller: new AbortController(), cleaned: false };
      this.active = record;
      try {
        await contribution.activate({ signal: record.controller.signal });
        if (version !== this.version || record.controller.signal.aborted || this.active !== record) {
          if (this.active === record) this.active = null;
          await this.enqueueCleanup(record);
        }
      } catch (error) {
        if (this.active === record) this.active = null;
        record.controller.abort();
        await this.enqueueCleanup(record);
        throw error;
      }
    });
    this.transition = operation.catch(() => undefined);
    const result = operation.then(() => Boolean(contribution && this.entries.get(id) === contribution));
    this.pending.set(id, result);
    void result.then(() => {
      if (this.pending.get(id) === result) this.pending.delete(id);
    }, () => {
      if (this.pending.get(id) === result) this.pending.delete(id);
    });
    return result;
  }

  deactivate(): Promise<void> {
    ++this.version;
    this.detachActive();
    return this.transition.then(() => this.cleanupChain);
  }

  get activeId(): string | null { return this.active?.contribution.id ?? null; }

  private detachActive(): void {
    const record = this.active;
    if (!record) return;
    this.active = null;
    record.controller.abort();
    this.enqueueCleanup(record);
  }

  private enqueueCleanup(record: ActivationRecord): Promise<void> {
    if (record.cleaned) return this.cleanupChain;
    record.cleaned = true;
    this.cleanupChain = this.cleanupChain
      .then(() => Promise.resolve(record.contribution.deactivate()))
      .catch((error) => {
        console.error(`[page] deactivate ${record.contribution.id} failed:`, error);
      });
    return this.cleanupChain;
  }
}
