import { Editor } from '@tiptap/core';
import { Markdown } from '@tiptap/markdown';
import StarterKit from '@tiptap/starter-kit';

const WIKI_LINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;
const MARKDOWN_WIKI_LINK_RE = /\[([^\]]+)\]\(wiki:([^)]+)\)/g;

function markdownForEditor(markdown: string): string {
  return markdown.replace(WIKI_LINK_RE, (_match, target: string, label?: string) => {
    const normalizedTarget = target.trim();
    const visible = (label || target).trim();
    return `[${visible}](wiki:${encodeURIComponent(normalizedTarget)})`;
  });
}

function markdownForStorage(markdown: string): string {
  return markdown.replace(MARKDOWN_WIKI_LINK_RE, (_match, label: string, encodedTarget: string) => {
    let target = encodedTarget;
    try {
      target = decodeURIComponent(encodedTarget);
    } catch {
      // 保留原值：损坏的百分号编码不应阻止用户保存正文。
    }
    return label === target ? `[[${target}]]` : `[[${target}|${label}]]`;
  });
}

export interface WikiEditorHandle {
  destroy(): void;
  flush(): string;
}

export interface WikiEditorOptions {
  element: HTMLElement;
  markdown: string;
  onChange(markdown: string): void;
  onWikiLink(title: string): void;
}

/**
 * Wiki 正文所见即所得编辑器。
 *
 * 编辑态由 TipTap/ProseMirror 管理，Markdown 只是输入输出协议；用户不会看到源码。
 * WikiLink 在编辑器内部使用 wiki: 链接表达，保存时恢复为 [[target|label]]。
 */
export function mountWikiEditor(options: WikiEditorOptions): WikiEditorHandle {
  const handleWikiLink = (event: Event): void => {
    const link = (event.target as HTMLElement | null)?.closest<HTMLAnchorElement>('a[href^="wiki:"]');
    if (!link) return;
    event.preventDefault();
    const raw = link.getAttribute('href')?.slice('wiki:'.length) || '';
    try {
      options.onWikiLink(decodeURIComponent(raw));
    } catch {
      options.onWikiLink(raw);
    }
  };
  options.element.addEventListener('click', handleWikiLink);

  const editor = new Editor({
    element: options.element,
    extensions: [
      StarterKit.configure({
        link: {
          openOnClick: false,
          autolink: true,
          protocols: ['wiki'],
          HTMLAttributes: {
            class: 'wiki-editor__wikilink',
          },
        },
      }),
      Markdown,
    ],
    content: markdownForEditor(options.markdown),
    contentType: 'markdown',
    editorProps: {
      attributes: {
        class: 'wiki-editor__content',
        spellcheck: 'true',
        'aria-label': 'Wiki 正文',
      },
    },
    onUpdate: ({ editor: current }) => {
      options.onChange(markdownForStorage(current.getMarkdown()));
    },
  });

  return {
    destroy: () => {
      options.element.removeEventListener('click', handleWikiLink);
      editor.destroy();
    },
    flush: () => markdownForStorage(editor.getMarkdown()),
  };
}

export const __wikiEditorTest = {
  markdownForEditor,
  markdownForStorage,
};
