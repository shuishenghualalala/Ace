import type { ReactNode } from "react";
import { useEffect, useRef } from "react";
import AgentsHub from "../components/AgentsHub";
import { UiPageRegistry, type UiPageContribution } from "../lib/ui-feature-registry";
import type { ExternalAgent, ExternalTeam } from "../types";

export interface AgentsPageContext {
  agentsEnabled: boolean;
  props: Props;
}

export type AgentsPageContribution = UiPageContribution<string, AgentsPageContext, ReactNode>;
export type AgentsPageRegistry = UiPageRegistry<string, AgentsPageContext, ReactNode>;

export function installAgentsPageContribution(registry: AgentsPageRegistry): () => void {
  return registry.register({
    id: "agents",
    isAvailable: ({ agentsEnabled }) => agentsEnabled,
    render: ({ props }) => <AgentsFeature {...props} />,
  });
}

interface Props {
  onAssignAgent: (agent: ExternalAgent) => void | Promise<void>;
  onAssignTeam: (team: ExternalTeam) => void | Promise<void>;
  onStartLeaderChat: (agent: ExternalAgent) => void | Promise<void>;
}

/**
 * 外援页面 Feature 单元。
 *
 * 只在 capability 启用且被 registry 投影时挂载；卸载后通过 mountedRef
 * 守卫外部动作回调，避免卸载后再切会话/改状态。
 */
export default function AgentsFeature({
  onAssignAgent,
  onAssignTeam,
  onStartLeaderChat,
}: Props) {
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  return (
    <AgentsHub
      onAssignAgent={async (agent) => {
        if (!mountedRef.current) return;
        await onAssignAgent(agent);
      }}
      onAssignTeam={async (team) => {
        if (!mountedRef.current) return;
        await onAssignTeam(team);
      }}
      onStartLeaderChat={async (agent) => {
        if (!mountedRef.current) return;
        await onStartLeaderChat(agent);
      }}
    />
  );
}
