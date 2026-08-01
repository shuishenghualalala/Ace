import { useState } from "react";

interface Props {
  content: string;
}

export default function ThinkingBlock({ content }: Props) {
  const [open, setOpen] = useState(true);

  return (
    <div className="thinking-block">
      <button
        className="thinking-block__header"
        onClick={() => setOpen(!open)}
        type="button"
      >
        <span className="thinking-block__icon">💭</span>
        <span className="thinking-block__title">思考过程</span>
        <span className={`thinking-block__caret ${open ? "thinking-block__caret--open" : ""}`}>
          ›
        </span>
      </button>
      {open && (
        <div className="thinking-block__body">
          {content}
        </div>
      )}
    </div>
  );
}
