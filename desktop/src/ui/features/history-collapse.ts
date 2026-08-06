/**
 * Keeps legacy chat layout state in sync with the application Shell context state.
 */

import { state } from '../state';
import { productModeStore } from '../stores/product-mode-store';

function applyCollapsed(collapsed: boolean): void {
  state.historyCollapsed = collapsed;
}

export function applyHistoryCollapsed(): void {
  applyCollapsed(productModeStore.get().views.assistant.navigationCollapsed);
}

export function bindHistoryPanelToggle(): () => void {
  applyHistoryCollapsed();
  return productModeStore.subscribe((next) => {
    applyCollapsed(next.views.assistant.navigationCollapsed);
  });
}
