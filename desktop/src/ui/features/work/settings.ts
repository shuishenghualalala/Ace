/** Work 设置组合页：复用来源、偏好、通知和现有权限 owner。 */

import { workApi } from '../../backend-client';
import {
  workStore,
  loadWorkPreferences,
  loadWorkSettings,
  loadWorkSources,
} from '../../stores/work-store';
import { renderPreferences } from './preferences';
import {
  loadNotificationSettings,
  notificationPermission,
  notificationSupported,
  saveNotificationSettings,
  type NotificationSettings,
} from './notifications';
import {
  loadSourceRecords,
  renderSourceRecords,
  renderSources,
  sourceDisplayName,
} from './sources';

const PERMISSION_LABELS: Record<NotificationPermission, string> = {
  granted: '已允许',
  denied: '已拒绝',
  default: '未授权',
};

const PANEL_DESCRIPTIONS: Record<string, string> = {
  sources: '连接邮件、日历等办公系统，统一查看启用状态、同步结果与本地数据。',
  preferences: '让办公助手记住稳定的表达习惯和工作方式，并随时暂停或修正。',
  automation: '控制事项处理成功后是否自动推进状态，不影响完成、归档或审批。',
  reminders: '管理桌面通知、免打扰时段以及各办公系统的提醒开关。',
};

function createPageHeader(): HTMLElement {
  const header = document.createElement('header');
  const title = document.createElement('h1');
  const description = document.createElement('p');
  header.className = 'mw-work-settings__page-header';
  title.className = 'set-v2-pane__title';
  title.textContent = '办公助手';
  description.className = 'set-v2-pane__desc';
  description.textContent = '管理办公数据、工作偏好、事项自动化与通知。';
  header.append(title, description);
  return header;
}
function section(title: string, key: string): { element: HTMLElement; body: HTMLElement } {
  const element = document.createElement('section');
  const header = document.createElement('header');
  const heading = document.createElement('h2');
  const description = document.createElement('p');
  const body = document.createElement('div');
  element.className = 'mw-work-settings__section';
  element.dataset.workSettingsPanel = key;
  element.id = `work-settings-panel-${key}`;
  element.setAttribute('role', 'tabpanel');
  header.className = 'mw-work-settings__panel-header';
  header.dataset.workSettingsPanelHeader = '';
  heading.className = 'mw-work-settings__heading';
  heading.textContent = title;
  description.className = 'mw-work-settings__description';
  description.textContent = PANEL_DESCRIPTIONS[key] ?? '';
  body.className = 'mw-work-settings__body';
  header.append(heading, description);
  element.append(header, body);
  return { element, body };
}

