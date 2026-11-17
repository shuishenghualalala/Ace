import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { MAX_CHAT_FRAME_BYTES } from '../shared/chat-outbox';
import {
  omitSubmittedComposer,
  type ComposerCheckpointRef,
  type RendererCheckpoint,
  type RendererDraftAttachment,
} from '../shared/renderer-checkpoint';

const MAX_RECORD_BYTES = 2 * MAX_CHAT_FRAME_BYTES;
const MAX_ATTACHMENTS = 256;
const MAX_RECENT_REVISIONS = 512;

interface StoredCheckpoint {
  version: 1;
  checkpoint: RendererCheckpoint | null;
  consumedText: string[];
  consumedAttachments: string[];
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function boundedString(value: unknown, limit: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= limit;
}

function revision(value: unknown): value is string {
  return typeof value === 'string' && /^[a-zA-Z0-9_.:-]{1,96}$/.test(value);
}

function revisions(value: unknown, maximum: number): value is string[] {
  return Array.isArray(value) && value.length <= maximum && value.every(revision);
}

function parseAttachment(raw: unknown): RendererDraftAttachment {
  if (!object(raw) || !boundedString(raw.id, 512) || !boundedString(raw.name, 1_024)
    || !boundedString(raw.path, 4_096) || !revision(raw.revision)
    || (raw.type !== 'file' && raw.type !== 'image')) {
    throw new Error('Invalid renderer draft attachment');
  }
  const size = raw.size;
  if (size !== undefined && (typeof size !== 'number' || !Number.isSafeInteger(size) || size < 0)) {
    throw new Error('Invalid renderer draft attachment size');
  }
  return {
    id: raw.id, name: raw.name, path: raw.path, type: raw.type, revision: raw.revision,
    ...(size !== undefined ? { size } : {}),
  };
}

function parseCheckpoint(raw: unknown): RendererCheckpoint {
  if (!object(raw) || !boundedString(raw.workspaceId, 512)
    || (raw.sessionId !== null && !boundedString(raw.sessionId, 400))
    || typeof raw.draftSession !== 'boolean' || !Array.isArray(raw.attachments)
    || raw.attachments.length > MAX_ATTACHMENTS) {
    throw new Error('Invalid renderer checkpoint');
  }
  const text = raw.text;
  if (text !== null && (!object(text) || !boundedString(text.value, MAX_CHAT_FRAME_BYTES) || !revision(text.revision))) {
    throw new Error('Invalid renderer draft text');
  }
  return {
    workspaceId: raw.workspaceId, sessionId: raw.sessionId, draftSession: raw.draftSession,
    text: text === null ? null : { value: text.value as string, revision: text.revision as string },
    attachments: raw.attachments.map(parseAttachment),
  };
}

function withinHistory(previous: string[], additions: string[]): string[] {
  return [...new Set([...previous, ...additions])].slice(-MAX_RECENT_REVISIONS);
}

function withoutConsumed(record: StoredCheckpoint, checkpoint: RendererCheckpoint): RendererCheckpoint {
  const textRevision = checkpoint.text?.revision;
  return omitSubmittedComposer(checkpoint, {
    textRevision: textRevision && record.consumedText.includes(textRevision) ? textRevision : null,
    attachmentRevisions: record.consumedAttachments,
  });
}

export class RendererCheckpointStore {
  private tail: Promise<unknown> = Promise.resolve();

  constructor(private readonly root: string) {}

  get(scope: string): Promise<RendererCheckpoint | null> {
    return this.serialize(async () => (await this.read(this.file(scope))).checkpoint);
  }

  put(scope: string, raw: unknown): Promise<void> {
    return this.serialize(async () => {
      const checkpoint = parseCheckpoint(raw);
      const file = this.file(scope);
      const record = await this.read(file);
      await this.write(file, { ...record, checkpoint: withoutConsumed(record, checkpoint) });
    });
  }

  consume(scope: string, raw: unknown): Promise<void> {
    if (raw === undefined) return Promise.resolve();
    return this.serialize(async () => {
      if (!object(raw) || (raw.textRevision !== null && !revision(raw.textRevision))
        || !revisions(raw.attachmentRevisions, MAX_ATTACHMENTS)) {
        throw new Error('Invalid renderer checkpoint reference');
      }
      const reference: ComposerCheckpointRef = {
        textRevision: raw.textRevision, attachmentRevisions: raw.attachmentRevisions,
      };
      if (reference.textRevision === null && reference.attachmentRevisions.length === 0) return;
      const file = this.file(scope);
      const record = await this.read(file);
      if ((!reference.textRevision || record.consumedText.includes(reference.textRevision))
        && reference.attachmentRevisions.every((id) => record.consumedAttachments.includes(id))) return;
      const next = {
        ...record,
        checkpoint: record.checkpoint ? omitSubmittedComposer(record.checkpoint, reference) : null,
        consumedText: withinHistory(record.consumedText, reference.textRevision ? [reference.textRevision] : []),
        consumedAttachments: withinHistory(record.consumedAttachments, reference.attachmentRevisions),
      };
      await this.write(file, next);
    });
  }

  private serialize<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.tail.then(operation);
    this.tail = next.catch(() => undefined);
    return next;
  }

  private file(scope: string): string {
    if (!/^[a-f0-9]{64}$/.test(scope)) throw new Error('Invalid renderer checkpoint scope');
    return path.join(this.root, `${scope}.json`);
  }

  private async read(file: string): Promise<StoredCheckpoint> {
    const empty: StoredCheckpoint = { version: 1, checkpoint: null, consumedText: [], consumedAttachments: [] };
    let content: string | undefined;
    try {
      if ((await fs.stat(file)).size <= MAX_RECORD_BYTES) content = await fs.readFile(file, 'utf8');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      return empty;
    }
    if (content !== undefined) {
      try {
        const data: unknown = JSON.parse(content);
        if (object(data) && data.version === 1 && revisions(data.consumedText, MAX_RECENT_REVISIONS)
          && revisions(data.consumedAttachments, MAX_RECENT_REVISIONS)) {
          return {
            version: 1, checkpoint: data.checkpoint === null ? null : parseCheckpoint(data.checkpoint),
            consumedText: data.consumedText, consumedAttachments: data.consumedAttachments,
          };
        }
      } catch {
        // An unreadable record is retained for inspection and replaced on the next write.
      }
    }
    await fs.rename(file, `${file}.corrupt`);
    return empty;
  }

  private async write(file: string, record: StoredCheckpoint): Promise<void> {
    const data = JSON.stringify(record);
    if (Buffer.byteLength(data, 'utf8') > MAX_RECORD_BYTES) throw new Error('Renderer checkpoint is too large');
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    const temporary = `${file}.${randomUUID()}.tmp`;
    try {
      await fs.writeFile(temporary, data, { flag: 'wx', mode: 0o600, flush: true });
      await fs.rename(temporary, file);
    } catch (error) {
      await fs.rm(temporary, { force: true }).catch(() => undefined);
      throw error;
    }
  }
}
