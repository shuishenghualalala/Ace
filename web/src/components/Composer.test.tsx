import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import Composer from "./Composer";
import ExternalAgentAvatar from "./ExternalAgentAvatar";
import type { AppConfig } from "../types";

const noop = () => {};
const config = (externalAgentsEnabled: boolean): AppConfig => ({
  model: "test",
  has_key: true,
  base_url: "",
  active_model_id: "test",
  models: [],
  wiki: { enabled: true },
  external_agents: { enabled: externalAgentsEnabled },
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

  it("renders the bottom Mode picker with auto selected for Team sessions", () => {
    const html = renderComposer(true);
    expect(html).toContain("team-mode-picker");
    expect(html).toContain(">Mode</span>");
    expect(html).toContain(">auto</span>");
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
