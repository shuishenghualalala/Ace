import type { AppConfig } from "../types";

interface Props {
  title: string;
  connected: boolean;
  config: AppConfig | null;
  boardOpen: boolean;
  onToggleBoard: () => void;
  onModelChange: (modelId: string) => void;
  activeModelId: string;
}

export default function TopBar({ title, connected, config, boardOpen, onToggleBoard, onModelChange, activeModelId }: Props) {
  return (
    <div className="topbar">
      <div className="topbar__title">{title}</div>
      <div className="topbar__meta">
        <span>
          <span className={"dot" + (connected ? " on" : "")} /> {connected ? "已连接" : "未连接"}
        </span>
        {config && config.models.length > 1 ? (
          <select
            className="topbar__model"
            value={activeModelId}
            onChange={(e) => onModelChange(e.target.value)}
            title="选择模型"
          >
            {config.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {m.model}{m.has_key ? "" : " · 演示"}
              </option>
            ))}
          </select>
        ) : config && (
          <span>{config.has_key ? config.model : `${config.model} · 演示模式`}</span>
        )}
      </div>
      <button
        className={"icon-btn" + (boardOpen ? " active" : "")}
        title="任务板"
        onClick={onToggleBoard}
      >
        ▦
      </button>
    </div>
  );
}
