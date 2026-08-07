import { createIcon, type IconId } from './icon';

export type ToolIconKind =
  | 'write' | 'read' | 'search' | 'web' | 'todo' | 'team' | 'memory' | 'skill' | 'cron' | 'terminal';

export interface ProcessTool {
  toolCallId: string;
  name: string;
  args?: string | undefined;
  result?: string | undefined;
  status: 'generating' | 'running' | 'done' | 'error';
  startedAt: number;
}

export interface ToolProcessOptions {
  title: string;
  foldKey: string;
  open: boolean;
  duration: string;
  resultText: string;
  media?: HTMLElement | undefined;
}

const TOOL_ICONS: Record<ToolIconKind, IconId> = {
  write: 'process-write',
  read: 'process-read',
  search: 'icon-search',
  web: 'process-web',
  todo: 'process-todo',
  team: 'icon-team',
  memory: 'process-memory',
  skill: 'process-skill',
  cron: 'process-clock',
  terminal: 'process-terminal',
};

export function toolIconKind(name: string): ToolIconKind {
  const lower = String(name || '').trim().toLowerCase();
  if (['write', 'file_write', 'edit', 'patch', 'apply_patch'].includes(lower)) return 'write';
  if (['read', 'file_read'].includes(lower)) return 'read';
  if (['grep', 'search_files', 'glob', 'tool_search'].includes(lower)) return 'search';
  if (lower.startsWith('web_') || lower.startsWith('browser') || lower === 'vision_analyze') return 'web';
  if (lower === 'todo') return 'todo';
  if (lower === 'memory') return 'memory';
  if (lower.startsWith('team_') || lower.startsWith('delegate') || lower.endsWith('_agent')
    || lower === 'run_agent' || lower === 'collect_subagent') return 'team';
  if (lower.startsWith('skills_') || lower === 'skill_view') return 'skill';
  if (lower.startsWith('cron_')) return 'cron';
  return 'terminal';
}

export function createProcessTimeline(parts: Node[]): HTMLElement {
  const timeline = document.createElement('div');
  timeline.className = 'process-timeline mw-process-timeline';
  timeline.setAttribute('aria-label', '执行过程');
  timeline.append(...parts);
  return timeline;
}

export function createProcessItem(
  iconId: IconId | null,
  content: HTMLElement,
  state: 'idle' | 'running' | 'error' = 'idle',
): HTMLElement {
  const item = document.createElement('div');
  item.className = 'process-timeline__item mw-process-timeline__item';
  const icon = document.createElement('div');
  icon.className = 'process-timeline__icon mw-process-timeline__icon';
  if (state !== 'idle') icon.classList.add(`process-timeline__icon--${state}`);
  if (iconId) icon.appendChild(createIcon(iconId, { size: 20 }));
  else icon.classList.add('process-timeline__icon--ghost');
  item.append(icon, content);
  return item;
}

export function createThinkingProcess(
  thinking: string,
  messageId: string,
  streaming: boolean,
): HTMLElement {
  const details = document.createElement('details');
  details.className = 'process-timeline__content process-timeline__details';
  details.open = streaming;
  const summary = document.createElement('summary');
  summary.className = 'process-timeline__row';
  const title = document.createElement('span');
  title.className = 'process-timeline__title';
  title.textContent = streaming ? '思考中' : '思考已完成';
  const chevron = document.createElement('span');
  chevron.className = 'process-timeline__chevron';
  chevron.textContent = '›';
  chevron.setAttribute('aria-hidden', 'true');
  const content = document.createElement('div');
  content.className = 'process-timeline__thinking';
  content.textContent = thinking;
  summary.append(title, chevron);
  details.append(summary, content);

  const item = createProcessItem(
    'process-thinking',
    details,
    streaming ? 'running' : 'idle',
  );
  item.dataset.thinkingFor = messageId;
  return item;
}

function prettyBlock(value?: string): string {
  if (!value) return '';
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function createDuration(tool: ProcessTool, value: string): HTMLElement {
  const duration = document.createElement('span');
  duration.className = 'process-timeline__duration';
  duration.textContent = value;
  if (tool.status === 'running' || tool.status === 'generating') {
    duration.dataset.active = 'true';
    duration.dataset.startedAt = String(tool.startedAt);
  }
  return duration;
}

function createToolDetails(tool: ProcessTool, options: ToolProcessOptions): HTMLElement {
  const details = document.createElement('details');
  details.className = 'process-timeline__content process-timeline__details';
  details.open = options.open;
  details.dataset.foldKey = options.foldKey;
  const summary = document.createElement('summary');
  summary.className = 'process-timeline__row';
  const title = document.createElement('span');
    title.className = 'process-timeline__title';
    title.textContent = options.title;
    const duration = createDuration(tool, options.duration);
    const showDuration =
      Boolean(options.duration) || tool.status === 'running' || tool.status === 'generating';
  const chevron = document.createElement('span');
  chevron.className = 'process-timeline__chevron';
  chevron.textContent = '›';
    chevron.setAttribute('aria-hidden', 'true');
    summary.append(title);
    if (showDuration) summary.append(duration);
  summary.append(chevron);

  const detail = document.createElement('div');
  detail.className = 'process-timeline__detail';
  if (tool.args) {
    const request = document.createElement('section');
    request.className = 'process-code-block';
    request.dataset.section = 'args';
    const heading = document.createElement('div');
    heading.className = 'process-code-block__title';
    heading.textContent = 'Request';
    const pre = document.createElement('pre');
    pre.textContent = prettyBlock(tool.args);
    request.append(heading, pre);
    detail.appendChild(request);
  }
  if (tool.result) {
    const response = document.createElement('section');
    response.className = 'process-code-block';
    response.dataset.section = 'result';
    const heading = document.createElement('div');
    heading.className = 'process-code-block__title';
    heading.textContent = 'Response';
    const pre = document.createElement('pre');
    pre.textContent = options.resultText;
    response.append(heading, pre);
    detail.appendChild(response);
  }
  details.append(summary, detail);
  return details;
}

export function createToolProcess(
  tool: ProcessTool,
  options: ToolProcessOptions,
): HTMLElement {
  const active = tool.status === 'running' || tool.status === 'generating';
  const state = tool.status === 'error' ? 'error' : active ? 'running' : 'idle';
  const hasDetail = Boolean(tool.args || tool.result);
  let content: HTMLElement;
  if (hasDetail) {
    const details = createToolDetails(tool, options);
    if (options.media) {
      const media = document.createElement('div');
      media.className = 'process-timeline__tool-media';
      media.append(details, options.media);
      content = media;
    } else {
      content = details;
    }
  } else {
    content = document.createElement('div');
    content.className = 'process-timeline__content';
    const row = document.createElement('div');
    row.className = 'process-timeline__row process-timeline__row--static';
    const title = document.createElement('span');
    title.className = 'process-timeline__title';
    title.textContent = options.title;
    row.appendChild(title);
    if (options.duration || active) row.appendChild(createDuration(tool, options.duration));
    content.appendChild(row);
  }
  return createProcessItem(TOOL_ICONS[toolIconKind(tool.name)], content, state);
}
