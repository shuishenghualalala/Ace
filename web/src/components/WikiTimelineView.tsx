import { useMemo } from "react";
import type { WikiPage } from "../types";
import { sortByUpdatedAt } from "../lib/wikiTree";
import WikiListItem from "./WikiListItem";

interface Props {
  pages: WikiPage[];
  selectedId: string | null;
  selectedIds: Set<string>;
  highlightedIds: Set<string>;
  onSelectPage: (page: WikiPage) => void;
  onToggleSelect: (page: WikiPage) => void;
  onDelete: (page: WikiPage) => void;
}

interface Group {
  label: string;
  pages: WikiPage[];
}

export default function WikiTimelineView({
  pages,
  selectedId,
  selectedIds,
  highlightedIds,
  onSelectPage,
  onToggleSelect,
  onDelete,
}: Props) {
  const groups = useMemo(() => groupPagesByDate(pages), [pages]);

  if (groups.length === 0) {
    return (
      <div className="wiki-list-empty">
        还没有 Wiki 页面。上传文档或粘贴一段文字，AI 会自动整理成可检索的笔记。
      </div>
    );
  }

  return (
    <div className="wiki-timeline">
      {groups.map((group) => (
        <div key={group.label} className="wiki-timeline-group">
          <div className="wiki-timeline-group__header">{group.label}</div>
          <ul className="wiki-list">
            {group.pages.map((page) => (
              <WikiListItem
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
        </div>
      ))}
    </div>
  );
}

function groupPagesByDate(pages: WikiPage[]): Group[] {
  const sorted = sortByUpdatedAt(pages);
  const now = new Date();
  const today = stripTime(now);
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);

  const bucketOrder = ["今天", "昨天", "本周", "本月", "更早"];
  const buckets: Record<string, WikiPage[]> = {
    今天: [],
    昨天: [],
    本周: [],
    本月: [],
    更早: [],
  };

  for (const page of sorted) {
    const d = new Date(page.updated_at * 1000);
    const date = stripTime(d);
    if (date.getTime() === today.getTime()) {
      buckets["今天"].push(page);
    } else if (date.getTime() === yesterday.getTime()) {
      buckets["昨天"].push(page);
    } else if (isSameWeek(d, now)) {
      buckets["本周"].push(page);
    } else if (isSameMonth(d, now)) {
      buckets["本月"].push(page);
    } else {
      buckets["更早"].push(page);
    }
  }

  return bucketOrder
    .map((label) => ({ label, pages: buckets[label] }))
    .filter((g) => g.pages.length > 0);
}

function stripTime(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function isSameWeek(a: Date, b: Date): boolean {
  const startOfWeek = (d: Date) => {
    const copy = stripTime(d);
    copy.setDate(copy.getDate() - ((copy.getDay() + 6) % 7));
    return copy.getTime();
  };
  return startOfWeek(a) === startOfWeek(b);
}

function isSameMonth(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth();
}
