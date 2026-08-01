import type { WikiPage } from "../types";

interface Props {
  page: WikiPage;
  onView?: (page: WikiPage) => void;
  onDelete?: (page: WikiPage) => void;
}

export default function WikiCard({ page, onView, onDelete }: Props) {
  const preview = (page.summary || page.content || "").slice(0, 160).replace(/\s+/g, " ").trim();
  return (
    <div className="wiki-card">
      <div className="wiki-card__header">
        <span className={`wiki-card__type wiki-card__type--${page.page_type}`}>
          {labelForType(page.page_type)}
        </span>
        <span className="wiki-card__title" title={page.title}>
          {page.title}
        </span>
      </div>
      <div className="wiki-card__body">
        <p className="wiki-card__preview">{preview || "（无内容）"}</p>
        {page.tags.length > 0 && (
          <div className="wiki-card__tags">
            {page.tags.map((tag) => (
              <span key={tag} className="wiki-card__tag">{tag}</span>
            ))}
          </div>
        )}
      </div>
      <div className="wiki-card__actions">
        <button className="wiki-card__btn" onClick={() => onView?.(page)} type="button">
          查看
        </button>
        {onDelete && (
          <button className="wiki-card__btn wiki-card__btn--danger" onClick={() => onDelete(page)} type="button">
            删除
          </button>
        )}
      </div>
    </div>
  );
}

function labelForType(type: WikiPage["page_type"]): string {
  const map: Record<WikiPage["page_type"], string> = {
    entity: "实体",
    topic: "主题",
    source: "来源摘要",
    comparison: "对比",
    synthesis: "综合",
  };
  return map[type] ?? type;
}
