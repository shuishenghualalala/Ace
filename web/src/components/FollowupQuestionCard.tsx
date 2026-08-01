import { useState } from "react";
import type { FollowupQuestion } from "../types";

interface Props {
  question: FollowupQuestion;
  onSubmit: (answers: { question_id: string; answers: string[] }[]) => boolean | void;
  onDismiss?: () => void;
}

const FREE_TEXT_OPTION = "__free_text__";

function permissionPresentation(text: string) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const valueOf = (prefix: string) => (
    lines.find((line) => line.startsWith(prefix))?.slice(prefix.length).trim() ?? ""
  );
  const action = valueOf("即将执行：") || "执行受控操作";
  const target = valueOf("目标：");
  const reason = valueOf("原因：");
  const context = lines.filter((line) => line.startsWith("成员：") || line.startsWith("节点："));
  const title = action.includes("修改文件")
    ? "允许修改此文件？"
    : action.includes("读取文件")
      ? "允许读取此文件？"
      : action.includes("执行命令")
        ? "允许执行此命令？"
        : "允许执行此操作？";
  return { action, target, reason, context, title };
}

export default function FollowupQuestionCard({ question, onSubmit, onDismiss }: Props) {
  // 每个问题当前选中的选项；answers[questionId] = 选项值数组
  const [answers, setAnswers] = useState<Record<string, string[]>>(() => {
    const init: Record<string, string[]> = {};
    for (const q of question.questions) {
      init[q.id] = [];
    }
    return init;
  });
  // 每个问题的自定义输入文本
  const [freeText, setFreeText] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const q of question.questions) {
      init[q.id] = "";
    }
    return init;
  });
  const [submitted, setSubmitted] = useState(false);
  const isPermission = question.record_history === false
    && ["acp_permission", "team_control"].includes(question.origin?.type ?? "");

  if (isPermission) {
    const item = question.questions[0];
    const presentation = permissionPresentation(item?.question ?? "");
    if (submitted) {
      return (
        <div className="followup-card followup-card--permission followup-card--submitted" role="status">
          <div className="followup-card__title">{presentation.title}</div>
          <div className="followup-card__note">正在确认您的选择…</div>
        </div>
      );
    }
    return (
      <div
        className="followup-card followup-card--permission"
        role="alertdialog"
        aria-modal="true"
        aria-label={presentation.title}
      >
        <div className="permission-card__header">
          <span className="permission-card__icon" aria-hidden="true">✓</span>
          <div>
            <div className="followup-card__title">{presentation.title}</div>
            <div className="permission-card__subtitle">Crew 请求执行以下操作</div>
          </div>
        </div>
        <div className="permission-card__operation">
          <span className="permission-card__action">{presentation.action}</span>
          {presentation.target && (
            <code className="permission-card__target">{presentation.target}</code>
          )}
          {presentation.context.map((line) => (
            <span className="permission-card__context" key={line}>{line}</span>
          ))}
        </div>
        {presentation.reason && (
          <div className="permission-card__reason">{presentation.reason}</div>
        )}
        <div className="permission-card__actions">
          {[...(item?.options ?? [])].sort((left, right) => (
            Number(left.value === "allow_once") - Number(right.value === "allow_once")
          )).map((option) => {
            const allow = option.value === "allow_once";
            return (
              <button
                key={option.value}
                type="button"
                className={allow ? "permission-card__allow" : "permission-card__deny"}
                onClick={() => {
                  const sent = onSubmit([{ question_id: item.id, answers: [option.value] }]);
                  if (sent !== false) setSubmitted(true);
                }}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  const toggleOption = (qid: string, option: string, multi: boolean) => {
    setAnswers((prev) => {
      const current = new Set(prev[qid] ?? []);
      if (multi) {
        if (current.has(option)) {
          current.delete(option);
        } else {
          // 取消互斥的自定义输入选项
          current.delete(FREE_TEXT_OPTION);
          current.add(option);
        }
      } else {
        current.clear();
        current.add(option);
      }
      return { ...prev, [qid]: Array.from(current) };
    });
  };

  const toggleFreeText = (qid: string, multi: boolean) => {
    setAnswers((prev) => {
      const current = new Set(prev[qid] ?? []);
      if (current.has(FREE_TEXT_OPTION)) {
        current.delete(FREE_TEXT_OPTION);
      } else {
        if (!multi) current.clear();
        // 自定义输入与普通选项互斥（单选时）；多选时保留已选普通选项
        if (!multi) {
          for (const opt of current) {
            if (opt !== FREE_TEXT_OPTION) current.delete(opt);
          }
        }
        current.add(FREE_TEXT_OPTION);
      }
      return { ...prev, [qid]: Array.from(current) };
    });
  };

  const handleSubmit = () => {
    const result: { question_id: string; answers: string[] }[] = [];
    for (const q of question.questions) {
      const textMode = q.inputMode === "text" || q.options.length === 0;
      if (textMode) {
        const text = freeText[q.id]?.trim();
        result.push({ question_id: q.id, answers: text ? [text] : [] });
        continue;
      }
      const selected = answers[q.id] ?? [];
      const texts: string[] = [];
      for (const opt of selected) {
        if (opt === FREE_TEXT_OPTION) {
          const custom = freeText[q.id]?.trim();
          if (custom) texts.push(custom);
        } else {
          texts.push(opt);
        }
      }
      result.push({ question_id: q.id, answers: texts });
    }
    setSubmitted(true);
    onSubmit(result);
  };

  const canSubmit = () => {
    for (const q of question.questions) {
      const textMode = q.inputMode === "text" || q.options.length === 0;
      if (textMode) {
        if (!freeText[q.id]?.trim()) return false;
        continue;
      }
      const selected = answers[q.id] ?? [];
      if (selected.length === 0) return false;
      // 如果选了自定义输入但未填内容，不允许提交
      if (selected.includes(FREE_TEXT_OPTION) && !freeText[q.id]?.trim()) return false;
    }
    return true;
  };

  if (submitted) {
    return (
      <div className="followup-card followup-card--submitted">
        <div className="followup-card__title">{question.title || "等待确认"}</div>
        <div className="followup-card__note">选择已提交，等待 AI 继续…</div>
      </div>
    );
  }

  return (
    <div className="followup-card">
      {question.title && <div className="followup-card__title">{question.title}</div>}
      {question.questions.map((q) => {
        const selected = answers[q.id] ?? [];
        const textMode = q.inputMode === "text" || q.options.length === 0;
        const isFreeTextSelected = selected.includes(FREE_TEXT_OPTION);
        const inputType = q.multiSelect ? "checkbox" : "radio";
        return (
          <div key={q.id} className="followup-card__question">
            <div className="followup-card__qtext">{q.question}</div>
            {textMode ? (
              <textarea
                className="followup-card__free-input followup-card__free-input--text"
                placeholder="请输入补充信息..."
                value={freeText[q.id] ?? ""}
                onChange={(e) => setFreeText((prev) => ({ ...prev, [q.id]: e.target.value }))}
                rows={3}
              />
            ) : (
              <div className="followup-card__options">
                {q.options.map((opt) => {
                  const checked = selected.includes(opt.value);
                  return (
                    <label
                      key={opt.value}
                      className={`followup-card__option ${checked ? "followup-card__option--checked" : ""}`}
                    >
                      <input
                        type={inputType}
                        name={`followup_${q.id}`}
                        checked={checked}
                        onChange={() => toggleOption(q.id, opt.value, q.multiSelect)}
                      />
                      <span className="followup-card__option-copy">
                        <span className="followup-card__option-label">{opt.label}</span>
                        {opt.description && (
                          <span className="followup-card__option-description">：{opt.description}</span>
                        )}
                      </span>
                    </label>
                  );
                })}
                {q.allowFreeText !== false && (
                  <label
                    className={`followup-card__option followup-card__option--free ${
                      isFreeTextSelected ? "followup-card__option--checked" : ""
                    }`}
                  >
                    <input
                      type={inputType}
                      name={`followup_${q.id}`}
                      checked={isFreeTextSelected}
                      onChange={() => toggleFreeText(q.id, q.multiSelect)}
                    />
                    <span>其他（自定义输入）</span>
                  </label>
                )}
                {q.allowFreeText !== false && isFreeTextSelected && (
                  <input
                    type="text"
                    className="followup-card__free-input"
                    placeholder="请输入你的回答…"
                    value={freeText[q.id] ?? ""}
                    onChange={(e) => setFreeText((prev) => ({ ...prev, [q.id]: e.target.value }))}
                  />
                )}
              </div>
            )}
          </div>
        );
      })}
      <div className="followup-card__actions">
        <button
          className="followup-card__submit"
          onClick={handleSubmit}
          disabled={!canSubmit()}
          type="button"
        >
          提交
        </button>
        {onDismiss && (
          <button className="followup-card__dismiss" onClick={onDismiss} type="button">
            取消
          </button>
        )}
      </div>
    </div>
  );
}
