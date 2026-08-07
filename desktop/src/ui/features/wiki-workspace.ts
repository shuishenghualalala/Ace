export interface WikiWorkspaceView {
  element: HTMLElement;
  slots: {
    header: HTMLElement;
    notice: HTMLElement;
    navigation: HTMLElement;
    sash: HTMLElement;
    detail: HTMLElement;
    agentSash: HTMLElement;
    agent: HTMLElement;
  };
  setGraphMode(graphMode: boolean): void;
}

function region(tag: string, className: string, label?: string): HTMLElement {
  const element = document.createElement(tag);
  element.className = className;
  if (label) element.setAttribute('aria-label', label);
  return element;
}

/** Creates the stable three-region Wiki composition used across all Wiki states. */
export function createWikiWorkspaceView(): WikiWorkspaceView {
  const element = region('section', 'mw-wiki-workspace');
  const header = region('header', 'mw-wiki-workspace__header');
  const notice = region('div', 'mw-wiki-workspace__notice');
  const body = region('div', 'mw-wiki-workspace__body');
  const navigation = region('aside', 'mw-wiki-workspace__navigation', 'Wiki 来源导航');
  const sash = region('div', 'mw-wiki-workspace__sash');
  const detail = region('main', 'mw-wiki-workspace__detail', 'Wiki 页面详情');
  const agentSash = region('div', 'mw-wiki-workspace__sash mw-wiki-workspace__agent-sash');
  const agent = region('aside', 'mw-wiki-workspace__agent', 'Wiki Agent 对话');

  element.dataset.template = 'wiki-workspace';
  element.dataset.graphMode = 'false';
  sash.dataset.wikiSash = '';
  sash.setAttribute('role', 'separator');
  sash.setAttribute('aria-orientation', 'vertical');
  sash.title = '拖拽调整列表宽度，双击复位';
  agentSash.dataset.wikiAgentSash = '';
  agentSash.setAttribute('role', 'separator');
  agentSash.setAttribute('aria-orientation', 'vertical');
  agentSash.title = '拖拽调整对话栏宽度，双击复位';
  agent.dataset.wikiAgentPanel = '';
  body.append(navigation, sash, detail, agentSash, agent);
  element.append(header, notice, body);

  return {
    element,
    slots: { header, notice, navigation, sash, detail, agentSash, agent },
    setGraphMode(graphMode) {
      element.dataset.graphMode = String(graphMode);
      navigation.setAttribute('aria-label', graphMode ? 'Wiki 图谱' : 'Wiki 来源导航');
      if (graphMode) sash.remove();
      else if (!body.contains(sash)) body.insertBefore(sash, detail);
    },
  };
}
