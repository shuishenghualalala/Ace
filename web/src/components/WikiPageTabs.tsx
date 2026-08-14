import type { WikiOpenTab } from "../lib/wikiTabs";
import { tabKey } from "../lib/wikiTabs";

interface Props {
  tabs: WikiOpenTab[];
  /** 当前激活 Tab 的 key（tabKey），可能为 null。 */
  activeKey: string | null;
  /** tabKey -> 显示标题。 */
  titles: Record<string, string>;
  onActivate: (tab: WikiOpenTab) => void;
  onClose: (key: string) => void;
}

/** Wiki 详情面板顶部的 Tab 栏：横向滚动 pill，支持切换与关闭。 */
export default function WikiPageTabs({ tabs, activeKey, titles, onActivate, onClose }: Props) {
  if (tabs.length === 0) return null;
  return (
    <div className="wiki-tabs" role="tablist">
      {tabs.map((tab) => {
        const key = tabKey(tab);
        const active = key === activeKey;
        return (
          <div
            key={key}
            role="tab"
            aria-selected={active}
            className={`wiki-tabs__tab ${active ? "is-active" : ""}`}
          >
            <button
              type="button"
              className="wiki-tabs__label"
              title={titles[key] ?? key}
              onClick={() => onActivate(tab)}
            >
              {titles[key] ?? key}
            </button>
            <button
              type="button"
              className="wiki-tabs__close"
              title="关闭"
              aria-label="关闭"
              onClick={(e) => {
                e.stopPropagation();
                onClose(key);
              }}
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
