import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PlanReviewCard } from "./PlanReviewPanel";
import type { PlanReview } from "../types";

const noop = () => {};

describe("PlanReviewCard", () => {
  it("默认展开：渲染计划正文与审批按钮，用户可直接阅读", () => {
    const review: PlanReview = {
      plan: "# 我的计划\n做事情",
      planFile: "/tmp/plan.md",
      status: "pending",
    };
    const html = renderToStaticMarkup(
      <PlanReviewCard review={review} onApprove={noop} onReject={noop} />,
    );
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain("plan-review--open");
    expect(html).toContain("我的计划");
    expect(html).toContain("等待审批");
    expect(html).toContain("做事情");
    expect(html).toContain("批准并执行");
    expect(html).toContain("继续修改");
    expect(html).toContain("拒绝并退出");
  });

  it("empty 计划：不渲染审批按钮，显示「计划为空」提示", () => {
    const review: PlanReview = {
      plan: "",
      planFile: "/tmp/plan.md",
      status: "empty",
      empty: true,
    };
    const html = renderToStaticMarkup(
      <PlanReviewCard review={review} onApprove={noop} onReject={noop} defaultOpen />,
    );
    // 无审批按钮
    expect(html).not.toContain("批准并执行");
    expect(html).not.toContain("继续修改");
    // 有空计划提示 + 引导文案
    expect(html).toContain("计划为空");
    expect(html).toContain("未写入计划");
    expect(html).toContain("file_write");
  });

  it("empty 标记经 empty=true 字段触发（即使 status 未显式设为 empty）", () => {
    const review: PlanReview = {
      plan: "",
      planFile: "/tmp/plan.md",
      status: "pending",
      empty: true,
    };
    const html = renderToStaticMarkup(
      <PlanReviewCard review={review} onApprove={noop} onReject={noop} defaultOpen />,
    );
    expect(html).not.toContain("批准并执行");
    expect(html).toContain("计划为空");
  });

  it("readonly 历史计划：只读，无按钮", () => {
    const review: PlanReview = {
      plan: "# 旧计划",
      planFile: "/tmp/plan.md",
      status: "readonly",
    };
    const html = renderToStaticMarkup(
      <PlanReviewCard review={review} onApprove={noop} onReject={noop} defaultOpen />,
    );
    expect(html).not.toContain("批准并执行");
    expect(html).toContain("历史计划");
  });
});
