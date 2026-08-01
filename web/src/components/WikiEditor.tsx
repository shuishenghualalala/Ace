import { useState } from "react";
import type { WikiPage, WikiPageType } from "../types";

interface Props {
  page?: WikiPage | null;
  onSave: (page: Partial<WikiPage>) => void;
  onCancel: () => void;
}

const PAGE_TYPES: { value: WikiPageType; label: string }[] = [
  { value: "entity", label: "实体" },
  { value: "topic", label: "主题" },
  { value: "source", label: "来源摘要" },
  { value: "comparison", label: "对比分析" },
  { value: "synthesis", label: "综合报告" },
];

export default function WikiEditor({ page, onSave, onCancel }: Props) {
  const isNew = !page;
  const [title, setTitle] = useState(page?.title ?? "");
  const [pageType, setPageType] = useState<WikiPageType>(page?.page_type ?? "topic");
  const [content, setContent] = useState(page?.content ?? "");
  const [tags, setTags] = useState(page?.tags.join(", ") ?? "");
  const [related, setRelated] = useState(page?.related.join(", ") ?? "");
  const [sources, setSources] = useState(page?.sources.join(", ") ?? "");

  const handleSubmit = () => {
    onSave({
      title: title.trim(),
      page_type: pageType,
      content,
      tags: splitList(tags),
      related: splitList(related),
      sources: splitList(sources),
    });
  };

  return (
    <div className="wiki-overlay" role="dialog" aria-modal="true" aria-label={isNew ? "新建 Wiki 页面" : "编辑 Wiki 页面"}>
      <div className="wiki-overlay__backdrop" onClick={onCancel} />
      <div className="wiki-overlay__panel">
        <div className="wiki-editor">
          <div className="wiki-editor__header">
            <h3>{isNew ? "新建 Wiki 页面" : `编辑：${page?.title || "未命名"}`}</h3>
            <button className="wiki-overlay__close" onClick={onCancel} type="button" aria-label="取消">
              ×
            </button>
          </div>

          <div className="wiki-editor__field">
            <label htmlFor="wiki-title">标题</label>
            <input id="wiki-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="页面标题" />
          </div>

          <div className="wiki-editor__row">
            <div className="wiki-editor__field">
              <label htmlFor="wiki-type">类型</label>
              <select id="wiki-type" value={pageType} onChange={(e) => setPageType(e.target.value as WikiPageType)}>
                {PAGE_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="wiki-editor__field">
            <label htmlFor="wiki-tags">标签（逗号分隔）</label>
            <input id="wiki-tags" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="标签1, 标签2" />
          </div>

          <div className="wiki-editor__field">
            <label htmlFor="wiki-related">相关页面（逗号分隔）</label>
            <input id="wiki-related" value={related} onChange={(e) => setRelated(e.target.value)} placeholder="页面A, 页面B" />
          </div>

          <div className="wiki-editor__field">
            <label htmlFor="wiki-sources">来源（逗号分隔）</label>
            <input id="wiki-sources" value={sources} onChange={(e) => setSources(e.target.value)} placeholder="source-id, URL" />
          </div>

          <div className="wiki-editor__field">
            <label htmlFor="wiki-content">内容（Markdown）</label>
            <textarea
              id="wiki-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={16}
              placeholder="支持 Markdown 与 [[页面名]] 双向链接"
            />
          </div>

          <div className="wiki-editor__actions">
            <button className="wiki-card__btn" onClick={onCancel} type="button">
              取消
            </button>
            <button className="wiki-card__btn wiki-card__btn--primary" onClick={handleSubmit} type="button">
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}
