/**
 * Work @ 提及：搜索事项、会话、知识与来源记录，选择后生成可查看/移除的引用标签。
 * Agent 会话选中时创建只读快照引用（不扩大文件权限）。
 *
 * 不复制 fetch——搜索经 workApi.searchMentions，引用经 workApi.createReference /
 * createAgentSessionReference / deleteReference。
 */

import { workApi } from '../../backend-client';
import type { WorkReference } from '../../backend-client';

export interface MentionResult {
  entity_type: 'work_item' | 'work_session' | 'agent_session'
    | 'personal_knowledge' | 'source_record' | 'browser_tab';
  id: string;
  title: string;
  workspace_id?: string;
  source_link?: string;
}

export interface MentionTag {
  result: MentionResult;
  reference?: WorkReference;
}

const ENTITY_LABELS: Record<MentionResult['entity_type'], string> = {
  work_item: '事项',
  work_session: '会话',
  agent_session: 'Agent',
  personal_knowledge: '个人知识',
  source_record: '来源',
  browser_tab: '标签页',
};

/** 搜索提及候选；空查询返回空数组。 */
export async function searchMentions(query: string, workspaceId?: string | null): Promise<MentionResult[]> {
  const q = query.trim();
  if (!q) return [];
  const raw = await workApi.searchMentions(q, workspaceId);
  return raw.filter((r): r is MentionResult => r.entity_type in ENTITY_LABELS);
}

/**
 * 选择一个提及候选：
 * - Agent 会话 → 创建只读快照引用（createAgentSessionReference），不扩大文件权限
 * - 其他类型 → 创建普通上下文引用
 *
 * targetWorkSessionId 是当前 Work 对话；候选 Agent 会话是快照来源。
 */
export async function selectMention(
  result: MentionResult,
  targetWorkSessionId: string,
): Promise<MentionTag> {
  if (result.entity_type === 'agent_session') {
    const reference = await workApi.createAgentSessionReference({
      target_session_id: targetWorkSessionId,
      source_session_id: result.id,
    });
    return { result, reference };
  }
  if (result.entity_type === 'browser_tab') {
    // 浏览器标签页：标题存 snapshot_summary、URL 存 source_link；
    // 发送时后端按 @browser_tab:<id> 注入标签页正文。
    const reference = await workApi.createReference({
      target_session_id: targetWorkSessionId,
      reference_type: 'browser_tab',
      source_id: result.id,
      snapshot_summary: result.title,
      ...(result.source_link ? { source_link: result.source_link } : {}),
    });
    return { result, reference };
  }
  const reference = await workApi.createReference({
    target_session_id: targetWorkSessionId,
    reference_type: result.entity_type,
    source_id: result.id,
    ...(result.source_link ? { source_link: result.source_link } : {}),
  });
  return { result, reference };
}

/** 移除一个提及标签；如果有引用则删除引用。 */
export async function removeMentionTag(
  tag: MentionTag,
): Promise<void> {
  if (tag.reference) {
    await workApi.deleteReference(tag.reference.reference_id);
  }
}

/** 刷新 Agent 快照引用（重新拉取来源摘要）。 */
export async function refreshSnapshot(
  referenceId: string,
): Promise<WorkReference> {
  return workApi.refreshReference(referenceId);
}

/** 渲染提及搜索结果列表到容器。 */
export function renderMentionResults(
  container: HTMLElement,
  results: MentionResult[],
  onSelect: (result: MentionResult) => void,
): void {
  container.className = 'mw-work-mentions__results';
  container.innerHTML = '';
  if (results.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-mentions__empty';
    empty.textContent = '无匹配结果';
    container.append(empty);
    return;
  }
  for (const result of results) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'mw-work-mentions__result';
    row.dataset.entityType = result.entity_type;
    row.dataset.id = result.id;
    const badge = document.createElement('span');
    badge.className = 'mw-work-mentions__entity-badge';
    badge.textContent = ENTITY_LABELS[result.entity_type] ?? result.entity_type;
    const title = document.createElement('span');
    title.className = 'mw-work-mentions__result-title';
    title.textContent = result.title;
    row.append(badge, title);
    row.addEventListener('click', () => onSelect(result));
    container.append(row);
  }
}

/** 渲染已选提及标签列表（可查看来源 / 刷新快照 / 移除）。 */
export function renderMentionTags(
  container: HTMLElement,
  tags: MentionTag[],
  onRemove: (tag: MentionTag) => void,
): void {
  container.className = 'mw-work-mentions__tags';
  container.innerHTML = '';
  for (const tag of tags) {
    const chip = document.createElement('span');
    chip.className = 'mw-work-mentions__tag';
    chip.dataset.entityType = tag.result.entity_type;
    const badge = document.createElement('span');
    badge.className = 'mw-work-mentions__entity-badge';
    badge.textContent = ENTITY_LABELS[tag.result.entity_type] ?? tag.result.entity_type;
    const label = document.createElement('span');
    label.className = 'mw-work-mentions__tag-label';
    label.textContent = tag.result.title;
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'mw-work-mentions__tag-remove';
    removeBtn.textContent = '×';
    removeBtn.setAttribute('aria-label', '移除');
    removeBtn.addEventListener('click', () => onRemove(tag));
    chip.append(badge, label);
    if (tag.reference) {
      const refreshBtn = document.createElement('button');
      refreshBtn.type = 'button';
      refreshBtn.className = 'mw-work-mentions__tag-refresh';
      refreshBtn.textContent = '更新';
      refreshBtn.title = '更新到最新内容';
      refreshBtn.addEventListener('click', () => {
        refreshBtn.disabled = true;
        void refreshSnapshot(tag.reference!.reference_id)
          .then((reference) => {
            tag.reference = reference;
            refreshBtn.disabled = false;
            refreshBtn.textContent = '已更新';
          })
          .catch((error) => {
            refreshBtn.disabled = false;
            refreshBtn.textContent = '重试更新';
            refreshBtn.title = error instanceof Error ? error.message : String(error);
          });
      });
      chip.append(refreshBtn);
    }
    chip.append(removeBtn);
    container.append(chip);
  }
}

/** Agent 会话引用是否只读快照（不可在提及中继续对话）。 */
export function isAgentSnapshot(tag: MentionTag): boolean {
  return tag.result.entity_type === 'agent_session' && tag.reference !== undefined;
}
