import type { DefaultConversationPermission, SecurityPermissionPreferences } from '../../shared/types';
import { notify } from '../state';
import { defaultConversationSecurityMode } from './security-mode';

export const FULL_ACCESS_CHOICE_CHANGED = 'security:full-access-choice-changed';
export const DEFAULT_NEW_CONVERSATION_MODE_CHANGED = 'security:default-new-conversation-mode-changed';

let choiceEnabled: boolean | undefined;
let newConversationMode: DefaultConversationPermission | undefined;
let readingChoice: Promise<void> | undefined;
const mountedPanels = new WeakMap<HTMLElement, () => void>();

export function fullAccessChoiceEnabled(): boolean {
  return choiceEnabled ?? (typeof window === 'undefined' || !window.Crew?.securityGetFullAccessChoice);
}

export function preferredNewConversationMode(): DefaultConversationPermission | undefined {
  return newConversationMode === 'full_access' && !fullAccessChoiceEnabled() ? undefined : newConversationMode;
}

function updatePreferences({ enabled, defaultMode }: SecurityPermissionPreferences): void {
  updateDefaultMode(enabled === false && defaultMode === 'full_access' ? 'auto_review' : defaultMode);
  updateChoice(enabled === true);
}

function updateDefaultMode(mode: DefaultConversationPermission | undefined): void {
  if (newConversationMode === mode) return;
  newConversationMode = mode;
  window.dispatchEvent(new Event(DEFAULT_NEW_CONVERSATION_MODE_CHANGED));
}

function updateChoice(enabled: boolean): void {
  if (choiceEnabled === enabled) return;
  choiceEnabled = enabled;
  window.dispatchEvent(new Event(FULL_ACCESS_CHOICE_CHANGED));
}

export function loadFullAccessChoice(): Promise<void> {
  if (choiceEnabled !== undefined || !window.Crew?.securityGetFullAccessChoice) return Promise.resolve();
  readingChoice ??= window.Crew.securityGetFullAccessChoice()
    .then(updatePreferences)
    .finally(() => { readingChoice = undefined; });
  return readingChoice;
}

export function renderSecurityPermissionSettings(root: HTMLElement): void {
  const syncMountedPanel = mountedPanels.get(root);
  if (syncMountedPanel) {
    syncMountedPanel();
    return;
  }

  root.innerHTML = `
    <h3 class="set-v2-section__title set-v2-permissions__title">权限</h3>
    <div class="set-v2-section set-v2-permissions">
      <div class="set-v2-row">
        <div class="set-v2-row__copy">
          <label class="set-v2-row__label" for="settings-default-permission">新对话默认权限</label>
          <div class="set-v2-row__desc" id="settings-default-permission-description"></div>
        </div>
        <div class="set-v2-row__control">
          <select class="set-v2-select" id="settings-default-permission" aria-describedby="settings-default-permission-description" disabled>
            <option value="auto_review">替我审批</option>
            <option value="request_approval">请求批准</option>
            <option value="full_access" id="settings-full-access-option">完全访问权限</option>
            <option value="sandbox_disabled" disabled hidden>沙箱未启用</option>
          </select>
        </div>
      </div>
      <div class="set-v2-row">
        <div class="set-v2-row__copy">
          <div class="set-v2-row__label" id="settings-full-access-label">启用「完全访问」选项</div>
          <div class="set-v2-row__desc" id="settings-full-access-description">开启后，可设为默认或按对话选择。完全访问使用当前系统用户权限，可读写工作区外的文件并联网。</div>
        </div>
        <div class="set-v2-row__control">
          <label class="set-v2-switch"><input type="checkbox" id="settings-full-access-choice" role="switch"
            aria-labelledby="settings-full-access-label" aria-describedby="settings-full-access-description" disabled>
            <span class="set-v2-switch__track"></span></label>
        </div>
      </div>
    </div>`;

  const selector = root.querySelector<HTMLSelectElement>('#settings-default-permission')!;
  const description = root.querySelector<HTMLElement>('#settings-default-permission-description')!;
  const toggle = root.querySelector<HTMLInputElement>('#settings-full-access-choice')!;
  const setChoice = window.Crew?.securitySetFullAccessChoice;
  const setDefault = window.Crew?.securitySetDefaultConversationPermission;
  let savingChoice = false;
  let savingDefault = false;
  const sync = () => {
    const fallback = defaultConversationSecurityMode();
    const mode = fallback === 'full_access' ? 'sandbox_disabled' : preferredNewConversationMode() ?? fallback;
    description.textContent = {
      request_approval: '新对话中需要扩权的操作由你确认。',
      auto_review: '新对话中，Ace 自动审批常规操作，超出权限时会询问你。',
      full_access: '新对话使用当前系统用户权限运行命令。',
      sandbox_disabled: '沙箱已关闭，对话命令使用当前系统用户权限。',
    }[mode];
    if (!savingDefault) selector.value = mode;
    const fullAccessOption = selector.options.namedItem('settings-full-access-option');
    if (fullAccessOption) fullAccessOption.disabled = fullAccessOption.hidden = !fullAccessChoiceEnabled();
    selector.disabled = choiceEnabled === undefined || !setDefault || savingDefault || savingChoice || mode === 'sandbox_disabled';
    if (!savingChoice) toggle.checked = fullAccessChoiceEnabled();
    toggle.disabled = choiceEnabled === undefined || !setChoice || savingChoice || savingDefault;
  };
  const refresh = () => {
    sync();
    void loadFullAccessChoice().then(sync).catch(() => notify('无法读取权限设置。'));
  };
  mountedPanels.set(root, refresh);
  refresh();
  selector.addEventListener('change', async () => {
    const mode = selector.value;
    if (!setDefault || (mode !== 'request_approval' && mode !== 'auto_review' && mode !== 'full_access')) return;
    savingDefault = true;
    sync();
    try {
      updatePreferences(await setDefault(mode));
    } catch {
      notify('无法保存新对话默认权限。');
    } finally {
      savingDefault = false;
      sync();
    }
  });
  toggle.addEventListener('change', async () => {
    if (!setChoice) return;
    savingChoice = true;
    sync();
    try {
      updatePreferences(await setChoice(toggle.checked));
    } catch {
      notify('无法保存完全访问权限设置。');
    } finally {
      savingChoice = false;
      sync();
    }
  });
}
