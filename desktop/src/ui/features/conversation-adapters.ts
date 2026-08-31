import type { Attachment } from '../backend-client';
import type { AvatarRef } from '../avatar-manager';

export type ConversationMemberState = 'online' | 'connecting' | 'offline';
export type ConversationAgentState = 'available' | 'working' | 'unavailable' | 'local';

export interface ConversationMentionTarget {
  kind: 'agent';
  peerId: string;
  publicAgentId: string;
  label: string;
  ownerLabel: string;
  routing: 'specific' | 'peer-default';
  avatar?: AvatarRef;
}

export interface ConversationAgentPresence extends ConversationMentionTarget {
  state: ConversationAgentState;
  stateLabel: string;
  disabledReason?: string;
}

export interface ConversationMemberPresence {
  peerId: string;
  label: string;
  isSelf: boolean;
  state: ConversationMemberState;
  stateLabel: string;
  avatar?: AvatarRef;
  agents: ConversationAgentPresence[];
}

export interface ConversationComposerContext {
  title: string;
  modeLabel: string;
  members: ConversationMemberPresence[];
}

export interface ConversationAbilities {
  canSendText: boolean;
  canAttach: boolean;
  canMentionPeople: boolean;
  canMentionAgents: boolean;
  showModelPicker: boolean;
  showSkills: boolean;
  showPlanMode: boolean;
  unavailableReason?: string;
}

export interface ConversationSendInput {
  sessionId: string;
  text: string;
  attachments: Attachment[];
  mentions?: ConversationMentionTarget[];
}

export interface ConversationAdapter {
  readonly id: string;
  matches(sessionId: string): boolean;
  abilities(sessionId: string): ConversationAbilities;
  composerContext?(sessionId: string): ConversationComposerContext | null;
  subscribe?(listener: () => void): () => void;
  send(input: ConversationSendInput): Promise<void>;
}

class ConversationAdapterRegistry {
  private readonly adapters = new Map<string, ConversationAdapter>();

  register(adapter: ConversationAdapter): () => void {
    this.adapters.set(adapter.id, adapter);
    return () => {
      if (this.adapters.get(adapter.id) === adapter) this.adapters.delete(adapter.id);
    };
  }

  resolve(sessionId: string): ConversationAdapter | null {
    for (const adapter of this.adapters.values()) {
      if (adapter.matches(sessionId)) return adapter;
    }
    return null;
  }
}

export const conversationAdapters = new ConversationAdapterRegistry();
