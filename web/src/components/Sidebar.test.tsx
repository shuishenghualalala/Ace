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

describe("Sidebar external agents feature gate", () => {
  it("hides the 外援 navigation entry while the feature is disabled", () => {
    expect(renderSidebar(false)).not.toContain("<span>外援</span>");
    expect(renderSidebar(true)).toContain("<span>外援</span>");
  });
});
