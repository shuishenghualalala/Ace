/** @vitest-environment happy-dom */
import React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentsFeature, { installAgentsPageContribution } from "./AgentsFeature";
import { UiPageRegistry } from "../lib/ui-feature-registry";

const apiMock = vi.hoisted(() => ({
  externalTeams: vi.fn(),
}));

vi.mock("../api", () => ({ api: apiMock }));

vi.mock("../components/AgentsHub", () => ({
  default: (props: {
    onAssignAgent: (agent: { id: string; name: string }) => void;
    onAssignTeam: (team: { id: string; name: string }) => void;
    onStartLeaderChat: (agent: { id: string; name: string }) => void;
  }) => (
    <section data-testid="agents-hub">
      <button
        type="button"
        data-assign-agent
        onClick={() => props.onAssignAgent({ id: "agent-1", name: "Agent 1" })}
      />
      <button
        type="button"
        data-assign-team
        onClick={() => props.onAssignTeam({ id: "team-1", name: "Team 1" })}
      />
      <button
        type="button"
        data-start-leader
        onClick={() => props.onStartLeaderChat({ id: "leader-1", name: "Leader 1" })}
      />
    </section>
  ),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

describe("AgentsFeature lifecycle", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    apiMock.externalTeams.mockReset();
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(() => {
    act(() => root?.unmount());
    container.remove();
  });

  it("does not write a late externalTeams response after unmount", async () => {
    const pending = deferred<{ id: string; name: string }[]>();
    apiMock.externalTeams.mockReturnValue(pending.promise);
    root = createRoot(container);

    await act(async () => {
      root.render(
        <AgentsFeature
          onAssignAgent={vi.fn()}
          onAssignTeam={vi.fn()}
          onStartLeaderChat={vi.fn()}
        />,
      );
    });

    act(() => root.unmount());
    pending.resolve([{ id: "late", name: "Late Team" }]);
    await act(async () => { await pending.promise; });

    expect(container.querySelector("[data-testid=agents-hub]")).toBeNull();
  });

  it("is not projected by the registry when agents capability is disabled", () => {
    const registry = new UiPageRegistry<string, { agentsEnabled: boolean; props: unknown }, React.ReactNode>();
    installAgentsPageContribution(registry);

    const page = registry.project("agents", {
      agentsEnabled: false,
      props: {
        onAssignAgent: vi.fn(),
        onAssignTeam: vi.fn(),
        onStartLeaderChat: vi.fn(),
      },
    });

    expect(page).toBeNull();
  });

  it("is projected by the registry when agents capability is enabled", () => {
    const registry = new UiPageRegistry<string, { agentsEnabled: boolean; props: unknown }, React.ReactNode>();
    installAgentsPageContribution(registry);

    const page = registry.project("agents", {
      agentsEnabled: true,
      props: {
        onAssignAgent: vi.fn(),
        onAssignTeam: vi.fn(),
        onStartLeaderChat: vi.fn(),
      },
    });

    expect(page).not.toBeNull();
  });
});
