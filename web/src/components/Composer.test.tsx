/** @vitest-environment happy-dom */

import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import Composer, { formatTeamMentionToken, teamMemberMentionId } from "./Composer";
import ExternalAgentAvatar from "./ExternalAgentAvatar";
import { EXTERNAL_AGENTS_FEATURE_ID } from "../lib/featureFlags";
import type { AppConfig } from "../types";

const noop = () => {};
const config = (externalAgentsEnabled: boolean): AppConfig => ({
  model: "test",
  has_key: true,
  base_url: "",
  active_model_id: "test",
  models: [],
  wiki: { enabled: true },
  feature_capabilities: {
    [EXTERNAL_AGENTS_FEATURE_ID]: {
      state: externalAgentsEnabled ? "enabled" : "disabled",
      available: externalAgentsEnabled,
      generation: null,
    },
  },
});

describe("Composer team execution tier picker", () => {
  const renderComposer = (isTeamSession = false) => renderToStaticMarkup(
    <Composer
      config={null}
      busy={false}
      attachments={[]}
      onSend={noop}
      onAttachmentsChange={noop}
      isTeamSession={isTeamSession}
    />,
  );

  it("renders the execution mode picker with 自动判断 selected for Team sessions", () => {
    const html = renderComposer(true);
    expect(html).toContain("team-mode-picker");
    expect(html).toContain(">处理方式</span>");
    expect(html).toContain(">自动判断</span>");
  });

  it("does not render the Team Mode picker for single-Agent sessions", () => {
    const html = renderComposer();
    expect(html).not.toContain("team-mode-picker");
    expect(html).not.toContain(">Mode<");
  });

  it("uses a neutral label before a model is configured", () => {
    const html = renderComposer();
    expect(html).toContain(">未配置模型</span>");
    expect(html).not.toContain("Infini-AI GLM-5.1");
  });

  it("only hides the 外援 entry when the Gateway explicitly disables it", () => {
    const disabledHtml = renderToStaticMarkup(
      <Composer
        config={config(false)}
        busy={false}
        attachments={[]}
        onSend={noop}
        onAttachmentsChange={noop}
      />,
    );
    const enabledHtml = renderToStaticMarkup(
      <Composer
        config={config(true)}
        busy={false}
        attachments={[]}
        onSend={noop}
        onAttachmentsChange={noop}
      />,
    );
    const pendingHtml = renderToStaticMarkup(
      <Composer
        config={null}
        busy={false}
        attachments={[]}
        onSend={noop}
        onAttachmentsChange={noop}
      />,
    );

    expect(disabledHtml).not.toContain("agents-picker");
    expect(enabledHtml).toContain("agents-picker");
    expect(pendingHtml).toContain("agents-picker");
    expect(enabledHtml).toContain(">外援</button>");
    expect(enabledHtml).toContain("external-agent-avatar");
    expect(enabledHtml).not.toContain(">Agents</button>");
  });
});

describe("Composer team member mention display", () => {
  it("uses the stable Team Runtime member id instead of the display name", () => {
    expect(teamMemberMentionId({
      agentId: "agent_c6f06632e6a4",
      isLeader: false,
    })).toBe("agent_c6f06632e6a4");
    expect(teamMemberMentionId({
      agentId: "agent_leader",
      isLeader: true,
    })).toBe("leader");
  });

  it("uses the member name instead of the internal agent id", () => {
    expect(formatTeamMentionToken({ agentId: "agent_c6f06632e6a4", name: "kk" })).toBe("@kk");
  });

  it("keeps spaced names readable", () => {
    expect(formatTeamMentionToken({ agentId: "crew-builtin", name: " Crew   内置智能体 " }))
      .toBe("@Crew 内置智能体");
  });
});

