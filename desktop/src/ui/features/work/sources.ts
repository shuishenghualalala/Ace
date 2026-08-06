/**
 * 来源与同步设置：组织允许来源、个人启停、刷新、异常、冲突解决。
 * 凭据不显示——WorkSourceState 只含状态字段。
 */

import { workApi } from '../../backend-client';
import type { WorkSourceRecord, WorkSourceState } from '../../backend-client';
import { workStore, loadWorkSources } from '../../stores/work-store';
import { openBrowserWorkbench } from '../inspector';
import { refreshDashboard } from './dashboard';
import { loadNotificationSettings, showNotification } from './notifications';

export const SOURCE_STATUS_LABELS: Record<string, string> = {
  disabled: '已停用',
  idle: '待同步',
  syncing: '同步中',
  ready: '已同步',
  error: '错误',
  unavailable: '来源不可用',
  conflict: '存在冲突',
};

const SOURCE_LABELS: Record<string, string> = {
  mail: '邮件',
  'organization-mail': '组织邮件',
  calendar: '日历',
};

export function sourceDisplayName(connectorKey: string): string {
  return SOURCE_LABELS[connectorKey] ?? connectorKey;
}

export async function loadSourceRecords(): Promise<WorkSourceRecord[]> {
  const result = await workApi.listSourceRecords();
  return result.items;
}

export function renderSourceRecords(container: HTMLElement, records: WorkSourceRecord[]): void {
  container.className = 'mw-work-source-records';
  container.replaceChildren();
  if (records.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-sources__empty';
    empty.textContent = '暂无同步记录';
    container.append(empty);
    return;
  }
  for (const record of records) {
    const row = document.createElement('article');
    const title = document.createElement('strong');
    const meta = document.createElement('span');
    row.className = 'mw-work-source-records__row';
    row.dataset.recordId = record.record_id;
    title.textContent = record.title;
    meta.textContent = `${sourceDisplayName(record.connector_key)} · ${SOURCE_STATUS_LABELS[record.sync_status] ?? record.sync_status}`;
    row.append(title, meta);
    if (record.source_url) {
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'mw-work-source-records__open';
      open.textContent = '打开原系统';
      open.addEventListener('click', () => openBrowserWorkbench({ url: record.source_url }));
      row.append(open);
    }
    if (record.sync_status === 'conflict') {
      const values = document.createElement('dl');
      const externalLabel = document.createElement('dt');
      const externalValue = document.createElement('dd');
      const localLabel = document.createElement('dt');
      const localValue = document.createElement('dd');
      values.className = 'mw-work-source-records__conflict';
      externalLabel.textContent = '外部值';
      externalValue.textContent = JSON.stringify(record.conflict_external);
      localLabel.textContent = 'Crew 待提交值';
      localValue.textContent = JSON.stringify(record.conflict_local);
      values.append(externalLabel, externalValue, localLabel, localValue);
      row.append(values);
      for (const [resolution, label] of [['external', '采用外部版本'], ['local', '回写本地修改']] as const) {
        const resolve = document.createElement('button');
        resolve.type = 'button';
        resolve.className = 'mw-work-source-records__resolve';
        resolve.textContent = label;
        resolve.addEventListener('click', () => {
          resolve.disabled = true;
          void workApi.resolveSourceConflict(record.record_id, resolution)
            .then(async (updated) => {
              await refreshDashboard();
              renderSourceRecords(container, records.map((item) =>
                item.record_id === updated.record_id ? updated : item));
            })
            .catch((error) => {
              resolve.disabled = false;
              resolve.title = error instanceof Error ? error.message : String(error);
            });
        });
        row.append(resolve);
      }
    }
    container.append(row);
  }
}

/** 启停来源。 */
export async function toggleSource(connectorKey: string, enabled: boolean): Promise<void> {
  await workApi.toggleSource(connectorKey, enabled);
  await loadWorkSources();
}

/** 刷新来源。 */
export async function refreshSource(connectorKey: string): Promise<void> {
  const previousConflicts = new Set(
    (await loadSourceRecords()).filter((record) => record.sync_status === 'conflict').map((record) => record.record_id),
  );
  await workApi.refreshSource(connectorKey);
  await Promise.all([loadWorkSources(), refreshDashboard()]);
  const settings = await loadNotificationSettings();
  for (const record of await loadSourceRecords()) {
    if (record.sync_status === 'conflict' && !previousConflicts.has(record.record_id)) {
      showNotification('来源同步冲突', record.title, null, settings, connectorKey);
    }
  }
}

/** 删除一个来源的本地同步记录；外部数据和已生成事项不受影响。 */
export async function deleteSourceLocalData(connectorKey: string): Promise<number> {
  const result = await workApi.deleteSourceLocalData(connectorKey);
  await loadWorkSources();
  return result.deleted_records;
}

/** 渲染来源列表。凭据字段不显示。 */
export function renderSources(
  container: HTMLElement,
  sources: WorkSourceState[],
  onDataChanged?: () => void,
): void {
  container.className = 'mw-work-sources';
  container.innerHTML = '';
  if (sources.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-sources__empty';
    empty.textContent = '尚未配置可用的数据来源';
    container.append(empty);
    return;
  }
  for (const source of sources) {
    const row = document.createElement('div');
    row.className = 'mw-work-sources__row';
    row.dataset.connectorKey = source.connector_key;
    if (!source.enabled) row.dataset.disabled = 'true';

    const name = document.createElement('span');
    name.className = 'mw-work-sources__name';
    name.textContent = sourceDisplayName(source.connector_key);

    const status = document.createElement('span');
    status.className = 'mw-work-sources__status';
    status.textContent = SOURCE_STATUS_LABELS[source.status] ?? source.status;
    if (source.last_error) status.title = source.last_error;

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'mw-work-sources__toggle';
    toggle.textContent = source.enabled ? '停用' : '启用';
    toggle.addEventListener('click', () => {
      toggle.disabled = true;
      void toggleSource(source.connector_key, !source.enabled)
        .then(() => renderSources(container, workStore.get().sources))
        .catch((error) => {
          toggle.disabled = false;
          status.textContent = `操作失败：${error instanceof Error ? error.message : String(error)}`;
          status.dataset.state = 'error';
        });
    });

    const refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.className = 'mw-work-sources__refresh';
    refresh.textContent = '刷新';
    refresh.addEventListener('click', () => {
      refresh.disabled = true;
      status.textContent = '同步中';
      void refreshSource(source.connector_key)
        .then(() => renderSources(container, workStore.get().sources))
        .catch((error) => {
          refresh.disabled = false;
          status.textContent = `刷新失败：${error instanceof Error ? error.message : String(error)}`;
          status.dataset.state = 'error';
        });
    });

    const deleteLocal = document.createElement('button');
    deleteLocal.type = 'button';
    deleteLocal.className = 'mw-work-sources__delete-local';
    deleteLocal.textContent = '删除本地数据';
    deleteLocal.addEventListener('click', () => {
      if (!window.confirm('确认删除此来源的本地同步数据？外部系统数据、既有事项和处理历史不会删除。')) return;
      deleteLocal.disabled = true;
      void deleteSourceLocalData(source.connector_key)
        .then((count) => {
          status.textContent = `已删除 ${count} 条本地同步记录`;
          onDataChanged?.();
        })
        .catch((error) => {
          deleteLocal.disabled = false;
          status.dataset.state = 'error';
          status.textContent = `删除失败：${error instanceof Error ? error.message : String(error)}`;
        });
    });

    row.append(name, status, toggle, refresh, deleteLocal);
    container.append(row);
  }
}
