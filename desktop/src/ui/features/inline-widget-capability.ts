import { backendApi } from '../backend-client';

/** Enable the existing, authorization-filtered authoring capability for desktop built-in chat. */
export async function ensureInlineWidgetCapability(sessionId: string, workspaceId: string, title: string): Promise<void> {
  await backendApi.ensureSession(sessionId, { workspace_id: workspaceId, title });
  const config = await backendApi.getSessionAgentConfig(sessionId);
  if (config.executor && config.executor !== 'builtin') return;
  const profiles = config.capability_profiles || [];
  if (profiles.includes('blueprint.authoring') || profiles.includes('sites.authoring')) return;
  await backendApi.setSessionAgentConfig(sessionId, {
    ...config, executor: 'builtin', capability_profiles: [...profiles, 'blueprint.authoring'],
    // agent-config PUT accepts these as session metadata, not executor settings.
    workspace_id: workspaceId, title,
  });
}