describe("Composer slash input", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function mount(props: Partial<ComponentProps<typeof Composer>> = {}) {
    await act(async () => root.render(
      <Composer config={null} busy={false} attachments={[]} onSend={noop} onAttachmentsChange={noop} {...props} />,
    ));
    return container.querySelector("textarea")!;
  }

  async function type(input: HTMLTextAreaElement, value: string) {
    await act(async () => {
      input.focus();
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input, value);
      input.setSelectionRange(value.length, value.length);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  function press(input: HTMLTextAreaElement, key: string) {
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })));
  }

  it("offers the Plan command before the skill catalog arrives and executes without a chat turn", async () => {
    let deliver!: (value: []) => void;
    const pending = new Promise<[]>((resolve) => { deliver = resolve; });
    const loadSkills = vi.spyOn(api, "skills").mockReturnValue(pending);
    const onEnterPlan = vi.fn();
    const onSend = vi.fn();
    const input = await mount({ onEnterPlan, onSend });
    await type(input, "/");
    await type(input, "/pl");
    expect(container.querySelector('[role="option"]')?.textContent).toContain("/plan");
    expect(loadSkills).toHaveBeenCalledTimes(1);
    press(input, "Enter");
    expect(onEnterPlan).toHaveBeenCalledTimes(1);
    expect(onSend).not.toHaveBeenCalled();
    expect(input.value).toBe("");
    await act(async () => deliver([]));
    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it("moves the keyboard selection and reuses the New Session action", async () => {
    vi.spyOn(api, "skills").mockResolvedValue([]);
    const onEnterPlan = vi.fn();
    const onNewSession = vi.fn();
    const input = await mount({ onEnterPlan, onNewSession });
    await type(input, "/");
    press(input, "ArrowDown");
    expect(container.querySelector('[aria-selected="true"]')?.textContent).toContain("/new");
    press(input, "Enter");
    expect(onNewSession).toHaveBeenCalledTimes(1);
    expect(onEnterPlan).not.toHaveBeenCalled();
  });

  it("distinguishes skills from commands and only fills the input when a skill is selected", async () => {
    vi.spyOn(api, "skills").mockResolvedValue([{
      slug: "web-gui-tester", name: "Web GUI", description: "Test a web page", category: "testing",
      source: "builtin", featured: false,
    }]);
    const onSend = vi.fn();
    const input = await mount({ onEnterPlan: noop, onSend });
    await type(input, "/");
    const menu = container.querySelector('[role="listbox"]')!;
    expect(menu.textContent).toContain("命令");
    expect(menu.textContent).toContain("技能");
    const skill = menu.querySelector<HTMLButtonElement>('button[aria-label^="技能 /web-gui-tester"]')!;
    act(() => skill.click());
    expect(onSend).not.toHaveBeenCalled();
    expect(input.value).toBe("/web-gui-tester ");
    expect(document.activeElement).toBe(input);
  });

  it("opens a side chat for either alias without submitting to the main conversation", async () => {
    vi.spyOn(api, "skills").mockResolvedValue([]);
    const onOpenSideChat = vi.fn();
    const onSend = vi.fn();
    const input = await mount({ onOpenSideChat, onSend });
    await type(input, "/si");
    expect(container.querySelector('[role="option"]')?.textContent).toContain("/side");
    press(input, "Enter");
    expect(onOpenSideChat).toHaveBeenCalledTimes(1);
    expect(input.value).toBe("");
    await type(input, "/btw");
    press(input, "Escape");
    press(input, "Enter");
    expect(onOpenSideChat).toHaveBeenCalledTimes(2);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not interpret a slash token as a command when it has attachments", async () => {
    const attachments = [{ id: "f", name: "fixture.txt", path: "fixture.txt", type: "file" as const }];
    const onSend = vi.fn();
    const onEnterPlan = vi.fn();
    const input = await mount({ attachments, onSend, onEnterPlan });
    await type(input, "/plan");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    press(input, "Enter");
    expect(onEnterPlan).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/plan", attachments, undefined, undefined);
  });

  it("keeps a scenario-bound slash token in the regular message path", async () => {
    const onSend = vi.fn();
    const onEnterPlan = vi.fn();
    const input = await mount({ onSend, onEnterPlan, subScenario: "review" });
    await type(input, "/plan");
    press(input, "Enter");
    expect(onEnterPlan).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/plan", [], "review", undefined);
  });
});

describe("shared external-agent avatar", () => {
  it("keeps the external-agent mark above the provider badge", () => {
    const html = renderToStaticMarkup(
      <ExternalAgentAvatar agent={{ provider: "kimi", display_badge: "K" }} size="compact" />,
    );

    expect(html).not.toContain("external-agent.png");
    expect(html).toContain(">K</span>");
    expect(html).toContain("external-agent-avatar--compact");
  });
});
