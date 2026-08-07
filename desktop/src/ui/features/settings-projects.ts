/**
 * Settings embeds the same Workspace owner used by the sidebar dialog.
 */

import { state } from '../state';
import { workspaceStore } from '../stores/workspace-store';
import { createWorkspaceView, type WorkspaceView } from './workspace-view';
import { syncLibrarySectionCounts } from './settings-library';
import { openSession } from './session-controller';
import { createWorkspaceViewOptions, loadWorkspaces } from './workspaces';

let mountedView: WorkspaceView | null = null;
let unsubscribeWorkspace: (() => void) | null = null;
let renderGeneration = 0;

function projectsRoot(): HTMLElement | null {
  return document.getElementById('settings-library-projects')
    ?? document.getElementById('settings-projects-root');
}

function syncCount(): void {
  syncLibrarySectionCounts({
    projects: state.workspaces.filter((workspace) => workspace.id !== 'default').length,
  });
}

function clearMountedProjectsPage(): void {
  mountedView?.dispose();
  mountedView = null;
  unsubscribeWorkspace?.();
  unsubscribeWorkspace = null;
}

/** Releases the Settings-owned Workspace view and store subscription. */
export function disposeSettingsProjectsPage(): void {
  renderGeneration += 1;
  clearMountedProjectsPage();
}

/** Renders the shared Workspace navigator inside Settings. */
export async function renderSettingsProjectsPage(): Promise<void> {
  const root = projectsRoot();
  if (!root) return;
  const generation = ++renderGeneration;
  await loadWorkspaces();
  if (generation !== renderGeneration) return;
  clearMountedProjectsPage();
  mountedView = createWorkspaceView(
    root,
    {
      ...createWorkspaceViewOptions(openSession),
      showDefault: false,
    },
    state.workspaces.find((workspace) => workspace.id !== 'default')?.id ?? '',
  );
  syncCount();
  unsubscribeWorkspace = workspaceStore.subscribe(syncCount);
}
