import { useState } from "react";
import type { PlanDecisionHandler, PlanDecisionNotice, PlanReview } from "../types";
import AgentAvatarLogo from "./AgentAvatarLogo";
import MarkdownContent from "./MarkdownContent";

interface Props {
  review: PlanReview;
  onDecision?: PlanDecisionHandler;
  embedded?: boolean;
  defaultOpen?: boolean;
}

/** Plan 模式：模型写完计划、等待审批时展示计划全文 + 批准/继续修改按钮。 */
export default function PlanReviewPanel({ review, onDecision, embedded = false }: Props) {
  const card = <PlanReviewCard review={review} onDecision={onDecision} />;
  if (embedded) return card;
  return (
    <div className="msg plan-review-msg">
      <div className="msg__avatar bot">
        <AgentAvatarLogo />
      </div>
      <div className="msg__body">
        <div className="msg__name">Crew</div>
        {card}
      </div>
    </div>
  );
}

const notices: Record<PlanDecisionNotice, string> = {
  confirming: "正在确认计划操作…",
  uncertain: "尚未收到确认，可以查询此次操作的结果。",
  unavailable: "暂时无法提交，请在连接恢复后重试。",
  expired: "这份计划已变更，请重新审阅。",
  rejected: "服务端未接受此次操作。",
  unknown: "服务端没有此次提交记录，可以重新提交。",
};

export function PlanReviewCard({ review, onDecision, defaultOpen = true }: Omit<Props, "embedded">) {
  const [open, setOpen] = useState(defaultOpen);
  const editing = review.status === "editing";
  const readonly = review.status === "readonly";
  const rejected = review.status === "rejected";
  const cancelled = review.status === "cancelled";
  const approved = review.status === "approved";
  const empty = review.status === "empty" || review.empty === true;
  const title = empty ? "计划为空" : firstHeading(review.plan) || "待审批的计划";

  return (
    <div className={"plan-review" + (open ? " plan-review--open" : "")}>
      <button
        className="plan-review__header"
        onClick={() => setOpen((v) => !v)}
        type="button"
        aria-expanded={open}
      >
        <span className="plan-review__icon">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 19.5V4a2 2 0 0 1 2-2h11l3 3v14.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z"/>
            <path d="M14 2v6h6"/>
            <path d="M8 13h8"/>
            <path d="M8 17h5"/>
          </svg>
        </span>
        <span className="plan-review__title">{title}</span>
        <span className="plan-review__meta">
          {empty
            ? "未写入计划"
            : rejected
              ? "已拒绝"
              : cancelled
                ? "已取消"
                : approved
                  ? "已批准"
                  : readonly
                    ? "历史计划"
                    : editing || review.status === "revising"
                      ? "继续修改中"
                      : "等待审批"}
        </span>
        <span className={"plan-review__caret" + (open ? " plan-review__caret--open" : "")}>›</span>
      </button>
      {open && (
        <div className="plan-review__body">
          {review.planFile && <div className="plan-review__file">{review.planFile}</div>}
          <div className="plan-review__content md-body">
            <MarkdownContent content={review.plan || "(计划为空)"} />
          </div>
          <div className="plan-review__actions">
            {review.decisionNotice && <span role="status">{notices[review.decisionNotice]}</span>}
            {review.decisionNotice === "uncertain" ? (
              <button className="plan-review__btn" type="button" onClick={() => onDecision?.("check", review)}>查询结果</button>
            ) : review.decisionNotice === "confirming" ? null : empty ? (
              <span className="plan-review__note">
                模型未写入计划文件。请在对话框要求模型先用 file_write 把计划写入上面的路径，再调用 exit_plan_mode。
              </span>
            ) : readonly || approved || rejected || cancelled ? (
              <span className="plan-review__note">历史计划只读展示</span>
            ) : editing || review.status === "revising" ? (
              <span className="plan-review__note">在对话框输入修改建议</span>
            ) : (
              <>
                <button className="plan-review__btn" type="button" onClick={() => onDecision?.("plan_reject", review)}>
                  继续修改
                </button>
                <button className="plan-review__btn plan-review__btn--primary" type="button" onClick={() => onDecision?.("plan_approve", review)}>
                  批准并执行
                </button>
                <button className="plan-review__btn" type="button" onClick={() => onDecision?.("plan_reject_and_exit", review)}>
                  拒绝并退出
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function firstHeading(markdown: string): string | null {
  const line = markdown
    .split("\n")
    .map((part) => part.trim())
    .find((part) => part.length > 0);
  if (!line) return null;
  return line.replace(/^#{1,6}\s+/, "").slice(0, 90);
}
