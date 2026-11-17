export type ShutdownReason = 'app-quit' | 'update' | 'uninstall';

interface QuitEvent {
  preventDefault(): void;
}

interface DesktopShutdownOptions {
  prepare(reason: ShutdownReason): Promise<void>;
  exit(): void;
  resume(): void;
  report(error: unknown): void;
}

/** Own the one drain shared by native quit and installer handoffs. */
export class DesktopShutdown {
  private preparation: Promise<void> | null = null;
  private handoff = false;
  private quitRequested = false;
  private exitAllowed = false;

  constructor(private readonly options: DesktopShutdownOptions) {}

  get canCloseWindows(): boolean {
    return this.exitAllowed;
  }

  beforeQuit(event: QuitEvent): void {
    if (this.exitAllowed) return;
    event.preventDefault();
    if (this.quitRequested) return;
    this.quitRequested = true;
    if (!this.handoff) this.exitAfter(this.prepare('app-quit'));
  }

  beginHandoff(reason: 'update' | 'uninstall'): Promise<void> | null {
    if (this.preparation || this.quitRequested) return null;
    this.handoff = true;
    return this.prepare(reason);
  }

  finishHandoff(launched: boolean): void {
    if (!this.handoff || !this.preparation) return;
    this.handoff = false;
    if (launched || this.quitRequested) {
      this.quitRequested = true;
      this.exitAfter(this.preparation);
    } else {
      this.preparation = null;
      this.options.resume();
    }
  }

  private prepare(reason: ShutdownReason): Promise<void> {
    this.preparation ??= Promise.resolve().then(() => this.options.prepare(reason));
    return this.preparation;
  }

  private exitAfter(preparation: Promise<void>): void {
    void preparation.catch(this.options.report).finally(() => {
      // Leave the native before-quit/close stack and give the initiating IPC
      // response an opportunity to reach the renderer.
      setImmediate(() => {
        this.exitAllowed = true;
        this.options.exit();
      });
    });
  }
}

export async function waitForShutdownTask<T>(task: Promise<T>, label: string, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      task,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} did not stop in ${timeoutMs}ms`)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
