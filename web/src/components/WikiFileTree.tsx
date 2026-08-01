import type { WikiPage } from "../types";
import type { WikiTreeNode } from "../lib/wikiTree";

import { TYPE_META, vaultDocumentLabel, vaultFolderLabel } from "../lib/wikiTree";
import WikiIcon from "./WikiIcon";

interface Props {
  nodes: WikiTreeNode[];
  selectedId: string | null;
  selectedDocumentName: "Home.md" | "index.md" | null;
  selectedIds: Set<string>;
  expandedPaths: Set<string>;
  onTogglePath: (path: string) => void;
  onSelectPage: (page: WikiPage) => void;
  onSelectDocument: (name: "Home.md" | "index.md") => void;
  onToggleSelect: (page: WikiPage) => void;
  onDelete: (page: WikiPage) => void;
  depth?: number;
}

export default function WikiFileTree({
  nodes,
  selectedId,
  selectedDocumentName,
  selectedIds,
  expandedPaths,
  onTogglePath,
  onSelectPage,
  onSelectDocument,
  onToggleSelect,
  onDelete,
  depth = 0,
}: Props) {
  return (
    <ul className="wiki-tree__list">
      {nodes.map((node) =>
        node.kind === "folder" ? (
          <li key={node.path} className="wiki-tree__folder">
            <button
              className="wiki-tree__folder-toggle"
              onClick={() => onTogglePath(node.path)}
              type="button"
              style={{ paddingLeft: `${depth * 16 + 6}px` }}
            >
              <span
                className={`wiki-tree__folder-caret ${
                  expandedPaths.has(node.path) ? "wiki-tree__folder-caret--open" : ""
                }`}
              >
                <WikiIcon name="caret" size={12} />
              </span>
              <span className="wiki-tree__folder-icon">
                <WikiIcon name="folder" size={14} />
              </span>
              <span className="wiki-tree__folder-name">{vaultFolderLabel(node.path, node.name)}</span>
            </button>
            {expandedPaths.has(node.path) && node.children.length > 0 && (
              <WikiFileTree
                nodes={node.children}
                selectedId={selectedId}
                selectedDocumentName={selectedDocumentName}
                selectedIds={selectedIds}
                expandedPaths={expandedPaths}
                onTogglePath={onTogglePath}
                onSelectPage={onSelectPage}
                onSelectDocument={onSelectDocument}
                onToggleSelect={onToggleSelect}
                onDelete={onDelete}
                depth={depth + 1}
              />
            )}
          </li>
        ) : node.kind === "document" ? (
          <li
            key={node.path}
            className={`wiki-tree__item wiki-tree__item--document ${
              selectedDocumentName === node.name ? "wiki-tree__item--active" : ""
            }`}
          >
            <button
              className="wiki-tree__label"
              onClick={() => onSelectDocument(node.name)}
              type="button"
              style={{ paddingLeft: `${depth * 16 + 6}px` }}
            >
              <span className="wiki-card__type wiki-card__type--muted">文件</span>
              <span className="wiki-tree__title">{vaultDocumentLabel(node.name)}</span>
            </button>
          </li>
        ) : (
          <li
            key={node.page.id}
            className={`wiki-tree__item ${selectedId === node.page.id ? "wiki-tree__item--active" : ""}`}
          >
            <input
              type="checkbox"
              className="wiki-tree__check"
              checked={selectedIds.has(node.page.id)}
              onChange={() => onToggleSelect(node.page)}
              onClick={(e) => e.stopPropagation()}
              title="选中"
            />
            <button
              className="wiki-tree__label"
              onClick={() => onSelectPage(node.page)}
              type="button"
              style={{ paddingLeft: `${depth * 16 + 6}px` }}
            >
              <span className="wiki-card__type wiki-card__type--muted">
                {TYPE_META[node.page.page_type].shortLabel}
              </span>
              <span className="wiki-tree__title">{node.page.title}</span>

            </button>
            <div className="wiki-tree__actions">
              <button
                className="wiki-tree__act wiki-tree__act--danger"
                onClick={() => onDelete(node.page)}
                type="button"
                title="删除"
              >
                删除
              </button>
            </div>
          </li>
        ),
      )}
    </ul>
  );
}
