import { useEffect, useState } from "react";
import type { Workspace } from "../types";

interface Props {
  initial: Workspace | null; // null = 新建
  onClose: () => void;
  onSubmit: (fields: { name: string; description: string; instructions: string }) => void;
}

export default function WorkspaceModal({ initial, onClose, onSubmit }: Props) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");

  useEffect(() => {
    setName(initial?.name ?? "");
    setDescription(initial?.description ?? "");
    setInstructions(initial?.instructions ?? "");
  }, [initial]);

  const submit = () => {
    if (!name.trim()) return;
    onSubmit({ name: name.trim(), description: description.trim(), instructions });
  };

  return (
    <div className="modal__mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__title">{initial ? "编辑工作空间" : "新建工作空间"}</div>
        <label className="field">
          <span>名称</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：电商后台" autoFocus />
        </label>
        <label className="field">
          <span>描述</span>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="一句话说明（可选）"
          />
        </label>
        <label className="field">
          <span>空间指令</span>
          <textarea
            rows={4}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="注入该空间所有会话的系统提示，如：技术栈用 TypeScript，回答用中文。"
          />
        </label>
        <div className="modal__foot">
          <button className="btn-ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn-solid" onClick={submit}>
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
