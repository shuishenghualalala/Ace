import React, { useMemo } from "react";
import type { WikiPage } from "../types";
import { groupByType, summaryOf } from "../lib/wikiTree";
import WikiIcon from "./WikiIcon";

interface Props {
  pages: WikiPage[];
  selectedId: string | null;
  selectedIds: Set<string>;
  highlightedIds: Set<string>;
  onSelectPage: (page: WikiPage) => void;
  onToggleSelect: (page: WikiPage) => void;
  onDelete: (page: WikiPage) => void;
}

export default function WikiTypeView({
  pages,
  selectedId,
  selectedIds,
  highlightedIds,
  onSelectPage,
  onToggleSelect,
  onDelete,
}: Props) {
  const groups = useMemo(() => groupByType(pages), [pages]);

  if (groups.length === 0) {
    return (
      <div className="wiki-list-empty">
        还没有 Wiki 页面。上传文档或粘贴一段文字，AI 会自动整理成可检索的笔记。
      </div>
    );
  }

  return (
    <div className="wiki-grouped">
      {groups.map((group) => (
        <section key={group.type} className="wiki-grouped__section">
          <h4 className="wiki-grouped__title">
            <WikiIcon name={group.icon} size={16} />
            {group.label}
            <span className="wiki-grouped__count">{group.pages.length}</span>
          </h4>
          <ul className="wiki-list wiki-list--compact">
            {group.pages.map((page) => (
              <WikiTypeItem
                key={page.id}
                page={page}
                selected={selectedId === page.id}
                checked={selectedIds.has(page.id)}
                highlighted={highlightedIds.has(page.id)}
                onSelect={() => onSelectPage(page)}
                onToggleSelect={() => onToggleSelect(page)}
                onDelete={() => onDelete(page)}
              />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

interface ItemProps {
  page: WikiPage;
  selected: boolean;
  checked: boolean;
  highlighted: boolean;
  onSelect: () => void;
  onToggleSelect: () => void;
  onDelete: () => void;
}

const WikiTypeItem = React.memo(function WikiTypeItem({
  page,
  selected,
  checked,
  highlighted,
  onSelect,
  onToggleSelect,
  onDelete,
}: ItemProps) {
  return (
    <li
      className={[
        "wiki-list-item",
        "wiki-list-item--compact",
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
        <span className="wiki-list-item__title-wrap">
          <span className="wiki-list-item__title">{page.title}</span>
          <span className="wiki-list-item__summary">{summaryOf(page)}</span>
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
});
