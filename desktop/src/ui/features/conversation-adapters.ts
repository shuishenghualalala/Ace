import type { Attachment } from '../backend-client';

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
}

export interface ConversationAdapter {
  readonly id: string;
  matches(sessionId: string): boolean;
  abilities(sessionId: string): ConversationAbilities;
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
