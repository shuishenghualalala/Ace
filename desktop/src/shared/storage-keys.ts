/**
 * 渲染层 localStorage key 集中注册表。
 *
 * 目的：
 * 1. 避免散落在多个模块的字符串字面量造成拼写错误 / 不一致。
 * 2. 提供给未来的迁移 / 清理 / 加密层使用。
 *
 * 注意：这是"软"约定 —— TypeScript 类型 + grep 双重保证。
 * 任何 setItem / getItem 调用都必须使用此处的常量，不允许再写字面量。
 */

export const STORAGE_KEYS = {
  /** gateway base URL（默认 http://127.0.0.1:8000）。 */
  gatewayBase: 'Crew.gatewayBase',
  /** 当前登录用户的 userInfo（明文）。 */
  userInfo: 'Crew.userInfo',
  /** 使用统计的模型定价。 */
  usagePricing: 'Crew.usage.pricing.v1',
  /** 使用统计的滚动 500 条记录（按账号 staffCode 分桶，见 usage-tracker）。 */
  usageRecords: 'Crew.usage.records.v1',
  /** 检查器面板宽度。 */
  inspectorWidth: 'crew.inspector.width',
  /** 应用外壳上下文栏（会话列表）宽度。 */
  contextWidth: 'crew.context.width',
  /** 设置页偏好。 */
  settings: 'crew.settings',
  /** 历史面板折叠状态。 */
  historyCollapsed: 'crew.history.collapsed',
  /** 反馈草稿。 */
  feedbackDraft: 'Crew.feedbackDraft',
  /** Desktop 外援中心首次使用引导是否已关闭。 */
  externalAgentsGuideDismissed: 'Crew.externalAgents.guideDismissed.v1',
} as const;

export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS];
