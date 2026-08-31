import { createIcon, ICON_IDS, type IconId } from './components/icon';

export type AvatarRef =
  | { kind: 'image'; src: string; alt?: string }
  | { kind: 'icon'; id: IconId };

export type AvatarIdentity =
  | { kind: 'crew'; id?: string | undefined; name?: string | undefined; avatar?: AvatarRef | undefined }
  | { kind: 'companion-user'; id: string; name: string; avatar?: AvatarRef | undefined }
  | { kind: 'companion-agent'; id: string; name: string; avatar?: AvatarRef | undefined }
  | { kind: 'external-agent'; id: string; name: string; provider: string; badge?: string | undefined; tone?: number | undefined; avatar?: AvatarRef | undefined }
  | { kind: 'external-team'; id: string; name: string; badge?: string | undefined; avatar?: AvatarRef | undefined }
  | { kind: 'service'; id: string; name: string; avatar?: AvatarRef | undefined }
  | { kind: 'wiki'; id: string; name?: string | undefined; avatar?: AvatarRef | undefined };

export interface AvatarElementOptions {
  className?: string;
  iconClassName?: string;
  size?: 16 | 18 | 20 | 24 | 32 | 40;
  large?: boolean;
  group?: boolean;
}

const AVATAR_ICON_BY_KIND: Partial<Record<AvatarIdentity['kind'], IconId>> = {
  crew: 'avatar-headphones',
  wiki: 'icon-wiki',
};

function escapeAttribute(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character] ?? character);
}

function safeAvatarSource(value: string): string | null {
  const source = value.trim();
  if (!source) return null;
  if (source.startsWith('data:image/')) return source;
  if (/^https?:\/\//i.test(source)) return source;
  if (source.startsWith('./') || source.startsWith('../')) return source;
  return null;
}

export function parseAvatarRef(value: unknown): AvatarRef | undefined {
  if (typeof value === 'string') {
    const src = safeAvatarSource(value);
    return src ? { kind: 'image', src } : undefined;
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const src = typeof record.src === 'string'
    ? safeAvatarSource(record.src)
    : typeof record.url === 'string'
      ? safeAvatarSource(record.url)
      : null;
  if (src) {
    return {
      kind: 'image',
      src,
      ...(typeof record.alt === 'string' && record.alt.trim() ? { alt: record.alt.trim() } : {}),
    };
  }
  const iconId = record.kind === 'icon' && typeof record.id === 'string' ? record.id : '';
  if (iconId && ICON_IDS.some((id) => id === iconId)) {
    return { kind: 'icon', id: iconId as IconId };
  }
  return undefined;
}

export function avatarInitial(name: string, badge?: string): string {
  const source = badge?.trim() || name.trim();
  return Array.from(source)[0]?.toLocaleUpperCase() || '?';
}

export function avatarTone(provider: string): number {
  const normalized = provider.trim().toLowerCase();
  const knownProviders: Record<string, number> = {
    kimi: 0,
    codex: 1,
    hermes: 2,
    'claude-code': 3,
    claude: 3,
    gemini: 4,
    sites: 5,
  };
  if (normalized in knownProviders) return knownProviders[normalized];
  let hash = 0;
  for (const character of normalized) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  return Math.abs(hash) % 6;
}

function identityName(identity: AvatarIdentity): string {
  if (identity.kind === 'crew') return identity.name?.trim() || 'Crew';
  if (identity.kind === 'wiki') return identity.name?.trim() || 'Wiki';
  return identity.name.trim() || 'Agent';
}

function identityAvatar(identity: AvatarIdentity): AvatarRef | undefined {
  return identity.avatar ?? (AVATAR_ICON_BY_KIND[identity.kind]
    ? { kind: 'icon', id: AVATAR_ICON_BY_KIND[identity.kind]! }
    : undefined);
}

function identityTone(identity: AvatarIdentity): number | undefined {
  if (identity.kind !== 'external-agent') return undefined;
  return identity.tone ?? avatarTone(identity.provider);
}

function applyIdentityClasses(element: HTMLElement, identity: AvatarIdentity, options: AvatarElementOptions): void {
  element.classList.add(`mw-avatar--${identity.kind}`);
  if (identity.kind === 'companion-agent' || identity.kind === 'external-agent') element.classList.add('is-agent');
  if (options.large) element.classList.add('is-large');
  if (options.group) element.classList.add('is-group');
  const tone = identityTone(identity);
  if (tone !== undefined) element.classList.add(`agent-provider-tone-${tone}`);
}

export function createAvatarElement(
  identity: AvatarIdentity,
  options: AvatarElementOptions = {},
): HTMLSpanElement {
  const element = document.createElement('span');
  element.className = options.className || 'mw-avatar';
  element.setAttribute('aria-hidden', 'true');
  applyIdentityClasses(element, identity, options);

  const name = identityName(identity);
  const fallback = avatarInitial(name, identity.kind === 'external-agent' ? identity.badge : undefined);
  const avatar = identityAvatar(identity);
  if (avatar?.kind === 'image') {
    const source = safeAvatarSource(avatar.src);
    if (source) {
      const image = document.createElement('img');
      image.className = 'mw-avatar__image';
      image.src = source;
      image.alt = avatar.alt || '';
      image.addEventListener('error', () => {
        element.replaceChildren(document.createTextNode(fallback));
      }, { once: true });
      element.appendChild(image);
      return element;
    }
  }
  if (avatar?.kind === 'icon') {
    const iconOptions = options.size === undefined
      ? { ...(options.iconClassName ? { className: options.iconClassName } : {}) }
      : { size: options.size, ...(options.iconClassName ? { className: options.iconClassName } : {}) };
    const icon = createIcon(avatar.id, iconOptions);
    if (identity.kind === 'crew' && avatar.id === 'avatar-headphones') {
      icon.querySelector('use')?.setAttribute('href', './crew-ui-symbols.svg#avatar-headphones');
    }
    element.append(icon);
    return element;
  }
  element.textContent = fallback;
  return element;
}

export function avatarMarkup(identity: AvatarIdentity, className = 'mw-avatar'): string {
  const classes = [className, `mw-avatar--${identity.kind}`];
  if (identity.kind === 'companion-agent' || identity.kind === 'external-agent') classes.push('is-agent');
  const tone = identityTone(identity);
  if (tone !== undefined) classes.push(`agent-provider-tone-${tone}`);
  const name = identityName(identity);
  const fallback = avatarInitial(name, identity.kind === 'external-agent' ? identity.badge : undefined);
  const avatar = identityAvatar(identity);
  if (avatar?.kind === 'image') {
    const source = safeAvatarSource(avatar.src);
    if (source) {
      return `<span class="${escapeAttribute(classes.join(' '))}" aria-hidden="true"><img class="mw-avatar__image" src="${escapeAttribute(source)}" alt=""><span class="mw-avatar__fallback">${escapeAttribute(fallback)}</span></span>`;
    }
  }
  if (avatar?.kind === 'icon') {
    const iconMarkup = identity.kind === 'crew' && avatar.id === 'avatar-headphones'
      ? '<svg class="mw-avatar__icon" viewBox="0 0 32 32" aria-hidden="true"><use href="#avatar-headphones"></use></svg>'
      : `<svg class="mw-avatar__icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#${escapeAttribute(avatar.id)}"></use></svg>`;
    return `<span class="${escapeAttribute(classes.join(' '))}" aria-hidden="true">${iconMarkup}</span>`;
  }
  return `<span class="${escapeAttribute(classes.join(' '))}" aria-hidden="true"><span>${escapeAttribute(fallback)}</span></span>`;
}
