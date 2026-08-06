/**
 * 事项详情面板：元数据（工作空间、来源、优先级、截止、版本）+ 活动流。
 * 活动流来自 GET /items/{id}/activity，不可编辑，供查阅。
 */

import { workApi } from '../../backend-client';
import type { WorkItem, WorkItemEvent } from '../../backend-client';
import { epochMilliseconds } from './time';

const FIELD_LABELS: Record<string, string> = {
  workspace_id: '工作空间',
  category: '分类',
  related_system: '关联系统',
  source: '来源',
  sync_status: '同步状态',
  priority: '优先级',
  due_at: '截止',
  ai_session: 'AI 协作',
};

const PRIORITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
};

const RELATED_SYSTEM_LABELS: Record<string, string> = {
  portal: '门户 / 公文',
  mail: '邮件系统',
  calendar: '日程系统',
  todo: '待办系统',
  meeting: '会议系统',
  hr: '人力系统',
};

const SOURCE_LABELS: Record<string, string> = {
  mail: '企业邮箱',
  calendar: '日程系统',
  todo: '待办系统',
  meeting: '会议系统',
  portal: '门户 / 公文',
};

const SYNC_STATUS_LABELS: Record<string, string> = {
  synced: '已同步',
  syncing: '同步中',
  pending: '待回写',
  conflict: '存在冲突',
  failed: '同步失败',
  unavailable: '来源不可用',
};

const EVENT_LABELS: Record<string, string> = {
  created: '创建',
  updated: '更新',
  completed: '完成',
  reopened: '重开',
  postponed: '延期',
  cancelled: '取消',
  archived: '归档',
  stop_tracking: '停止跟踪',
  deleted: '删除',
};

function formatTimestamp(ts: number | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(epochMilliseconds(ts));
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' });
}

/** 渲染活动流单条。 */
function renderEvent(event: WorkItemEvent): HTMLElement {
  const row = document.createElement('div');
  row.className = 'mw-work-item-details__event';
  const label = document.createElement('span');
  label.className = 'mw-work-item-details__event-type';
  label.textContent = EVENT_LABELS[event.event_type] ?? event.event_type;
  const actor = document.createElement('span');
  actor.className = 'mw-work-item-details__event-actor';
  actor.textContent = event.actor || '';
  const time = document.createElement('span');
  time.className = 'mw-work-item-details__event-time';
  time.textContent = formatTimestamp(event.created_at);
  row.append(label, actor, time);
  return row;
}

/**
 * 渲染事项详情面板：元数据表格 + 活动流。
 * 活动流异步加载，加载中显示占位、失败显示错误但不阻塞元数据。
 */
export async function renderItemDetails(
  container: HTMLElement,
  item: WorkItem,
): Promise<void> {
  container.className = 'mw-work-item-details';
  container.innerHTML = '';

  // ── 元数据 ──
  const meta = document.createElement('dl');
  meta.className = 'mw-work-item-details__meta';
  const fields: Array<[string, string]> = [
    ['workspace_id', item.workspace_id || '默认工作空间'],
    ['category', item.category || '未分类'],
    [
      'related_system',
      RELATED_SYSTEM_LABELS[item.related_system ?? ''] ?? item.related_system ?? '未关联',
    ],
    ['priority', PRIORITY_LABELS[item.priority ?? ''] ?? '未设置'],
    ['due_at', formatTimestamp(item.due_at)],
    ['ai_session', item.processing_session_id ? '已关联' : '未关联'],
  ];
  if (item.source) {
    fields.splice(1, 0, [
      'source',
      `${SOURCE_LABELS[item.source.connector_key] ?? item.source.connector_key} · ${item.source.external_id}`,
    ]);
    fields.splice(2, 0, [
      'sync_status',
      SYNC_STATUS_LABELS[item.sync_status ?? ''] ?? item.sync_status ?? '状态未知',
    ]);
  }
  for (const [key, value] of fields) {
    const dt = document.createElement('dt');
    dt.textContent = FIELD_LABELS[key] ?? key;
    const dd = document.createElement('dd');
    dd.textContent = value;
    meta.append(dt, dd);
  }
  container.append(meta);

  if (item.source) {
    const sourceActions = document.createElement('div');
    sourceActions.className = 'mw-work-item-details__source-actions';
    container.append(sourceActions);
    try {
      const { items } = await workApi.listSourceRecords(item.source.connector_key);
      const record = items.find((candidate) =>
        candidate.external_id === item.source?.external_id);
      if (record) {
        const synced = document.createElement('span');
        synced.className = 'mw-work-item-details__source-time';
        synced.textContent = `最近同步：${formatTimestamp(record.updated_at)}`;
        sourceActions.append(synced);
        if (record.source_url) {
          const open = document.createElement('button');
          open.type = 'button';
          open.className = 'mw-work-item-details__source-open';
          open.textContent = '在工作浏览器打开';
          open.addEventListener('click', () => {
            void import('../browser-panel').then(({ openUserBrowser }) =>
              openUserBrowser(record.source_url));
          });
          sourceActions.append(open);
        }
      }
    } catch {
      sourceActions.textContent = '来源详情暂不可用';
    }
  }

  // ── 活动流 ──
  const activityHeader = document.createElement('h3');
  activityHeader.className = 'mw-work-item-details__activity-header';
  activityHeader.textContent = '活动';
  container.append(activityHeader);

  const activitySlot = document.createElement('div');
  activitySlot.className = 'mw-work-item-details__activity';
  activitySlot.textContent = '加载中…';
  container.append(activitySlot);

  try {
    const result = await workApi.getItemActivity(item.item_id);
    activitySlot.innerHTML = '';
    if (result.events.length === 0) {
      activitySlot.textContent = '暂无活动记录';
    } else {
      for (const event of result.events) {
        activitySlot.append(renderEvent(event));
      }
    }
  } catch {
    activitySlot.textContent = '活动加载失败';
  }
}