function renderNotifications(
  container: HTMLElement,
  settings: NotificationSettings,
  sourceKeys: string[],
  rerender: () => void,
): void {
  const permission = document.createElement('div');
  const permissionCopy = document.createElement('div');
  const permissionLabel = document.createElement('strong');
  const permissionState = document.createElement('span');
  const permissionButton = document.createElement('button');
  const dnd = document.createElement('label');
  const dndCopy = document.createElement('span');
  const dndLabel = document.createElement('strong');
  const dndDescription = document.createElement('span');
  const dndSwitch = document.createElement('span');
  const dndToggle = document.createElement('input');
  const dndTrack = document.createElement('span');
  const timeRange = document.createElement('div');
  const startField = document.createElement('label');
  const endField = document.createElement('label');
  const start = document.createElement('input');
  const end = document.createElement('input');
  const save = document.createElement('button');
  const feedback = document.createElement('p');
  const sourceToggles = new Map<string, HTMLInputElement>();
  permission.className = 'mw-work-settings__row';
  permissionCopy.className = 'mw-work-settings__setting-copy';
  permissionLabel.textContent = '桌面通知';
  permissionState.textContent = notificationSupported()
    ? PERMISSION_LABELS[notificationPermission()]
    : '当前平台不支持';
  permissionCopy.append(permissionLabel, permissionState);
  permissionButton.type = 'button';
  permissionButton.className = 'mw-work-settings__action';
  permissionButton.textContent = '请求权限';
  permissionButton.disabled = !notificationSupported() || notificationPermission() === 'granted';
  permissionButton.addEventListener('click', () => {
    permissionButton.disabled = true;
    void Notification.requestPermission().finally(rerender);
  });
  permission.append(permissionCopy, permissionButton);

  dnd.className = 'mw-work-settings__row';
  dndCopy.className = 'mw-work-settings__setting-copy';
  dndLabel.textContent = '免打扰';
  dndDescription.textContent = '在设定时段暂停非紧急桌面提醒。';
  dndCopy.append(dndLabel, dndDescription);
  dndSwitch.className = 'set-v2-switch';
  dndToggle.type = 'checkbox';
  dndToggle.checked = settings.dnd_enabled;
  dndToggle.setAttribute('aria-label', '启用免打扰');
  dndTrack.className = 'set-v2-switch__track';
  dndSwitch.append(dndToggle, dndTrack);
  dnd.append(dndCopy, dndSwitch);
  timeRange.className = 'mw-work-settings__time-range';
  startField.className = 'mw-work-settings__time-field';
  endField.className = 'mw-work-settings__time-field';
  start.type = 'time';
  start.value = settings.dnd_start ?? '22:00';
  start.setAttribute('aria-label', '免打扰开始时间');
  end.type = 'time';
  end.value = settings.dnd_end ?? '07:00';
  end.setAttribute('aria-label', '免打扰结束时间');
  startField.append(document.createTextNode('开始'), start);
  endField.append(document.createTextNode('结束'), end);
  timeRange.append(startField, endField);
  save.type = 'button';
  save.className = 'mw-work-settings__action mw-work-settings__save';
  save.textContent = '保存通知设置';
  feedback.className = 'mw-work-settings__feedback';
  feedback.setAttribute('aria-live', 'polite');
  save.addEventListener('click', () => {
    save.disabled = true;
    feedback.textContent = '正在保存…';
    void saveNotificationSettings({
      ...settings,
      dnd_enabled: dndToggle.checked,
      dnd_start: start.value || null,
      dnd_end: end.value || null,
      source_notifications: Object.fromEntries(
        [...sourceToggles].map(([key, toggle]) => [key, toggle.checked]),
      ),
    })
      .then(rerender)
      .catch((error) => {
        save.disabled = false;
        feedback.dataset.state = 'error';
        feedback.textContent = `保存失败：${error instanceof Error ? error.message : String(error)}`;
      });
  });
  container.append(permission, dnd, timeRange);
  if (sourceKeys.length > 0) {
    const sourceHeading = document.createElement('h3');
    sourceHeading.className = 'mw-work-settings__subheading';
    sourceHeading.textContent = '来源通知';
    container.append(sourceHeading);
  }
  for (const sourceKey of sourceKeys) {
    const source = document.createElement('label');
    const toggle = document.createElement('input');
    const track = document.createElement('span');
    const switchControl = document.createElement('span');
    source.className = 'mw-work-settings__row mw-work-settings__source-notification';
    switchControl.className = 'set-v2-switch';
    toggle.type = 'checkbox';
    toggle.checked = settings.source_notifications[sourceKey] !== false;
    const sourceName = sourceDisplayName(sourceKey);
    toggle.setAttribute('aria-label', `${sourceName}通知`);
    track.className = 'set-v2-switch__track';
    switchControl.append(toggle, track);
    sourceToggles.set(sourceKey, toggle);
    source.append(document.createTextNode(`${sourceName}通知`), switchControl);
    container.append(source);
  }
  container.append(save, feedback);
}

function renderAutomation(container: HTMLElement, rerender: () => void): void {
  const row = document.createElement('label');
  const copy = document.createElement('span');
  const title = document.createElement('strong');
  const description = document.createElement('span');
  const switchControl = document.createElement('span');
  const toggle = document.createElement('input');
  const track = document.createElement('span');
  const feedback = document.createElement('p');
  row.className = 'mw-work-settings__row';
  copy.className = 'mw-work-settings__setting-copy';
  title.textContent = '自动推进事项';
  description.textContent = '首次成功处理后，将事项从“待处理”切换为“进行中”。';
  copy.append(title, description);
  switchControl.className = 'set-v2-switch';
  toggle.type = 'checkbox';
  toggle.checked = workStore.get().settings.auto_status_transition === true;
  toggle.setAttribute('aria-label', '自动切换事项状态');
  track.className = 'set-v2-switch__track';
  switchControl.append(toggle, track);
  row.append(copy, switchControl);
  feedback.className = 'mw-work-settings__feedback';
  feedback.setAttribute('aria-live', 'polite');
  toggle.addEventListener('change', () => {
    toggle.disabled = true;
    feedback.textContent = '正在保存…';
    void workApi.putSettings({ auto_status_transition: toggle.checked })
      .then(async () => {
        await loadWorkSettings();
        rerender();
      })
      .catch((error) => {
        toggle.disabled = false;
        feedback.dataset.state = 'error';
        feedback.textContent = `保存失败：${error instanceof Error ? error.message : String(error)}`;
      });
  });
  container.append(row, feedback);
}

