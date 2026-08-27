/**
 * config-store：后端配置 + 当前模型 + 模式
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { BackendConfig, Mode } from '../backend-client';
import type { ComposerMode } from '../state';

export interface ConfigStoreState {
  config: BackendConfig | null;
  /** 当前模型 ID；空字符串表示尚未配置模型。 */
  configModel: string;
  mode: Mode;
  composerMode: ComposerMode;
}

export const configStore: Store<ConfigStoreState> = createStore<ConfigStoreState>(
  {
    config: null,
    configModel: '',
    mode: 'agent',
    composerMode: 'craft',
  },
  'config',
);
