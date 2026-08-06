/**
 * 工作偏好：自动学习总开关、手动新增、偏好列表、编辑/暂停/删除。
 * 已应用偏好及优先级可见；关闭后停止提取/应用；权限不提升。
 */

import { workApi } from '../../backend-client';
import type { WorkPreference } from '../../backend-client';
import { workStore, loadWorkPreferences } from '../../stores/work-store';
import {
  loadNotificationSettings,
  saveNotificationSettings,
  type NotificationSettings,
} from './notifications';

/** 切换自动学习总开关。 */
export async function setAutoLearning(enabled: boolean): Promise<void> {
  await workApi.setPreferenceSettings(enabled);
  await loadWorkPreferences();
}

/** 新增一条账号级手动工作偏好。 */
export async function createPreference(category: string, content: string): Promise<void> {
  await workApi.createPreference({ category: category.trim(), content: content.trim() });
  await loadWorkPreferences();
}

/** 编辑一个偏好的文本内容。 */
export async function editPreference(preference: WorkPreference, content: string): Promise<void> {
  await workApi.updatePreference(preference.preference_id, {
    expected_version: preference.version,
    content: content.trim(),
  });
  await loadWorkPreferences();
}

/** 暂停或恢复一个偏好。 */
export async function setPreferencePaused(
  preference: WorkPreference,
  paused: boolean,
): Promise<void> {
  await workApi.updatePreference(preference.preference_id, {
    expected_version: preference.version,
    status: paused ? 'paused' : 'active',
  });
  await loadWorkPreferences();
}

/** 删除一个偏好。调用方负责提供撤销窗口。 */
export async function deletePreference(preference: WorkPreference): Promise<void> {
  await workApi.deletePreference(preference.preference_id, preference.version);
  await loadWorkPreferences();
}

/** 渲染免打扰设置区（DND + 来源通知开关）。 */
export function renderDndSettings(container: HTMLElement, settings: NotificationSettings): void {
  container.className = 'mw-work-preferences__dnd';
  container.innerHTML = '';
  const label = document.createElement('span');
  label.className = 'mw-work-preferences__toggle-label';
  label.textContent = '免打扰';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'mw-work-preferences__toggle';
  toggle.textContent = settings.dnd_enabled ? '已开启' : '已关闭';
  toggle.dataset.enabled = String(settings.dnd_enabled);
  toggle.addEventListener('click', () => {
    toggle.disabled = true;
    void saveNotificationSettings({ ...settings, dnd_enabled: !settings.dnd_enabled })
      .finally(() => { toggle.disabled = false; });
  });
  container.append(label, toggle);
}

