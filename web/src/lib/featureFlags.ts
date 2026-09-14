import type { AppConfig } from "../types";

/** 只有 Gateway 明确返回 false 时才关闭外援；加载中或暂时断连不降级为关闭。 */
export function externalAgentsAvailable(config: AppConfig | null): boolean {
  return config?.external_agents?.enabled !== false;
}

/** Team / 动态看板在 /api/config feature_capabilities 中的 feature id，与后端约定对齐。 */
export const TEAM_FEATURE_ID = "product.team";
export const KANBAN_FEATURE_ID = "product.dynamic-kanban";

function featureCapabilityAvailable(config: AppConfig | null, featureId: string): boolean {
  // config 未加载或旧后端没有 feature_capabilities 字段时不降级为关闭，保持今日行为。
  if (config == null || config.feature_capabilities == null) return true;
  return Boolean(config.feature_capabilities[featureId]?.available);
}

/** Team 能力是否可用；config 未加载或 feature_capabilities 字段缺席时按可用处理，映射存在而条目缺席则不可用。 */
export function teamFeatureAvailable(config: AppConfig | null): boolean {
  return featureCapabilityAvailable(config, TEAM_FEATURE_ID);
}

/** 动态看板能力是否可用；config 未加载或 feature_capabilities 字段缺席时按可用处理，映射存在而条目缺席则不可用。 */
export function kanbanFeatureAvailable(config: AppConfig | null): boolean {
  return featureCapabilityAvailable(config, KANBAN_FEATURE_ID);
}
