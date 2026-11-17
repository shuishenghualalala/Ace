import { createHash, randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { MAX_CHAT_FRAME_BYTES, type DesktopChatOutboxEntry } from '../shared/chat-outbox';

const MAX_OUTBOX_ENTRY_BYTES = 2 * MAX_CHAT_FRAME_BYTES + 16 * 1024;
const phases = new Set(['queued', 'submitted', 'accepted', 'withdrawing']);

interface StoredEntry {
  version: 1;
  createdAt: number;
  entry: DesktopChatOutboxEntry;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isRequestId(value: unknown): value is string {
  return typeof value === 'string' && /^[\w.:-]{1,200}$/.test(value);
}

export function desktopChatOutboxScope(identityMode: string, owner: string, crewHome: string): string {
  const home = path.resolve(crewHome);
  return createHash('sha256')
    .update(JSON.stringify([identityMode, owner, process.platform === 'win32' ? home.toLowerCase() : home]))
    .digest('hex');
}

function validateEntry(raw: unknown): asserts raw is DesktopChatOutboxEntry {
  if (!object(raw) || !isRequestId(raw.requestId)
    || typeof raw.sessionId !== 'string' || raw.sessionId.length === 0 || raw.sessionId.length > 400
    || typeof raw.phase !== 'string' || !phases.has(raw.phase) || !object(raw.value)) {
    throw new Error('Invalid chat outbox entry');
  }
}

export class DesktopChatOutbox {
  private tail: Promise<unknown> = Promise.resolve();
  private lastCreatedAt = 0;

  constructor(private readonly root: string) {}

  list(scope: string): Promise<DesktopChatOutboxEntry[]> {
    return this.serialize(async () => {
      const directory = this.directory(scope);
      let names: string[];
      try {
        names = await fs.readdir(directory);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') return [];
        throw error;
      }
      const entries: StoredEntry[] = [];
      for (const name of names) {
        if (/^[a-f0-9]{64}\.json\.[a-f0-9-]{36}\.tmp$/.test(name)) {
          // A process exit during a flush can leave an unpublished temporary file.
          await fs.rm(path.join(directory, name), { force: true }).catch(() => undefined);
          continue;
        }
        if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
        const stored = await this.read(path.join(directory, name));
        if (stored) entries.push(stored);
      }
      entries.sort((left, right) => left.createdAt - right.createdAt);
      const last = entries[entries.length - 1];
      if (last) this.lastCreatedAt = Math.max(this.lastCreatedAt, last.createdAt);
      return entries.map(({ entry }) => entry);
    });
  }

  put(scope: string, raw: unknown): Promise<void> {
    return this.serialize(async () => {
      validateEntry(raw);
      const file = this.file(scope, raw.requestId);
      const previous = await this.read(file);
      if (previous && previous.entry.sessionId !== raw.sessionId) {
        throw new Error('Chat outbox request already belongs to a different session');
      }
      const createdAt = previous?.createdAt ?? Math.max(Date.now(), this.lastCreatedAt + 1);
      this.lastCreatedAt = Math.max(this.lastCreatedAt, createdAt);
      const entry: DesktopChatOutboxEntry = {
        requestId: raw.requestId, sessionId: raw.sessionId, phase: raw.phase, value: raw.value,
      };
      const data = JSON.stringify({ version: 1, createdAt, entry } satisfies StoredEntry);
      if (Buffer.byteLength(data, 'utf8') > MAX_OUTBOX_ENTRY_BYTES) {
        throw new Error('Chat outbox entry is too large');
      }
      await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
      const temporary = `${file}.${randomUUID()}.tmp`;
      try {
        await fs.writeFile(temporary, data, { flag: 'wx', mode: 0o600, flush: true });
        await fs.rename(temporary, file);
      } catch (error) {
        await fs.rm(temporary, { force: true }).catch(() => undefined);
        throw error;
      }
    });
  }

  remove(scope: string, requestId: unknown): Promise<void> {
    return this.serialize(async () => {
      if (!isRequestId(requestId)) throw new Error('Invalid chat outbox request id');
      await fs.rm(this.file(scope, requestId), { force: true });
    });
  }

  private serialize<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.tail.then(operation);
    this.tail = next.catch(() => undefined);
    return next;
  }

  private directory(scope: string): string {
    if (!/^[a-f0-9]{64}$/.test(scope)) throw new Error('Invalid chat outbox scope');
    return path.join(this.root, scope);
  }

  private file(scope: string, requestId: string): string {
    const digest = createHash('sha256').update(requestId).digest('hex');
    return path.join(this.directory(scope), `${digest}.json`);
  }

  private async read(file: string): Promise<StoredEntry | undefined> {
    let content: string;
    try {
      content = await fs.readFile(file, 'utf8');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
      throw error;
    }
    const data: unknown = JSON.parse(content);
    if (!object(data) || data.version !== 1 || !Number.isSafeInteger(data.createdAt)) {
      throw new Error('Invalid stored chat outbox record');
    }
    validateEntry(data.entry);
    return { version: 1, createdAt: data.createdAt as number, entry: data.entry };
  }
}
