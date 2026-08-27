import { createIcon, MONOCHROME_ICON_CLASS, type IconId } from '../components/icon';

export interface ComposerContextView {
  dispose(): void;
}

interface ContextTargets {
  project: HTMLElement;
  beforeInput: HTMLElement;
  toolbarLeft: HTMLElement;
  toolbarRight: HTMLElement;
}

function requireTarget(root: HTMLElement, slot: string): HTMLElement {
  const target = root.querySelector<HTMLElement>(`[data-composer-context-target="${slot}"]`);
  if (!target) throw new Error(`Composer context target is missing: ${slot}`);
  return target;
}

function createChip(
  id: string,
  label: string,
  icon: IconId | null,
  labelId?: string,
): HTMLButtonElement {
  const button = document.createElement('button');
  const copy = document.createElement('span');
  button.type = 'button';
  button.id = id;
  button.className = 'mw-context-chip';
  button.title = label;
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  copy.className = 'mw-context-chip__label';
  copy.textContent = label;
  if (labelId) copy.id = labelId;
  if (icon) button.append(createIcon(icon, {
    className: icon === 'icon-agent' || icon === 'icon-crew-agent' || icon === 'icon-external-agent'
      ? `mw-context-chip__icon ${MONOCHROME_ICON_CLASS}`
      : 'mw-context-chip__icon',
    size: 16,
  }));
  button.append(copy, createIcon(
    'icon-chevron-down',
    { className: 'mw-context-chip__chevron', size: 16 },
  ));
  return button;
}

function createWorkflowRecommendation(): HTMLElement {
  const recommendation = document.createElement('div');
  const text = document.createElement('span');
  const start = document.createElement('button');
  const close = document.createElement('button');
  recommendation.id = 'chat-workflow-recommendation';
  recommendation.className = 'chat-workflow-recommendation';
  recommendation.hidden = true;
  text.id = 'chat-workflow-recommendation-text';
  text.className = 'chat-workflow-recommendation__text';
  text.textContent = '该任务适合走 Dynamic Workflow';
  start.type = 'button';
  start.id = 'chat-workflow-recommendation-btn';
  start.className = 'chat-workflow-recommendation__btn';
  start.textContent = '启动工作流';
  close.type = 'button';
  close.id = 'chat-workflow-recommendation-close';
  close.className = 'chat-workflow-recommendation__close';
  close.setAttribute('aria-label', '关闭');
  close.append(createIcon('icon-close', { size: 16 }));
  recommendation.append(
    createIcon('process-todo', { className: 'chat-workflow-recommendation__icon', size: 16 }),
    text,
    start,
    close,
  );
  return recommendation;
}

function createBeforeInput(): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const input = document.createElement('input');
  const preview = document.createElement('div');
  const siteAnnotationPreview = document.createElement('div');
  const blueprintAnnotationPreview = document.createElement('div');
  const sitesMode = document.createElement('div');
  const scenario = document.createElement('div');
  input.type = 'file';
  input.id = 'chat-file-input';
  input.className = 'chat-file-input';
  input.multiple = true;
  input.hidden = true;
  preview.dataset.attachmentPreview = '';
  preview.className = 'mw-attachment-list';
  preview.hidden = true;
  siteAnnotationPreview.id = 'chat-site-annotation-preview';
  siteAnnotationPreview.className = 'chat-site-annotation-preview';
  siteAnnotationPreview.hidden = true;
  blueprintAnnotationPreview.id = 'chat-blueprint-annotation-preview';
  blueprintAnnotationPreview.className = 'chat-site-annotation-preview';
  blueprintAnnotationPreview.hidden = true;
  sitesMode.id = 'chat-sites-mode';
  sitesMode.className = 'chat-sites-mode';
  sitesMode.hidden = true;
  sitesMode.innerHTML = `<span class="chat-sites-mode__logo" data-sites-logo aria-hidden="true">
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2.5" y="3" width="15" height="14" rx="2.25" />
      <path d="M2.5 7h15M6 11h5M6 14h8" />
    </svg>
  </span><strong>灵感</strong><span>设计一个 App</span>`;
  scenario.id = 'chat-scenario-chip';
  scenario.className = 'scenario-chip';
  scenario.hidden = true;
  fragment.append(
    sitesMode,
    input,
    preview,
    siteAnnotationPreview,
    blueprintAnnotationPreview,
    scenario,
    createWorkflowRecommendation(),
  );
  return fragment;
}

function createProjectControl(): HTMLElement {
  const workspace = document.createElement('div');
  workspace.id = 'chat-workspace-row';
  workspace.className = 'mw-context-project';
  workspace.hidden = true;
  workspace.append(createChip(
    'chat-workspace-btn',
    '不在项目中工作',
    'icon-folder',
    'chat-workspace-btn-label',
  ));
  return workspace;
}

