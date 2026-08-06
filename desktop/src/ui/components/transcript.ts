import { renderMarkdownHtml, renderMarkdownHtmlStreaming } from '../markdown';

export type TranscriptVariant = 'user' | 'assistant' | 'agent';

/**
 * Creates the stable message root used by transcript renderers.
 * Legacy `msg` classes remain during the phased CSS migration.
 */
export function createTranscriptMessage(
  messageId: string,
  variant: TranscriptVariant,
): HTMLElement {
  const root = document.createElement('article');
  root.className = `msg mw-transcript mw-transcript--${variant}`;
  if (variant === 'user') root.classList.add('user');
  root.dataset.messageId = messageId;
  return root;
}

export function createTranscriptBody(): HTMLElement {
  const body = document.createElement('div');
  body.className = 'msg__body mw-transcript__body';
  return body;
}

export function createTranscriptText(className = ''): HTMLElement {
  const text = document.createElement('div');
  text.className = `msg__text mw-transcript__content${className ? ` ${className}` : ''}`;
  return text;
}

/**
 * Replaces only a message's Markdown children, keeping the keyed message and
 * text container stable so streaming updates do not reset the scroll anchor.
 * The only HTML accepted here is the DOMPurify-sanitized Markdown output.
 */
export function patchTranscriptMarkdown(
  target: HTMLElement,
  source: string,
  streaming = false,
): void {
  const template = document.createElement('template');
  template.innerHTML = source
    ? (streaming ? renderMarkdownHtmlStreaming(source) : renderMarkdownHtml(source))
    : '';
  target.replaceChildren(template.content.cloneNode(true));
  target.hidden = streaming && !source;
}

export function createTranscriptMarkdown(
  source: string,
  options: {
    streaming?: boolean;
    textFor?: string | undefined;
    className?: string | undefined;
  } = {},
): HTMLElement {
  const text = createTranscriptText(`md-body chat-markdown${options.className ? ` ${options.className}` : ''}`);
  if (options.textFor) text.dataset.textFor = options.textFor;
  patchTranscriptMarkdown(text, source, options.streaming);
  return text;
}

export function appendTranscriptFooter(
  body: HTMLElement,
  metaParts: string[],
  actions: HTMLElement[],
): void {
  const footer = document.createElement('footer');
  footer.className = 'msg__footer mw-transcript__footer';
  const meta = metaParts.filter(Boolean).join(' · ');
  if (meta) {
    const metaElement = document.createElement('span');
    metaElement.className = 'msg__meta mw-transcript__meta';
    metaElement.textContent = meta;
    footer.appendChild(metaElement);
  }
  if (actions.length > 0) {
    const actionGroup = document.createElement('span');
    actionGroup.className = 'msg__actions mw-transcript__actions';
    actionGroup.append(...actions);
    footer.appendChild(actionGroup);
  }
  if (footer.childNodes.length > 0) body.appendChild(footer);
}
