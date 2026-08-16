/**
 * Work @ 提及：搜索事项、会话、知识与来源记录，选择后生成可查看/移除的引用标签。
 * Agent 会话选中时创建只读快照引用（不扩大文件权限）。
 *
 * 不复制 fetch——搜索经 workApi.searchMentions，引用经 workApi.createReference /
 * createAgentSessionReference / deleteReference。
 */

import { workApi } from '../../backend-client';
import type { WorkReference } from '../../backend-client';
import type { IconId } from '../../components/icon';

/** 提及实体类型：即 MENTION_KINDS 注册表键，新增类型只需改注册表。 */
export type MentionEntityType = keyof typeof MENTION_KINDS;

export interface MentionResult {
  entity_type: MentionEntityType;
  id: string;
  title: string;
  workspace_id?: string;
  source_link?: string;
}

export interface MentionTag {
  result: MentionResult;
  reference?: WorkReference;
}

/** 选中候选后创建引用的payload构建入口（selectMention 按类型分派到注册表）。 */
type CreateMentionReference = (result: MentionResult, targetWorkSessionId: string) => Promise<WorkReference>;

/** 普通上下文引用：reference_type 即实体类型，带可选 source_link。 */
const createContextReference: CreateMentionReference = (result, targetWorkSessionId) =>
  workApi.createReference({
    target_session_id: targetWorkSessionId,
    reference_type: result.entity_type,
    source_id: result.id,
    ...(result.source_link ? { source_link: result.source_link } : {}),
  });

/** 浏览器标签页：标题存 snapshot_summary、URL 存 source_link；
 *  发送时后端按 @browser_tab:<id> 注入标签页正文。 */
const createBrowserTabReference: CreateMentionReference = (result, targetWorkSessionId) =>
  workApi.createReference({
    target_session_id: targetWorkSessionId,
    reference_type: 'browser_tab',
    source_id: result.id,
    snapshot_summary: result.title,
    ...(result.source_link ? { source_link: result.source_link } : {}),
  });

/** Agent 会话 → 创建只读快照引用（createAgentSessionReference），不扩大文件权限。 */
const createAgentSnapshotReference: CreateMentionReference = (result, targetWorkSessionId) =>
  workApi.createAgentSessionReference({
    target_session_id: targetWorkSessionId,
    source_session_id: result.id,
  });

export interface MentionKind {
  /** 结果行与引用标签上的短徽标文案（mentions 标签语境）。 */
  label: string;
  /** 补全弹窗候选右侧的元数据文案（composer 弹窗语境）。 */
  meta: string;
  /** 弹窗候选图标类别（决定 CSS 类 mention-pop__sig--*）。 */
  sig: 'file' | 'tab';
  /** 弹窗候选左侧图标。 */
  icon: IconId;
  /** 选中后创建引用。 */
  createReference: CreateMentionReference;
}

/**
 * @ 提及类型注册表：entity_type union、补全正则前缀、弹窗图标/元数据、标签徽标、
 * 引用创建全部由此派生——新增一种类型（如 wiki_page）只需在此加一行。
 * label 与 meta 是两套历史措辞（标签短徽标 vs 弹窗元数据），保持各自现状未统一。
 */
export const MENTION_KINDS = {
  work_item: { label: '事项', meta: '事项', sig: 'file', icon: 'icon-file', createReference: createContextReference },
  work_session: { label: '会话', meta: 'Work 会话', sig: 'file', icon: 'icon-file', createReference: createContextReference },
  agent_session: { label: 'Agent', meta: 'Agent 会话快照', sig: 'file', icon: 'icon-file', createReference: createAgentSnapshotReference },
  personal_knowledge: { label: '个人知识', meta: '个人知识', sig: 'file', icon: 'icon-file', createReference: createContextReference },
  source_record: { label: '来源', meta: '来源记录', sig: 'file', icon: 'icon-file', createReference: createContextReference },
  browser_tab: { label: '标签页', meta: '浏览器标签页', sig: 'tab', icon: 'process-web', createReference: createBrowserTabReference },
} satisfies Record<string, MentionKind>;

/** 搜索提及候选；空查询返回空数组。 */
export async function searchMentions(query: string, workspaceId?: string | null): Promise<MentionResult[]> {
  const q = query.trim();
  if (!q) return [];
  const raw = await workApi.searchMentions(q, workspaceId);
  return raw.filter((r): r is MentionResult => r.entity_type in MENTION_KINDS);
}

/**
 * 选择一个提及候选：按注册表分派创建引用（Agent 会话走只读快照接口，不扩大文件权限；
 * 浏览器标签页把标题存 snapshot_summary）。
 *
 * targetWorkSessionId 是当前 Work 对话；候选 Agent 会话是快照来源。
 */
export async function selectMention(
  result: MentionResult,
  targetWorkSessionId: string,
): Promise<MentionTag> {
  const reference = await MENTION_KINDS[result.entity_type].createReference(result, targetWorkSessionId);
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
    badge.textContent = MENTION_KINDS[result.entity_type]?.label ?? result.entity_type;
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
    badge.textContent = MENTION_KINDS[tag.result.entity_type]?.label ?? tag.result.entity_type;
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
