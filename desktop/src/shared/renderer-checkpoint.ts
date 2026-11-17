export interface ComposerCheckpointRef {
  textRevision: string | null;
  attachmentRevisions: string[];
}

export interface RendererDraftAttachment {
  id: string;
  name: string;
  path: string;
  type: 'image' | 'file';
  size?: number;
  revision: string;
}

export interface RendererCheckpoint {
  workspaceId: string;
  sessionId: string | null;
  draftSession: boolean;
  text: { value: string; revision: string } | null;
  attachments: RendererDraftAttachment[];
}

export interface RendererCheckpointSnapshot {
  scope: string;
  checkpoint: RendererCheckpoint | null;
}

/** A draft can change while a previously captured send waits for a receipt. */
export function omitSubmittedComposer(
  checkpoint: RendererCheckpoint,
  reference: ComposerCheckpointRef,
): RendererCheckpoint {
  const sent = new Set(reference.attachmentRevisions);
  return {
    ...checkpoint,
    text: checkpoint.text?.revision === reference.textRevision ? null : checkpoint.text,
    attachments: checkpoint.attachments.filter(({ revision }) => !sent.has(revision)),
  };
}
