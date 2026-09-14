import type { AppConfig } from "../types";

/** Team / 动态看板 / 外援在 /api/config feature_capabilities 中的 feature id，与后端约定对齐。 */
export const TEAM_FEATURE_ID = "product.team";
export const KANBAN_FEATURE_ID = "product.dynamic-kanban";
export const EXTERNAL_AGENTS_FEATURE_ID = "product.external-agents";

function featureCapabilityAvailable(config: AppConfig | null, featureId: string): boolean {
  // config 未加载或旧后端没有 feature_capabilities 字段时不降级为关闭，保持今日行为。
  if (config == null || config.feature_capabilities == null) return true;
  return Boolean(config.feature_capabilities[featureId]?.available);
}

/** 外援（AgentsHub / 侧边栏入口）能力是否可用；config 未加载或 feature_capabilities 字段缺席时按可用处理，映射存在而条目缺席则不可用。 */
export function externalAgentsAvailable(config: AppConfig | null): boolean {
  return featureCapabilityAvailable(config, EXTERNAL_AGENTS_FEATURE_ID);
}

/** Team 能力是否可用；config 未加载或 feature_capabilities 字段缺席时按可用处理，映射存在而条目缺席则不可用。 */
export function teamFeatureAvailable(config: AppConfig | null): boolean {
  return featureCapabilityAvailable(config, TEAM_FEATURE_ID);
}

/** 动态看板能力是否可用；config 未加载或 feature_capabilities 字段缺席时按可用处理，映射存在而条目缺席则不可用。 */
export function kanbanFeatureAvailable(config: AppConfig | null): boolean {
  return featureCapabilityAvailable(config, KANBAN_FEATURE_ID);
}
