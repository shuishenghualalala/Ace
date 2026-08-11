import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AgentsHub, {
  agentsGuideStepDefinition,
  buildTeamConstraintText,
  calculateAgentsGuideTooltipPosition,
  decideTeamDescriptionDraftRequest,
  formatLlmElapsed,
  formatTeamDraftElapsed,
  resolveFormationUiStatus,
} from "./AgentsHub";
import { externalAgentInitial, externalAgentTone } from "./ExternalAgentAvatar";

describe("external helper language", () => {
  it("presents the runtime and agent flow in user-facing terms", () => {
    const html = renderToStaticMarkup(createElement(AgentsHub, {
      onAssignAgent: () => {},
      onAssignTeam: () => {},
      onStartLeaderChat: () => {},
    }));

    expect(html).toContain("<h1>外援中心</h1>");
    expect(html).toContain("Crew 会找到本机能帮上忙的 AI 工具");
    expect(html).toContain(">我的阵容</button>");
    expect(html).toContain(">发现外援</button>");
    expect(html).toContain(">添加外援</button>");
    expect(html).toContain(">组建团队</button>");
    expect(html).not.toContain("<h1>异构智能体</h1>");
  });
});

describe("external agent avatar identity", () => {
  it("uses the existing display badge rule and falls back to the provider initial", () => {
    expect(externalAgentInitial({ provider: "kimi", display_badge: "" })).toBe("K");
    expect(externalAgentInitial({ provider: "codex", display_badge: "X" })).toBe("X");
    expect(externalAgentInitial({ provider: "claude-code" })).toBe("C");
    expect(externalAgentTone("kimi")).not.toBe(externalAgentTone("codex"));
    expect(externalAgentTone("kimi")).toBe(externalAgentTone("KIMI"));
  });
});

describe("agentsGuideStepDefinition", () => {
  it("keeps the guide independent from runtime state and points each step at one action", () => {
    expect(agentsGuideStepDefinition(1, false)).toMatchObject({
      progress: "1/3",
      target: '[data-agents-guide-target="scan"]',
      side: "left",
    });
    expect(agentsGuideStepDefinition(2, true)).toMatchObject({
      progress: "2/3",
      target: '[data-agents-guide-target="runtime-select"]',
      side: "right",
    });
    expect(agentsGuideStepDefinition(3, true)).toMatchObject({
      progress: "3/3",
      target: '[data-agents-guide-target="assign"]',
    });
    expect(agentsGuideStepDefinition(3, false)).toMatchObject({
      progress: "3/3",
      target: '[data-agents-tab="create-agent"]',
    });
  });
});

describe("calculateAgentsGuideTooltipPosition", () => {
  const viewport = { width: 1200, height: 800 };
  const tooltip = { width: 332, height: 176 };

  it("places the bubble beside the target on the preferred side", () => {
    expect(calculateAgentsGuideTooltipPosition(
      { left: 700, right: 900, top: 240, bottom: 300, width: 200, height: 60 },
      tooltip,
      viewport,
      "left",
    )).toEqual({ left: 356, top: 182 });
  });

  it("keeps the bubble inside a narrow viewport", () => {
    const result = calculateAgentsGuideTooltipPosition(
      { left: 180, right: 260, top: 620, bottom: 680, width: 80, height: 60 },
      { width: 300, height: 176 },
      { width: 420, height: 720 },
      "right",
    );
    expect(result.left).toBeGreaterThanOrEqual(12);
    expect(result.left).toBeLessThanOrEqual(108);
    expect(result.top).toBeGreaterThanOrEqual(12);
    expect(result.top).toBeLessThanOrEqual(532);
  });
});

describe("formatTeamDraftElapsed", () => {
  it("formats live description generation time in seconds and minutes", () => {
    expect(formatTeamDraftElapsed(0)).toBe("0s");
    expect(formatTeamDraftElapsed(9_800)).toBe("9s");
    expect(formatTeamDraftElapsed(60_000)).toBe("1min");
    expect(formatTeamDraftElapsed(73_000)).toBe("1min 13s");
    expect(formatLlmElapsed(3_240)).toBe("3.2s");
  });
});

describe("resolveFormationUiStatus", () => {
  it("distinguishes improved, unchanged and partial AI checks without exposing backend modes", () => {
    expect(resolveFormationUiStatus({
      requested_formation_mode: "auto",
      selected_formation_mode: "ai",
      fallback_reason: "",
      ai_material_improvements: ["补充分工"],
    })).toBe("ready_improved");
    expect(resolveFormationUiStatus({
      requested_formation_mode: "auto",
      selected_formation_mode: "fast",
      fallback_reason: "no_material_improvement",
      ai_material_improvements: [],
    })).toBe("ready_unchanged");
    expect(resolveFormationUiStatus({
      requested_formation_mode: "auto",
      selected_formation_mode: "fast",
      fallback_reason: "provider_error",
      ai_material_improvements: [],
    })).toBe("ready_partial");
    expect(resolveFormationUiStatus({
      requested_formation_mode: "auto",
      selected_formation_mode: "fast",
      fallback_reason: "",
      ai_material_improvements: [],
    })).toBe("ready_unchanged");
  });
});

describe("buildTeamConstraintText", () => {
  it("turns included agents, excluded agents and capabilities into formation constraints", () => {
    expect(buildTeamConstraintText({
      requiredAgentNames: ["Kimi"],
      excludedAgentNames: ["Hermes"],
      requiredCapabilities: ["information_retrieval", "verification"],
      customCapabilities: ["数据分析"],
    })).toBe([
      "Kimi 必须作为成员加入团队。",
      "不要让 Hermes 加入团队。",
      "必须包含信息检索能力。",
      "必须包含核验复核能力。",
      "团队还必须具备「数据分析」能力。",
    ].join("\n"));
  });

  it("ignores unknown capabilities", () => {
    expect(buildTeamConstraintText({
      requiredAgentNames: [],
      excludedAgentNames: [],
      requiredCapabilities: ["unknown"],
      customCapabilities: [],
    })).toBe("");
  });
});

describe("decideTeamDescriptionDraftRequest", () => {
  it("regenerates the description after the team is renamed", () => {
    expect(decideTeamDescriptionDraftRequest({
      name: "新团队",
      description: "用户改过的旧描述",
      generatedDescription: "",
      lastDescriptionName: "旧团队",
      lastDraftKey: "旧团队",
    })).toMatchObject({ shouldRequest: true, regenerateDescription: true });
  });

  it("does not restart the same draft after a manual edit", () => {
    expect(decideTeamDescriptionDraftRequest({
      name: "测试团队",
      description: "用户刚刚开始编辑",
      generatedDescription: "",
      lastDescriptionName: "测试团队",
      lastDraftKey: "测试团队",
    })).toMatchObject({ shouldRequest: false, shouldInvalidate: false });
  });

  it("does not restart after a streamed description update", () => {
    expect(decideTeamDescriptionDraftRequest({
      name: "测试团队",
      description: "LLM 已生成的描述",
      generatedDescription: "LLM 已生成的描述",
      lastDescriptionName: "测试团队",
      lastDraftKey: "测试团队",
    })).toMatchObject({ shouldRequest: false, shouldInvalidate: false });
  });
});
