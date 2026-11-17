import {
  omitSubmittedComposer,
  type ComposerCheckpointRef,
  type RendererCheckpoint,
  type RendererDraftAttachment,
} from '../../shared/renderer-checkpoint';
import type { DesktopChatOutboxEntry } from '../../shared/chat-outbox';
import { CrewBridge, type Attachment } from '../api/transport';
import { replaceAttachments, setCurrentWorkspaceId, state } from '../state';
import { authStore, messageStore, sessionStore, workspaceStore } from '../stores/stores';
import { findChannelSession } from './channel-sessions';
import {
  composerWorkspaceId,
  createSessionInWorkspace,
  isDraftSession,
  isWorkspaceHidden,
  type OpenSessionFn,
} from './workspaces';

const CHECKPOINT_INTERVAL_MS = 150;

type Navigation = Pick<RendererCheckpoint, 'sessionId' | 'workspaceId' | 'draftSession'>;
interface Binding {
  owner: string;
  scope?: string;
  navigation: Navigation;
  restoring: boolean;
  textChanged: boolean;
  attachmentsChanged: boolean;
  navigationChanged: boolean;
  pendingNavigation?: Navigation;
}

let restoreNavigation: ((open: OpenSessionFn) => Promise<void>) | undefined;

function readNavigation(): Navigation {
  const sessionId = state.activeSessionId;
  return { sessionId, workspaceId: composerWorkspaceId(), draftSession: !!sessionId && isDraftSession(sessionId) };
}

function sameNavigation(left: Navigation, right: Navigation): boolean {
  return left.sessionId === right.sessionId && left.workspaceId === right.workspaceId && left.draftSession === right.draftSession;
}

function restoreEditableDraft(checkpoint: RendererCheckpoint, outbox: DesktopChatOutboxEntry[]): RendererCheckpoint {
  for (const { phase, value } of outbox) {
    if (phase === 'withdrawing') continue;
    const reference = value.composerCheckpoint as ComposerCheckpointRef | undefined;
    if (reference && Array.isArray(reference.attachmentRevisions)) {
      checkpoint = omitSubmittedComposer(checkpoint, reference);
    }
  }
  return checkpoint;
}

export async function restoreRendererCheckpointNavigation(open: OpenSessionFn): Promise<void> {
  await restoreNavigation?.(open);
}