function createIconControl(
  id: string,
  label: string,
  icon: IconId,
  className: string,
): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.id = id;
  button.className = `mw-button mw-button--ghost mw-button--icon ${className}`;
  button.title = label;
  button.setAttribute('aria-label', label);
  button.append(createIcon(icon, { size: 18 }));
  return button;
}

function createLeftControls(): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const security = document.createElement('div');
  const workflow = document.createElement('div');
  const attach = createIconControl(
    'chat-attach-btn',
    '添加附件',
    'icon-attachment',
    'mw-context-attach',
  );
  const mode = document.createElement('div');
  const modeTrigger = createChip(
    'chat-craft-btn',
    '智能体',
    'icon-crew-agent',
    'chat-craft-btn-label',
  );
  const clearMode = createIconControl(
    'chat-craft-clear',
    '清除对话模式',
    'icon-close',
    'mw-context-selection__clear',
  );
  const skills = document.createElement('div');
  const workdir = createChip('chat-workflow-workdir-btn', '产物目录', 'icon-folder');

  mode.id = 'chat-craft-inline';
  mode.className = 'mw-context-selection';
  modeTrigger.setAttribute('aria-haspopup', 'listbox');
  clearMode.hidden = true;
  mode.append(modeTrigger, clearMode);
  skills.id = 'chat-skills-inline';
  skills.append(createChip(
    'chat-skills-btn',
    '技能',
    'skill-badge',
    'chat-skills-btn-label',
  ));
  security.id = 'chat-security-mode-inline';
  security.append(createChip(
    'chat-security-mode-btn',
    '请求批准',
    'icon-security',
    'chat-security-mode-btn-label',
  ));
  workflow.id = 'chat-workflow-toggle-inline';
  workflow.hidden = true;
  workdir.disabled = true;
  workdir.removeAttribute('aria-haspopup');
  workdir.removeAttribute('aria-expanded');
  workflow.append(workdir);
  fragment.append(attach, mode, skills, security, workflow);
  return fragment;
}

function createContextRing(): HTMLButtonElement {
  const button = document.createElement('button');
  const percentage = document.createElement('span');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const track = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  const progress = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  button.type = 'button';
  button.id = 'chat-context-ring-btn';
  button.className = 'mw-button mw-button--ghost mw-context-ring';
  button.hidden = true;
  button.title = '上下文占用';
  button.setAttribute('aria-label', '上下文占用');
  percentage.id = 'chat-context-ring-pct';
  percentage.className = 'mw-context-ring__percentage';
  percentage.setAttribute('aria-hidden', 'true');
  percentage.textContent = '0%';
  svg.setAttribute('class', 'mw-context-ring__graphic');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  for (const circle of [track, progress]) {
    circle.setAttribute('cx', '12');
    circle.setAttribute('cy', '12');
    circle.setAttribute('r', '9');
    circle.setAttribute('fill', 'none');
    circle.setAttribute('stroke-width', '2.5');
  }
  track.classList.add('mw-context-ring__track');
  progress.classList.add('mw-context-ring__progress');
  progress.setAttribute('stroke-linecap', 'round');
  progress.setAttribute('transform', 'rotate(-90 12 12)');
  svg.append(track, progress);
  button.append(svg, percentage);
  return button;
}

function createRightControls(): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const model = document.createElement('div');
  model.id = 'chat-model-picker-inline';
  model.append(createChip(
    'chat-model-picker-inline-btn',
    '模型',
    null,
    'chat-model-picker-inline-label',
  ));
  fragment.append(model, createContextRing());
  return fragment;
}

/**
 * Builds the main Composer context surface. Feature binders keep the existing
 * Gateway and state contracts while static HTML no longer owns these controls.
 */
export function createComposerContextView(root: HTMLElement): ComposerContextView {
  const targets: ContextTargets = {
    project: requireTarget(root, 'project'),
    beforeInput: requireTarget(root, 'before-input'),
    toolbarLeft: requireTarget(root, 'toolbar-left'),
    toolbarRight: requireTarget(root, 'toolbar-right'),
  };
  const controls = root.querySelector<HTMLElement>('#composer-controls');
  targets.project.append(createProjectControl());
  targets.beforeInput.append(createBeforeInput());
  targets.toolbarLeft.append(createLeftControls());
  targets.toolbarRight.prepend(createRightControls());
  if (controls) targets.toolbarRight.append(controls);

  return {
    dispose() {
      targets.project.replaceChildren();
      targets.beforeInput.replaceChildren();
      targets.toolbarLeft.replaceChildren();
      for (const child of [...targets.toolbarRight.children]) {
        if (child !== controls) child.remove();
      }
    },
  };
}
