import { createRendererAdapter } from './adapters/renderer-adapter';
import { mountRenderer } from './app';

let disposeRenderer: (() => void) | null = null;

function startRenderer(): void {
  if (disposeRenderer) return;
  disposeRenderer = mountRenderer(document.body, createRendererAdapter());
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startRenderer, { once: true });
} else {
  startRenderer();
}

window.addEventListener('pagehide', () => {
  disposeRenderer?.();
  disposeRenderer = null;
}, { once: true });
