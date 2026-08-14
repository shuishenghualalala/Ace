/** Work 模板只作为工作台快速开始项，不再拥有独立管理页面。 */

import { workApi, type WorkItem, type WorkTemplate } from '../../backend-client';
import { mergeItem, workStore } from '../../stores/work-store';

/** 实例化模板为带处理会话的工作事项。 */
export async function instantiateTemplate(
  templateId: string,
  payload: Record<string, unknown> = {
    workspace_id: workStore.get().selectedWorkspaceId ?? 'default',
  },
): Promise<WorkItem> {
  const created = await workApi.instantiateTemplate(templateId, payload);
  mergeItem(created);
  return created;
}

/** 按使用次数取工作台 Top-N 快速开始项。 */
export function mostUsed(templates: WorkTemplate[], count: number): WorkTemplate[] {
  return [...templates]
    .sort((left, right) => (right.usage_count ?? 0) - (left.usage_count ?? 0))
    .slice(0, count);
}
