/**
 * config-store：后端配置 + 当前模型 + 模式
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { BackendConfig, Mode } from '../backend-client';
import type { ComposerMode } from '../state';

export interface ConfigStoreState {
  config: BackendConfig | null;
  /** 模型 ID。`__default__` 占位由 gateway 返回填充。 */
  configModel: string;
  mode: Mode;
  composerMode: ComposerMode;
}

export const configStore: Store<ConfigStoreState> = createStore<ConfigStoreState>(
  {
    config: null,
    configModel: '__default__',
    mode: 'agent',
    composerMode: 'craft',
  },
  'config',
);
