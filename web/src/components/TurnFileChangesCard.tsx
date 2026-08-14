import { useState } from "react";
import { openLocalPath } from "../lib/localPath";
import type { TurnFileChangeSummary } from "../types";

function fileLabel(path: string): { directory: string; name: string } {
  const normalized = path.replace(/\\/g, "/");
  const index = normalized.lastIndexOf("/");
  return index < 0
    ? { directory: "", name: normalized }
    : { directory: normalized.slice(0, index + 1), name: normalized.slice(index + 1) };
}

export default function TurnFileChangesCard({ files }: { files?: TurnFileChangeSummary[] }) {
  const [expanded, setExpanded] = useState(false);
  const byPath = new Map<string, TurnFileChangeSummary>();
  for (const file of files || []) if (file.path) byPath.set(file.path, file);
  const list = Array.from(byPath.values());
  if (list.length === 0) return null;
  const visible = expanded ? list : list.slice(0, 3);
  const added = list.reduce((sum, file) => sum + file.added, 0);
  const removed = list.reduce((sum, file) => sum + file.removed, 0);
  return (
    <section className="turn-file-changes" aria-label={`已编辑 ${list.length} 个文件`}>
      <div className="turn-file-changes__header">
        <strong>已编辑 {list.length} 个文件</strong>
        <span className="turn-file-changes__totals">
          {added > 0 && <em className="is-added">+{added.toLocaleString("en-US")}</em>}
          {removed > 0 && <em className="is-removed">-{removed.toLocaleString("en-US")}</em>}
        </span>
      </div>
      <ul className="turn-file-changes__list">
        {visible.map((file) => {
          const label = fileLabel(file.path);
          const statusLabel = file.status === "added" ? "新增" : file.status === "deleted" ? "删除" : "修改";
          return (
            <li key={file.path} data-file-status={file.status}>
              <button
                type="button"
                title={file.status === "deleted" ? "文件已删除" : file.path}
                disabled={file.status === "deleted"}
                onClick={() => void openLocalPath(file.path)}
              >
                <span className="turn-file-changes__path">
                  {label.directory && <span>{label.directory}</span>}
                  <strong>{label.name}</strong>
                </span>
                <span className="turn-file-changes__badges">
                  <em className={`is-${file.status}`}>{statusLabel}</em>
                  {file.added > 0 && <em className="is-added">+{file.added.toLocaleString("en-US")}</em>}
                  {file.removed > 0 && <em className="is-removed">-{file.removed.toLocaleString("en-US")}</em>}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {!expanded && list.length > 3 && (
        <button className="turn-file-changes__more" type="button" onClick={() => setExpanded(true)}>
          再显示 {list.length - 3} 个文件
        </button>
      )}
    </section>
  );
}
