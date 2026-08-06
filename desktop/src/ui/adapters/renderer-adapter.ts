import { backendApi } from '../backend-client';

export interface RendererAdapter {
  readonly bridge: Window['Crew'];
  readonly backend: typeof backendApi;
  now(): number;
}

/** Production dependencies consumed by the shared Renderer root. */
export function createRendererAdapter(target: Window = window): RendererAdapter {
  return {
    bridge: target.Crew,
    backend: backendApi,
    now: Date.now,
  };
}
