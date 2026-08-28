const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';

export const MONOCHROME_ICON_CLASS = 'mw-icon--monochrome';

export const ICON_IDS = [
  'avatar-base',
  'avatar-cap',
  'avatar-sunglasses',
  'avatar-headphones',
  'avatar-wizard',
  'avatar-detective',
  'avatar-bow',
  'avatar-sprout',
  'team-general',
  'team-research',
  'team-development',
  'team-design',
  'team-review',
  'team-operation',
  'team-leader',
  'team-collaboration',
  'skill-badge',
  'plugin-badge',
  'icon-plus',
  'icon-panel-collapse',
  'icon-chat-new',
  'icon-send',
  'icon-stop',
  'icon-refresh',
  'icon-expand',
  'icon-search',
  'icon-filter',
  'icon-settings',
  'icon-help',
  'icon-bell',
  'icon-more',
  'icon-attachment',
  'icon-file',
  'icon-folder',
  'icon-code',
  'icon-task',
  'icon-check',
  'icon-warning',
  'icon-security',
  'icon-error',
  'icon-chevron-down',
  'icon-chevron-up',
  'icon-back',
  'icon-close',
  'icon-agent',
  'icon-crew-agent',
  'icon-external-agent',
  'icon-team',
  'icon-expert-picker',
  'icon-wiki',
  'icon-image',
  'icon-inspiration',
  'process-thinking',
  'process-write',
  'process-read',
  'process-web',
  'process-todo',
  'process-memory',
  'process-skill',
  'process-clock',
  'process-terminal',
  'process-error',
  'status-running',
  'status-waiting',
  'status-complete',
  'loading-dots',
  'loading-frame',
  'loading-stream',
] as const;

export type IconId = (typeof ICON_IDS)[number];

const iconIds = new Set<string>(ICON_IDS);

export interface IconOptions {
  className?: string;
  label?: string;
  size?: 16 | 18 | 20 | 24 | 32 | 40;
}

/**
 * Creates an SVG that references the canonical product sprite.
 *
 * Unknown ids render the error symbol and retain the missing id for diagnostics.
 * Icons are decorative unless the caller supplies a visible-equivalent label.
 */
export function createIcon(id: IconId | string, options: IconOptions = {}): SVGSVGElement {
  const resolvedId: IconId = iconIds.has(id) ? (id as IconId) : 'icon-error';
  const svg = document.createElementNS(SVG_NAMESPACE, 'svg');
  const isEntity = /^(?:avatar-|team-|skill-badge|plugin-badge)/.test(resolvedId);

  svg.setAttribute('class', options.className ? `mw-icon ${options.className}` : 'mw-icon');
  svg.setAttribute('viewBox', isEntity ? '0 0 32 32' : '0 0 24 24');
  svg.setAttribute('focusable', 'false');
  if (options.size) {
    svg.setAttribute('width', String(options.size));
    svg.setAttribute('height', String(options.size));
  }
  if (options.label) {
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', options.label);
  } else {
    svg.setAttribute('aria-hidden', 'true');
  }
  if (resolvedId !== id) svg.dataset.iconMissing = id;

  if (resolvedId === 'icon-external-agent' || resolvedId === 'icon-crew-agent') {
    const image = document.createElementNS(SVG_NAMESPACE, 'image');
    const isCrewAgent = resolvedId === 'icon-crew-agent';
    image.setAttribute('href', isCrewAgent ? './menubar/default.png' : './external-agent.png');
    image.setAttribute('x', isCrewAgent ? '0' : '1');
    image.setAttribute('y', isCrewAgent ? '0' : '1');
    image.setAttribute('width', isCrewAgent ? '24' : '22');
    image.setAttribute('height', isCrewAgent ? '24' : '22');
    image.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    if (isCrewAgent) svg.classList.add('mw-icon--template-image');
    svg.append(image);
  } else {
    const use = document.createElementNS(SVG_NAMESPACE, 'use');
    use.setAttribute('href', `#${resolvedId}`);
    svg.append(use);
  }
  return svg;
}