/** Checkpoints only the primary composer in the trusted desktop window. */
export function bindPrimaryRendererCheckpoint(input: HTMLTextAreaElement): {
  capture(): ComposerCheckpointRef | undefined;
  dispose(): void;
} | null {
  const bridge = CrewBridge();
  const get = bridge?.rendererCheckpointGet;
  const put = bridge?.rendererCheckpointPut;
  if (!get || !put) return null;

  let binding: Binding | undefined;
  let hydration: Promise<void> = Promise.resolve();
  let applyingDraft = false;
  let text: RendererCheckpoint['text'] = null;
  let attachmentRevisions = new Map<Attachment, string>();
  let timer: ReturnType<typeof setTimeout> | undefined;

  function readText(): void {
    if (input.value !== (text?.value ?? '')) {
      text = input.value ? { value: input.value, revision: crypto.randomUUID() } : null;
    }
  }

  function readAttachments(): RendererDraftAttachment[] {
    const next = new Map<Attachment, string>();
    const attachments = messageStore.get().attachments.map((attachment) => {
      const revision = attachmentRevisions.get(attachment) ?? crypto.randomUUID();
      next.set(attachment, revision);
      return { ...attachment, revision };
    });
    attachmentRevisions = next;
    return attachments;
  }

  function flush(): void {
    clearTimeout(timer);
    timer = undefined;
    const current = binding;
    if (!current?.scope || current.restoring) return;
    const checkpoint = {
      ...(current.pendingNavigation ?? current.navigation), text, attachments: readAttachments(),
    };
    void put!(current.scope, checkpoint).catch(() => {
      // Persistence failures leave the live composer editable.
    });
  }

  function schedule(): void {
    if (!binding?.scope || binding.restoring || timer !== undefined) return;
    timer = setTimeout(flush, CHECKPOINT_INTERVAL_MS);
  }

  function onText(): void {
    if (!binding || applyingDraft) return;
    readText();
    binding.textChanged = true;
    schedule();
  }

  function onAttachments(): void {
    if (!binding || applyingDraft) return;
    readAttachments();
    binding.attachmentsChanged = true;
    schedule();
  }

  function onNavigation(): void {
    const current = binding;
    if (!current) return;
    const navigation = readNavigation();
    if (sameNavigation(current.navigation, navigation)) return;
    current.navigation = navigation;
    current.navigationChanged = true;
    delete current.pendingNavigation;
    schedule();
  }

  function syncOwner(): void {
    const { isLoggedIn, userInfo } = authStore.get();
    const owner = isLoggedIn ? userInfo?.staffCode : undefined;
    if (owner === binding?.owner) return;
    clearTimeout(timer);
    timer = undefined;
    if (binding) {
      applyingDraft = true;
      text = null;
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      attachmentRevisions.clear();
      replaceAttachments([]);
      applyingDraft = false;
    }
    binding = undefined;
    if (!owner) return;

    readText();
    const attachments = readAttachments();
    const current: Binding = {
      owner, navigation: readNavigation(), restoring: true, textChanged: text !== null,
      attachmentsChanged: attachments.length > 0, navigationChanged: false,
    };
    binding = current;
    hydration = (async () => {
      try {
        // The journal is read before the checkpoint. A send moving to terminal
        // in between the reads is still represented by one of the two files.
        const outbox = await bridge?.chatOutboxList?.();
        const snapshot = await get!();
        if (binding !== current || (outbox && outbox.scope !== snapshot.scope)) return;
        current.scope = snapshot.scope;
        if (!snapshot.checkpoint) return;
        const restored = restoreEditableDraft(snapshot.checkpoint, outbox?.entries ?? []);
        if (!current.navigationChanged) {
          current.pendingNavigation = {
            sessionId: restored.sessionId, workspaceId: restored.workspaceId, draftSession: restored.draftSession,
          };
        }
        applyingDraft = true;
        if (!current.textChanged) {
          text = restored.text;
          input.value = text?.value ?? '';
          input.dispatchEvent(new Event('input', { bubbles: true }));
        }
        if (!current.attachmentsChanged) {
          const attachments = restored.attachments.map(({ revision, ...attachment }) => {
            attachmentRevisions.set(attachment, revision);
            return attachment;
          });
          replaceAttachments(attachments);
        }
      } finally {
        applyingDraft = false;
        current.restoring = false;
        if (binding === current && (current.textChanged || current.attachmentsChanged || current.navigationChanged)) schedule();
      }
    })().catch(() => {});
  }

  async function restore(open: OpenSessionFn): Promise<void> {
    const current = binding;
    await hydration;
    const saved = current?.pendingNavigation;
    if (!current || binding !== current || !saved) return;
    delete current.pendingNavigation;
    const accessible = (workspaceId: string): boolean => workspaceId === 'default' ||
      state.workspaces.some((workspace) => workspace.id === workspaceId && !isWorkspaceHidden(workspace));
    const session = saved.sessionId
      ? state.sessions.find(({ id }) => id === saved.sessionId) ?? findChannelSession(saved.sessionId)
      : undefined;
    if (session && !session.archived && accessible(session.workspaceId)) {
      await open(session.id);
    } else if (saved.draftSession && accessible(saved.workspaceId) && saved.workspaceId !== 'default') {
      createSessionInWorkspace(saved.workspaceId, open);
    } else {
      setCurrentWorkspaceId(accessible(saved.workspaceId) ? saved.workspaceId : 'default');
    }
    if (binding === current) {
      current.navigation = readNavigation();
      schedule();
    }
  }

  input.addEventListener('input', onText);
  const unsubscribeAuth = authStore.subscribe(syncOwner);
  const unsubscribeMessages = messageStore.subscribe((next, previous) => {
    if (next.attachments !== previous.attachments) onAttachments();
  });
  const unsubscribeSessions = sessionStore.subscribe(onNavigation);
  const unsubscribeWorkspace = workspaceStore.subscribe(onNavigation);
  restoreNavigation = restore;
  syncOwner();

  return {
    capture() {
      if (!binding) return undefined;
      readText();
      const reference: ComposerCheckpointRef = {
        textRevision: text?.revision ?? null,
        attachmentRevisions: readAttachments().map(({ revision }) => revision),
      };
      flush();
      return reference;
    },
    dispose() {
      flush();
      binding = undefined;
      input.removeEventListener('input', onText);
      unsubscribeAuth();
      unsubscribeMessages();
      unsubscribeSessions();
      unsubscribeWorkspace();
      if (restoreNavigation === restore) restoreNavigation = undefined;
    },
  };
}
