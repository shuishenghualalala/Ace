import { createStore, type Store } from '../reducers/store-bus';

export type ProductMode = 'assistant' | 'work';

export interface ProductModeViewState {
  lastPosition: string;
  navigationCollapsed: boolean;
  historyFilter: string;
}

export interface ProductModeStoreState {
  productMode: ProductMode;
  views: Record<ProductMode, ProductModeViewState>;
}

export const PRODUCT_MODE_STORAGE_KEY = 'Crew.productMode.v1';

export function defaultProductModeState(): ProductModeStoreState {
  return {
    productMode: 'assistant',
    views: {
      assistant: {
        lastPosition: 'chat',
        navigationCollapsed: false,
        historyFilter: '',
      },
      work: {
        lastPosition: 'workbench',
        navigationCollapsed: false,
        historyFilter: '',
      },
    },
  };
}

export const productModeStore: Store<ProductModeStoreState> =
  createStore<ProductModeStoreState>(defaultProductModeState(), 'product-mode');

function isViewState(value: unknown): value is ProductModeViewState {
  if (!value || typeof value !== 'object') return false;
  const view = value as Record<string, unknown>;
  return (
    typeof view.lastPosition === 'string' &&
    typeof view.navigationCollapsed === 'boolean' &&
    typeof view.historyFilter === 'string'
  );
}

function parseSnapshot(value: string | null): ProductModeStoreState | null {
  if (!value) return null;
  try {
    const snapshot = JSON.parse(value) as Record<string, unknown>;
    const views = snapshot.views as Record<string, unknown> | undefined;
    if (
      (snapshot.productMode !== 'assistant' && snapshot.productMode !== 'work') ||
      !views ||
      !isViewState(views.assistant) ||
      !isViewState(views.work)
    ) {
      return null;
    }
    return {
      productMode: snapshot.productMode,
      views: {
        assistant: { ...views.assistant },
        work: { ...views.work },
      },
    };
  } catch {
    return null;
  }
}

function persist(storage: Storage): void {
  storage.setItem(PRODUCT_MODE_STORAGE_KEY, JSON.stringify(productModeStore.get()));
}

/** Restores a validated product snapshot or the assistant defaults. */
export function restoreProductMode(storage: Storage = localStorage): void {
  const snapshot =
    parseSnapshot(storage.getItem(PRODUCT_MODE_STORAGE_KEY)) ?? defaultProductModeState();
  // ponytail: 办公助手 (work) 模式已移除——把历史里残留的 work 选择强制回落到 assistant。
  if (snapshot.productMode === 'work') snapshot.productMode = 'assistant';
  productModeStore.replace(snapshot);
}

/** Switches product UI without touching Agent execution or session state. */
export function setProductMode(
  productMode: ProductMode,
  storage: Storage = localStorage,
): void {
  // ponytail: 办公助手 (work) 模式已移除——拒绝任何再次进入 work 的调用（如事项通知点击）。
  if (productMode === 'work') return;
  productModeStore.set({ productMode });
  persist(storage);
}

/** Updates only the active product mode's navigation snapshot. */
export function updateProductModeView(
  patch: Partial<ProductModeViewState>,
  storage: Storage = localStorage,
): void {
  const state = productModeStore.get();
  productModeStore.set({
    views: {
      ...state.views,
      [state.productMode]: {
        ...state.views[state.productMode],
        ...patch,
      },
    },
  });
  persist(storage);
}
