/**
 * 场景化推荐：用户点 welcome 细分玩法后 arm sub_scenario，随下一条 WS 消息发送。
 */

import { createIcon } from '../components/icon';
import { $ } from '../state';

let armedSubScenario = '';

/** 记录待发送的 sub_scenario id，并显示 composer chip。 */
export function armSubScenario(label: string, subId: string): void {
  armedSubScenario = subId;
  const chip = $('#chat-scenario-chip') as HTMLElement | null;
  if (!chip) return;
  const selected = document.createElement('span');
  const clear = document.createElement('button');
  selected.className = 'scenario-chip__label';
  selected.append(createIcon('icon-file', { size: 16 }), document.createTextNode(label));
  clear.type = 'button';
  clear.className = 'scenario-chip__clear';
  clear.title = '清除场景';
  clear.ariaLabel = '清除场景';
  clear.append(createIcon('icon-close', { size: 16 }));
  chip.setAttribute('role', 'status');
  chip.setAttribute('aria-label', `已选择场景：${label}`);
  chip.replaceChildren(selected, clear);
  chip.hidden = false;
}

/** 取出并清空 arm 的 sub_scenario（send 时调用）。 */
export function takeArmedSubScenario(): string {
  const id = armedSubScenario;
  armedSubScenario = '';
  return id;
}

/** 用户点 chip ✕ 时清除场景绑定。 */
export function clearScenarioChip(): void {
  armedSubScenario = '';
  const chip = $('#chat-scenario-chip') as HTMLElement | null;
  if (!chip) return;
  chip.hidden = true;
  chip.removeAttribute('aria-label');
  chip.replaceChildren();
}
