import React from "react";
import MarkdownContent from "./MarkdownContent";
import type { WikiPage, WikiRelationPage, WikiSourceFiles, WikiSourceTitles } from "../types";
import { api } from "../api";
import { TYPE_META } from "../lib/wikiTree";

interface Props {
  page: WikiPage;
  sourceTitles?: WikiSourceTitles;
  /** source_id -> 原始文件元信息 */
  sourceFiles?: WikiSourceFiles;
  /** 当前知识库 ID，用于构造原文件下载链接 */
  kbId?: string;
  inline?: boolean;
  onClose?: () => void;
  /** 点击来源或相关页面时导航到目标页面 */
  onNavigate?: (pageId: string) => void;
  /** 点击正文 [[Wiki 双链]] 时回调链接标题（由上层解析为页面并跳转） */
  onWikiLink?: (title: string) => void;
  /** 所有页面列表，用于将相关页面标题解析为页面 ID */
  pages?: WikiPage[];
  relationPages?: WikiRelationPage[];
}

const RELATION_LABELS: Record<string, string> = {
  related: "相关",
  uses: "使用",
  depends_on: "依赖",
  part_of: "属于",
  contrasts_with: "对比",
  describes: "描述",
  covers: "涵盖",
  references: "引用",
  mentions: "提及",
};

function relationLabel(item: WikiRelationPage): string {
  const label = RELATION_LABELS[item.relation] || item.relation;
  if (item.direction === "outgoing") return label;
  return {
    describes: "描述本页",
    covers: "涵盖本页",
    depends_on: "依赖本页",
    part_of: "包含本页",
    uses: "使用本页",
    references: "引用本页",
    mentions: "提及本页",
  }[item.relation] || `反向${label}`;
}

function WikiPageView({ page, sourceTitles, sourceFiles, kbId, inline, onClose, onNavigate, onWikiLink, pages, relationPages = [] }: Props) {
  const visibleRelations = page.page_type === "source"
    ? []
    : relationPages.filter((relation) => relation.page_type !== "source");
  const content = (
    <div className={`wiki-page-view ${inline ? "wiki-page-view--inline" : ""}`}>
      <div className="wiki-page-view__header">
        <div className="wiki-page-view__badges">
          <span className={`wiki-card__type wiki-card__type--${page.page_type}`}>
            {TYPE_META[page.page_type]?.label || page.page_type}
          </span>
        </div>
        <h2 className="wiki-page-view__title">{page.title}</h2>
        {!inline && (
          <button className="wiki-overlay__close" onClick={onClose} type="button" aria-label="关闭">
            ×
          </button>
        )}
      </div>

      {page.tags.length > 0 && (
        <div className="wiki-page-view__tags">
          {page.tags.map((tag) => (
            <span key={tag} className="wiki-card__tag">{tag}</span>
          ))}
        </div>
      )}

      <div className="wiki-page-view__content">
        {page.content ? (
          <MarkdownContent content={page.content} fold onWikiLink={onWikiLink} />
        ) : (
          <div className="wiki-panel__empty">加载页面正文中…</div>
        )}
      </div>

      {page.sources.length > 0 && (
        <div className="wiki-page-view__section">
          <h4>来源</h4>
          <ul>
            {page.sources.map((src) => {
              const title = sourceTitles?.[src] || src;
              const fileMeta = sourceFiles?.[src];
              const hasFile = !!(fileMeta?.original_path);
              const targetPage = pages?.find((p) => p.id === src);
              const canNavigate = !!(targetPage && onNavigate);
              return (
                <li key={src} title={`source_id: ${src}`}>
                  {hasFile ? (
                    <a
                      className="wiki-page-view__link wiki-page-view__file-link"
                      href={api.wikiSourceFileUrl(src, kbId)}
                      target="_blank"
                      rel="noreferrer"
                      title="打开原文件"
                    >
                      {title}
                    </a>
                  ) : (
                    <span>{title}</span>
                  )}
                  {canNavigate && (
                    <button
                      className="wiki-page-view__link wiki-page-view__nav-link"
                      onClick={() => onNavigate!(src)}
                      type="button"
                      title="查看 Wiki 页面"
                    >
                      [[页面]]
                    </button>
                  )}
                  {title !== src && <small className="wiki-page-view__source-id"> ({src})</small>}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {visibleRelations.length > 0 && (
        <div className="wiki-page-view__section">
          <h4>相关页面</h4>
          <div className="wiki-page-view__relations">
            {visibleRelations.map((relation) => (
              <button
                className="wiki-page-view__relation"
                key={`${relation.id}:${relation.relation}:${relation.direction}`}
                onClick={() => onNavigate?.(relation.id)}
                type="button"
              >
                <span>{relation.title}</span>
                <small>
                  {TYPE_META[relation.page_type]?.label || relation.page_type} · {relationLabel(relation)}
                </small>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="wiki-page-view__actions">
        {!inline && onClose && (
          <button className="wiki-card__btn" onClick={onClose} type="button">
            关闭
          </button>
        )}
      </div>
    </div>
  );

  if (inline) return content;

  return (
    <div className="wiki-overlay" role="dialog" aria-modal="true" aria-label={`Wiki 页面：${page.title}`}>
      <div className="wiki-overlay__backdrop" onClick={onClose} />
      <div className="wiki-overlay__panel">{content}</div>
    </div>
  );
}

export default React.memo(WikiPageView);
