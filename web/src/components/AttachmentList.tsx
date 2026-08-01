import type { Attachment } from "../types";

interface Props {
  attachments: Attachment[];
  onRemove: (id: string) => void;
  /** 是否紧凑模式（消息气泡内嵌时隐藏图像预览） */
  compact?: boolean;
}

const ICON_MAP: Record<string, string> = {
  file: "📄",
  image: "🖼️",
  url: "🔗",
};

/** 获取图像预览 URL：优先 previewUrl（后端上传返回），其次本地 Object URL */
function getImageUrl(att: Attachment): string | null {
  if (att.type !== "image") return null;
  if (att.previewUrl) return att.previewUrl;
  // 本地粘贴/拖拽的图像可能有 data URL 形式的 content
  if (att.content && att.content.startsWith("data:image/")) return att.content;
  return null;
}

export default function AttachmentList({ attachments, onRemove, compact }: Props) {
  if (attachments.length === 0) return null;

  const images = attachments.filter((a) => getImageUrl(a));
  const files = attachments.filter((a) => !getImageUrl(a));

  return (
    <div className="att-list">
      {/* 图像缩略图预览 */}
      {!compact && images.length > 0 && (
        <div className="att-images">
          {images.map((att) => (
            <div key={att.id} className="att-image-wrap">
              <img
                className="att-image-thumb"
                src={getImageUrl(att)!}
                alt={att.name}
                title={att.name}
              />
              <button
                className="att-image-remove"
                onClick={() => onRemove(att.id)}
                type="button"
                title="移除"
              >
                ×
              </button>
              <span className="att-image-name">{att.name}</span>
            </div>
          ))}
        </div>
      )}
      {/* 紧凑模式或无预览时，图像仍作为 pill 显示 */}
      {compact && images.length > 0 && images.map((att) => (
        <div key={att.id} className="att-pill">
          <span className="att-pill__icon">🖼️</span>
          <span className="att-pill__name">{att.name}</span>
          <button
            className="att-pill__remove"
            onClick={() => onRemove(att.id)}
            type="button"
            title="移除"
          >
            ×
          </button>
        </div>
      ))}
      {/* 非图像附件 */}
      {files.map((att) => (
        <div key={att.id} className="att-pill">
          <span className="att-pill__icon">{ICON_MAP[att.type] || "📄"}</span>
          <span className="att-pill__name">{att.name}</span>
          <button
            className="att-pill__remove"
            onClick={() => onRemove(att.id)}
            type="button"
            title="移除"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
