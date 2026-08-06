/**
 * external-store：外援（external agents）的会话级外源团队绑定。
 */
import { createStore, type Store } from '../reducers/store-bus';

export interface ExternalStoreState {
  /** session_id -> external_team_id（空串 = 未绑定外源团队） */
  activeExternalTeamIdBySession: Record<string, string>;
}

export const externalStore: Store<ExternalStoreState> = createStore<ExternalStoreState>(
  {
    activeExternalTeamIdBySession: {},
  },
  'external',
);
