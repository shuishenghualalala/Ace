import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import Sidebar from "./Sidebar";

const noop = () => {};

function renderSidebar(externalAgentsEnabled: boolean): string {
  return renderToStaticMarkup(
    <Sidebar
      workspaces={[{ id: "default", name: "默认", description: "", instructions: "" }]}
      sessions={[]}
      currentSessionId=""
      sessionStatus={{}}
      expanded={new Set(["default"])}
      view="chat"
      externalAgentsEnabled={externalAgentsEnabled}
      onViewChange={noop}
      onToggleExpand={noop}
      onNewWorkspace={noop}
      onEditWorkspace={noop}
      onDeleteWorkspace={noop}
      onNewSession={noop}
      onSelectSession={noop}
      onRenameSession={noop}
      onDeleteSession={noop}
    />,
  );
}

function renderExternalSession(): string {
  return renderToStaticMarkup(
    <Sidebar
      workspaces={[{ id: "default", name: "默认", description: "", instructions: "" }]}
      sessions={[{
        session_id: "s-kimi",
        workspace_id: "default",
        title: "接口联调",
        message_count: 0,
        created_at: 1,
        updated_at: 1,
        agent_label: { name: "kimi", provider: "kimi", display_badge: "K" },
      }]}
      currentSessionId=""
      sessionStatus={{}}
      expanded={new Set(["default"])}
      view="chat"
      externalAgentsEnabled
      onViewChange={noop}
      onToggleExpand={noop}
      onNewWorkspace={noop}
      onEditWorkspace={noop}
      onDeleteWorkspace={noop}
      onNewSession={noop}
      onSelectSession={noop}
      onRenameSession={noop}
      onDeleteSession={noop}
    />,
  );
}

describe("Sidebar external agents feature gate", () => {
  it("hides the 外援 navigation entry while the feature is disabled", () => {
    expect(renderSidebar(false)).not.toContain("<span>外援</span>");
    expect(renderSidebar(true)).toContain("<span>外援</span>");
  });

  it("uses the supplied external-agent mark and name-first identity label", () => {
    const html = renderExternalSession();
    expect(html).toContain("session__agent-badge");
    expect(html).not.toContain("session__agent-icon");
    expect(html).toContain(">K</span>");
    expect(html).toContain("kimi · Agent · 接口联调");
  });
});