/** 渲染偏好设置页：自动学习开关 + DND + 偏好列表。 */
export function renderPreferences(
  container: HTMLElement,
  autoLearningEnabled: boolean,
  preferences: WorkPreference[],
  notificationSettings?: NotificationSettings,
): void {
  container.className = 'mw-work-preferences';
  container.innerHTML = '';

  // ── 自动学习总开关 ──
  const toggleSection = document.createElement('div');
  toggleSection.className = 'mw-work-preferences__toggle-section';
  const toggleLabel = document.createElement('span');
  toggleLabel.className = 'mw-work-preferences__toggle-label';
  toggleLabel.textContent = '自动学习工作偏好';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'mw-work-preferences__toggle';
  toggle.textContent = autoLearningEnabled ? '已开启' : '已关闭';
  toggle.dataset.enabled = String(autoLearningEnabled);
  toggle.addEventListener('click', () => {
    toggle.disabled = true;
    void setAutoLearning(!autoLearningEnabled)
      .then(() => renderPreferences(
        container,
        workStore.get().preferenceAutoLearning,
        workStore.get().preferences,
        notificationSettings,
      ))
      .catch((error) => {
        toggle.disabled = false;
        toggle.title = error instanceof Error ? error.message : String(error);
      });
  });
  toggleSection.append(toggleLabel, toggle);
  container.append(toggleSection);

  const create = document.createElement('div');
  const createButton = document.createElement('button');
  create.className = 'mw-work-preferences__create';
  createButton.type = 'button';
  createButton.className = 'mw-work-preferences__pref-action';
  createButton.dataset.workPreferenceCreate = '';
  createButton.textContent = '新增偏好';
  createButton.addEventListener('click', () => {
    const category = document.createElement('input');
    const content = document.createElement('input');
    const save = document.createElement('button');
    const cancel = document.createElement('button');
    category.className = 'mw-work-preferences__pref-input';
    category.placeholder = '类别，例如：文档';
    category.setAttribute('aria-label', '偏好类别');
    content.className = 'mw-work-preferences__pref-input';
    content.placeholder = '描述你的工作偏好';
    content.setAttribute('aria-label', '偏好内容');
    save.type = 'button';
    save.className = 'mw-work-preferences__pref-action';
    save.textContent = '保存';
    cancel.type = 'button';
    cancel.className = 'mw-work-preferences__pref-action';
    cancel.textContent = '取消';
    const rerender = (): void => renderPreferences(
      container,
      workStore.get().preferenceAutoLearning,
      workStore.get().preferences,
      notificationSettings,
    );
    save.addEventListener('click', () => {
      if (!category.value.trim() || !content.value.trim()) {
        content.setCustomValidity('请填写类别和偏好内容');
        content.reportValidity();
        return;
      }
      save.disabled = true;
      void createPreference(category.value, content.value).then(rerender).catch((error) => {
        save.disabled = false;
        content.setCustomValidity(error instanceof Error ? error.message : String(error));
        content.reportValidity();
      });
    });
    cancel.addEventListener('click', rerender);
    create.replaceChildren(category, content, save, cancel);
    category.focus();
  });
  create.append(createButton);
  container.append(create);

  // ── 免打扰设置 ──
  if (notificationSettings) {
    const dnd = document.createElement('div');
    renderDndSettings(dnd, notificationSettings);
    container.append(dnd);
  }

  // ── 偏好列表 ──
  const listSection = document.createElement('div');
  listSection.className = 'mw-work-preferences__list';
  const listHeader = document.createElement('h3');
  listHeader.className = 'mw-work-preferences__list-header';
  listHeader.textContent = `工作偏好（${preferences.length}）`;
  listSection.append(listHeader);

  if (preferences.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-preferences__empty';
    empty.textContent = '尚无工作偏好，可以手动新增或继续使用自动学习';
    listSection.append(empty);
  } else {
    for (const pref of preferences) {
      const row = document.createElement('div');
      row.className = 'mw-work-preferences__pref';
      row.dataset.status = pref.status;
      const label = document.createElement('span');
      label.className = 'mw-work-preferences__pref-label';
      label.textContent = `${pref.category}: ${pref.content}`;
      const actions = document.createElement('div');
      actions.className = 'mw-work-preferences__pref-actions';
      const rerender = (): void => renderPreferences(
        container,
        workStore.get().preferenceAutoLearning,
        workStore.get().preferences,
        notificationSettings,
      );
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'mw-work-preferences__pref-action';
      editBtn.textContent = '编辑';
      editBtn.addEventListener('click', () => {
        const input = document.createElement('input');
        const save = document.createElement('button');
        const cancel = document.createElement('button');
        input.className = 'mw-work-preferences__pref-input';
        input.value = pref.content;
        input.setAttribute('aria-label', `编辑 ${pref.category} 偏好`);
        save.type = 'button';
        save.className = 'mw-work-preferences__pref-action';
        save.textContent = '保存';
        cancel.type = 'button';
        cancel.className = 'mw-work-preferences__pref-action';
        cancel.textContent = '取消';
        save.addEventListener('click', () => {
          if (!input.value.trim()) {
            input.setCustomValidity('偏好内容不能为空');
            input.reportValidity();
            return;
          }
          save.disabled = true;
          void editPreference(pref, input.value).then(rerender).catch((error) => {
            save.disabled = false;
            input.setCustomValidity(error instanceof Error ? error.message : String(error));
            input.reportValidity();
          });
        });
        cancel.addEventListener('click', rerender);
        label.replaceWith(input);
        actions.replaceChildren(save, cancel);
        input.focus();
        input.select();
      });
      const pauseBtn = document.createElement('button');
      pauseBtn.type = 'button';
      pauseBtn.className = 'mw-work-preferences__pref-action';
      pauseBtn.textContent = pref.status === 'paused' ? '恢复' : '暂停';
      pauseBtn.addEventListener('click', () => {
        pauseBtn.disabled = true;
        void setPreferencePaused(pref, pref.status !== 'paused').then(rerender).catch((error) => {
          pauseBtn.disabled = false;
          pauseBtn.title = error instanceof Error ? error.message : String(error);
        });
      });
      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'mw-work-preferences__pref-action mw-work-preferences__pref-action--danger';
      deleteBtn.textContent = '删除';
      deleteBtn.addEventListener('click', () => {
        if (!window.confirm('确认删除这条工作偏好？')) return;
        const undo = document.createElement('button');
        undo.type = 'button';
        undo.className = 'mw-work-preferences__pref-action';
        undo.textContent = '撤销删除';
        row.dataset.pendingDelete = 'true';
        const timer = window.setTimeout(() => {
          void deletePreference(pref).then(rerender).catch((error) => {
            row.dataset.pendingDelete = 'false';
            undo.title = error instanceof Error ? error.message : String(error);
          });
        }, 5000);
        undo.addEventListener('click', () => {
          window.clearTimeout(timer);
          rerender();
        });
        actions.replaceChildren(undo);
      });
      actions.append(editBtn, pauseBtn, deleteBtn);
      row.append(label, actions);
      listSection.append(row);
    }
  }
  container.append(listSection);
}

export { loadNotificationSettings };
