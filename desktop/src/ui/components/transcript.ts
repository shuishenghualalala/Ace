import { renderMarkdownHtml, renderMarkdownHtmlStreaming } from '../markdown';
import { stableMarkdownPrefix } from './transcript-markdown-blocks';

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

interface RenderedMarkdownChild {
  node: ChildNode;
  markup: string;
}

interface StreamingMarkdown {
  source: string;
  sealedOffset: number;
  sealedChildren: number;
  children: RenderedMarkdownChild[];
}

const streamingMarkdown = new WeakMap<HTMLElement, StreamingMarkdown>();

function markdownFragment(document: Document, source: string, streaming: boolean): DocumentFragment {
  const template = document.createElement('template');
  template.innerHTML = streaming ? renderMarkdownHtmlStreaming(source) : renderMarkdownHtml(source);
  return template.content;
}

function markdownChildren(document: Document, source: string, streaming: boolean): RenderedMarkdownChild[] {
  return Array.from(markdownFragment(document, source, streaming).childNodes, (node) => ({
    node,
    // Keep the original markup because hydration can add code and diagram UI.
    markup: node.nodeType === Node.ELEMENT_NODE ? (node as HTMLElement).outerHTML : (node.nodeValue ?? ''),
  }));
}

function replaceMarkdownSuffix(
  target: HTMLElement,
  previous: RenderedMarkdownChild[],
  from: number,
  next: RenderedMarkdownChild[],
): HTMLElement[] {
  const boundary = from > 0 ? previous[from - 1]!.node : null;
  let old = boundary ? boundary.nextSibling : target.firstChild;
  let matching = 0;
  for (const fresh of next) {
    const known = previous[from + matching];
    if (!old || !known || old !== known.node || old.nodeType !== fresh.node.nodeType || known.markup !== fresh.markup) break;
    old = old.nextSibling;
    matching += 1;
  }

  while (old) {
    const nextOld = old.nextSibling;
    old.remove();
    old = nextOld;
  }
  previous.length = from + matching;
  const fragment = target.ownerDocument.createDocumentFragment();
  const changed: HTMLElement[] = [];
  for (let index = matching; index < next.length; index += 1) {
    const child = next[index]!;
    previous.push(child);
    fragment.append(child.node);
    if (child.node instanceof HTMLElement) changed.push(child.node);
  }
  if (fragment.hasChildNodes()) target.append(fragment);
  return changed;
}

/**
 * Keeps sealed blocks and matching nodes alive across streaming patches. Only
 * the mutable suffix is parsed during append, and final or revised responses
 * use canonical whole-message Markdown. Returns newly mounted elements.
 */
export function patchTranscriptMarkdown(
  target: HTMLElement,
  source: string,
  streaming = false,
): readonly HTMLElement[] {
  target.hidden = streaming && !source;
  const previous = streamingMarkdown.get(target);
  const lastSealed = previous && previous.sealedChildren > 0
    ? previous.children[previous.sealedChildren - 1]?.node
    : undefined;
  const appending = Boolean(previous && streaming && source.startsWith(previous.source)
    && (!lastSealed || lastSealed.parentNode === target));
  if (appending && previous?.source === source) return [];

  const owner = target.ownerDocument;
  if (!streaming && !previous) {
    const fragment = markdownFragment(owner, source, false);
    const changed = Array.from(fragment.children).filter((node): node is HTMLElement => node instanceof HTMLElement);
    target.replaceChildren(fragment);
    return changed;
  }

  const children = previous?.children ?? [];
  const offset = previous?.sealedOffset ?? 0;
  const tail = source.slice(offset);
  // The full parser strips a BOM only at the document start. Its blank-input
  // shortcut can also drop a BOM that makes up the entire isolated tail.
  if (!streaming || (previous && !appending) || tail.includes('\uFEFF')) {
    const changed = replaceMarkdownSuffix(target, children, 0, markdownChildren(owner, source, streaming));
    if (streaming) {
      streamingMarkdown.set(target, { source, sealedOffset: 0, sealedChildren: 0, children });
    } else {
      streamingMarkdown.delete(target);
    }
    return changed;
  }

  const stableChildren = previous?.sealedChildren ?? 0;
  const oldLength = previous?.source.length ?? 0;
  const newLines = source.indexOf('\n', oldLength) !== -1 || source.indexOf('\r', oldLength) !== -1
    || previous?.source.endsWith('\r');
  const newSeal = newLines ? stableMarkdownPrefix(tail) : 0;
  const sealed = newSeal ? markdownChildren(owner, tail.slice(0, newSeal), false) : [];
  const live = markdownChildren(owner, tail.slice(newSeal), true);
  const changed = replaceMarkdownSuffix(target, children, stableChildren, [...sealed, ...live]);
  streamingMarkdown.set(target, {
    source,
    sealedOffset: offset + newSeal,
    sealedChildren: stableChildren + sealed.length,
    children,
  });
  return changed;
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
