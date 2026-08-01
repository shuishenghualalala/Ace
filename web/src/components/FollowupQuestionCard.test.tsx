import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { FollowupQuestion } from "../types";
import FollowupQuestionCard from "./FollowupQuestionCard";

describe("FollowupQuestionCard permission presentation", () => {
  it("does not offer arbitrary custom text for a permission decision", () => {
    const question: FollowupQuestion = {
      question_id: "permission-1",
      title: "权限确认 · browser_use",
      record_history: false,
      origin: { type: "team_control", agent_name: "产品开发团队" },
      questions: [{
        id: "perm",
        question: (
          "即将执行：修改文件\n" +
          "目标：/workspace/game.js\n\n" +
          "原因：目标位于工作区外。\n" +
          "工具调用：tool_internal_only"
        ),
        options: [
          { label: "允许一次", value: "allow_once" },
          { label: "拒绝", value: "deny" },
        ],
        allowFreeText: false,
        multiSelect: false,
      }],
    };

    const html = renderToStaticMarkup(
      <FollowupQuestionCard question={question} onSubmit={() => undefined} />,
    );
    expect(html).toContain("允许一次");
    expect(html).toContain("/workspace/game.js");
    expect(html).not.toContain("其他（自定义输入）");
    expect(html).not.toContain("tool_internal_only");
    expect(html).not.toContain('type="radio"');
    expect(html).not.toContain(">提交<");
  });

  it("keeps the built-in tool permission card unchanged", () => {
    const question: FollowupQuestion = {
      question_id: "permission-built-in",
      title: "权限确认 · terminal",
      record_history: false,
      questions: [{
        id: "perm",
        question: "即将执行：terminal(echo ok)\n\n原因：命令需要确认",
        options: [
          { label: "允许一次", value: "allow_once" },
          { label: "始终允许", value: "always" },
          { label: "拒绝", value: "deny" },
        ],
        allowFreeText: false,
        multiSelect: false,
      }],
    };

    const html = renderToStaticMarkup(
      <FollowupQuestionCard question={question} onSubmit={() => undefined} />,
    );

    expect(html).toContain("允许一次");
    expect(html).toContain("始终允许");
    expect(html).toContain("拒绝");
    expect(html).toContain('type="radio"');
    expect(html).toContain(">提交<");
  });
});