/** 装载 Work 设置的真实来源、偏好、通知和权限状态。 */
export async function renderWorkSettings(container: HTMLElement): Promise<void> {
  const activePanel =
    container.querySelector<HTMLButtonElement>('[data-work-settings-tab][aria-selected="true"]')
      ?.dataset.workSettingsTab ?? 'sources';
  container.classList.add('mw-work-settings');
  const pageHeader = createPageHeader();
  const status = document.createElement('p');
  status.className = 'mw-work-settings__feedback';
  status.textContent = '正在加载办公助手设置…';
  status.setAttribute('aria-live', 'polite');
  container.replaceChildren(pageHeader, status);
  try {
    const [, , , notifications, sourceRecords] = await Promise.all([
      loadWorkSources(),
      loadWorkPreferences(),
      loadWorkSettings(),
      loadNotificationSettings(),
      loadSourceRecords(),
    ]);
    container.replaceChildren(pageHeader);
    const sources = section('数据来源', 'sources');
    const preferences = section('工作偏好', 'preferences');
    const automation = section('计划自动化', 'automation');
    const notification = section('办公提醒', 'reminders');
    const sourceList = document.createElement('div');
    const preferenceList = document.createElement('div');
    renderSources(sourceList, workStore.get().sources, () => {
      void renderWorkSettings(container);
    });
    sources.body.append(sourceList);
    if (sourceRecords.length > 0) {
      const records = document.createElement('div');
      renderSourceRecords(records, sourceRecords);
      sources.body.append(records);
    }
    const permissionNote = document.createElement('div');
    const permissionTitle = document.createElement('strong');
    const permissionDescription = document.createElement('span');
    permissionNote.className = 'mw-work-settings__permission-note';
    permissionTitle.textContent = '文件访问';
    permissionDescription.textContent =
      '沿用 Workspace 目录授权；目录外访问继续进入安全审批。';
    permissionNote.append(permissionTitle, permissionDescription);
    sources.body.append(permissionNote);
    renderPreferences(
      preferenceList,
      workStore.get().preferenceAutoLearning,
      workStore.get().preferences,
    );
    preferences.body.append(preferenceList);
    renderAutomation(automation.body, () => {
      void renderWorkSettings(container);
    });
    renderNotifications(notification.body, notifications, workStore.get().sources.map((source) => source.connector_key), () => {
      void renderWorkSettings(container);
    });
    const tabs = document.createElement('div');
    tabs.className = 'mw-work-settings__tabs';
    tabs.setAttribute('role', 'tablist');
    const panels = [sources, preferences, automation, notification];
    const activatePanel = (key: string): void => {
      panels.forEach(({ element }) => {
        const active = element.dataset.workSettingsPanel === key;
        element.hidden = !active;
      });
      tabs.querySelectorAll<HTMLButtonElement>('[data-work-settings-tab]').forEach((button) => {
        const active = button.dataset.workSettingsTab === key;
        button.setAttribute('aria-selected', String(active));
        button.tabIndex = active ? 0 : -1;
      });
    };
    const handleTabKeydown = (event: KeyboardEvent): void => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const buttons = [...tabs.querySelectorAll<HTMLButtonElement>('[data-work-settings-tab]')];
      const currentIndex = buttons.indexOf(event.currentTarget as HTMLButtonElement);
      const nextIndex = event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? buttons.length - 1
          : (currentIndex + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length)
            % buttons.length;
      const next = buttons[nextIndex];
      if (!next?.dataset.workSettingsTab) return;
      event.preventDefault();
      next.focus();
      activatePanel(next.dataset.workSettingsTab);
    };
    panels.forEach((panel) => {
      const key = panel.element.dataset.workSettingsPanel!;
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.id = `work-settings-tab-${key}`;
      tab.dataset.workSettingsTab = key;
      tab.textContent = panel.element.querySelector('.mw-work-settings__heading')?.textContent ?? '';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', panel.element.id);
      panel.element.setAttribute('aria-labelledby', tab.id);
      tab.addEventListener('click', () => {
        activatePanel(key);
      });
      tab.addEventListener('keydown', handleTabKeydown);
      tabs.append(tab);
    });
    activatePanel(panels.some(({ element }) => element.dataset.workSettingsPanel === activePanel)
      ? activePanel
      : 'sources');
    container.append(tabs, ...panels.map((panel) => panel.element));
  } catch (error) {
    status.dataset.state = 'error';
    status.textContent = `加载失败：${error instanceof Error ? error.message : String(error)}`;
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = '重试';
    retry.addEventListener('click', () => void renderWorkSettings(container));
    container.append(retry);
  }
}
