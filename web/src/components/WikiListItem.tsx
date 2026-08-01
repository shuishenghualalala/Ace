import React from "react";
import type { WikiPage } from "../types";
import { summaryOf, TYPE_META } from "../lib/wikiTree";
import WikiIcon from "./WikiIcon";

interface Props {
  page: WikiPage;
  selected: boolean;
  checked: boolean;
  highlighted: boolean;
  onSelect: () => void;
  onToggleSelect: () => void;
  onDelete: () => void;
}

function WikiListItem({
  page,
  selected,
  checked,
  highlighted,
  onSelect,
  onToggleSelect,
  onDelete,
}: Props) {
  const meta = TYPE_META[page.page_type];
  const time = new Date(page.updated_at * 1000).toLocaleString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <li
      className={[
        "wiki-list-item",
        selected ? "wiki-list-item--active" : "",
        highlighted ? "wiki-list-item--highlight" : "",
      ].join(" ")}
    >
      <input
        type="checkbox"
        className="wiki-tree__check"
        checked={checked}
        onChange={onToggleSelect}
        onClick={(e) => e.stopPropagation()}
        title="选中"
      />
      <button className="wiki-list-item__main" onClick={onSelect} type="button">
        <span className="wiki-list-item__icon" title={meta.label}>
          <WikiIcon name={meta.icon} size={18} />
        </span>
        <span className="wiki-list-item__title-wrap">
          <span className="wiki-list-item__title">{page.title}</span>
          <span className="wiki-list-item__summary">{summaryOf(page)}</span>
          <span className="wiki-list-item__meta">
            {page.tags.length > 0 && (
              <span className="wiki-list-item__tags">
                {page.tags.map((tag) => (
                  <span key={tag} className="wiki-card__tag">
                    {tag}
                  </span>
                ))}
              </span>
            )}
            <span className="wiki-list-item__time">{time}</span>
          </span>
        </span>
      </button>
      <div className="wiki-list-item__actions">
        <button
          className="wiki-tree__act wiki-tree__act--danger"
          onClick={onDelete}
          type="button"
          title="删除"
        >
          删除
        </button>
      </div>
    </li>
  );
}

export default React.memo(WikiListItem);
