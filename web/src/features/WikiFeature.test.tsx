/** @vitest-environment happy-dom */
import React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Props as ChatPanelProps } from "../components/ChatPanel";
import type { Mode, Session } from "../types";
import WikiFeature from "./WikiFeature";

const apiMock = vi.hoisted(() => ({
  wikiAgentSession: vi.fn(),
  deleteSession: vi.fn(),
}));

vi.mock("../api", () => ({ api: apiMock }));
vi.mock("../components/WikiHub", () => ({
  default: (props: {
    kbId: string;
    sessionId: string;
    onKbChange: (id: string) => void;
    onNewSession: () => void;
    onSelectSession: (id: string) => void;
    onDeleteSession: (id: string) => void;
  }) => (
    <section data-testid="wiki-hub" data-kb={props.kbId} data-session={props.sessionId}>
      <button type="button" data-switch-kb onClick={() => props.onKbChange("work")} />
      <button type="button" data-new-session onClick={() => void props.onNewSession()} />
      <button type="button" data-select-session onClick={() => props.onSelectSession("selected-session")} />
      <button type="button" data-delete-session onClick={() => void props.onDeleteSession(props.sessionId)} />
    </section>
  ),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

type TestChat = React.ComponentProps<typeof WikiFeature>["chat"] & {
  loadHistory: ReturnType<typeof vi.fn>;
  clearSession: ReturnType<typeof vi.fn>;
  forSession: ReturnType<typeof vi.fn>;
};

function makeChat(): TestChat {
  return {
    loadHistory: vi.fn(),
    clearSession: vi.fn(),
    forSession: vi.fn(() => ({})),
    wikiProgress: {},
  } as unknown as TestChat;
}

function makeProps(chat: TestChat, overrides: Partial<React.ComponentProps<typeof WikiFeature>> = {}) {
  return {
    baseChatProps: {} as ChatPanelProps,
    chat,
    mode: "agent" as Mode,
    workspaceId: "workspace-1",
    currentAgentLabel: undefined as Session["agent_label"],
    pendingWikiLinkTitle: null,
    onPendingWikiLinkHandled: vi.fn(),
    ...overrides,
  };
}

describe("WikiFeature lifecycle", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    apiMock.wikiAgentSession.mockReset();
    apiMock.deleteSession.mockReset();
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(() => {
    act(() => root?.unmount());
    container.remove();
  });

  it("does not write a late session response after the page unmounts", async () => {
    const pending = deferred<{ session_id: string }>();
    apiMock.wikiAgentSession.mockReturnValue(pending.promise);
    const chat = makeChat();
    root = createRoot(container);

    await act(async () => { root.render(<WikiFeature {...makeProps(chat)} />); });
    expect(apiMock.wikiAgentSession).toHaveBeenCalledWith("default");
    act(() => root.unmount());
    pending.resolve({ session_id: "late-session" });
    await act(async () => { await pending.promise; });

    expect(chat.loadHistory).not.toHaveBeenCalled();
    expect(container.querySelector("[data-testid=wiki-hub]")).toBeNull();
  });

  it("ignores the old KB request after a real page event switches to another KB", async () => {
    const defaultRequest = deferred<{ session_id: string }>();
    const workRequest = deferred<{ session_id: string }>();
    apiMock.wikiAgentSession
      .mockReturnValueOnce(defaultRequest.promise)
      .mockReturnValueOnce(workRequest.promise);
    const chat = makeChat();
    root = createRoot(container);

    await act(async () => { root.render(<WikiFeature {...makeProps(chat)} />); });
    await act(async () => {
      container.querySelector<HTMLButtonElement>("[data-switch-kb]")?.click();
    });
    defaultRequest.resolve({ session_id: "default-late" });
    await act(async () => { await defaultRequest.promise; });
    expect(chat.loadHistory).not.toHaveBeenCalled();

    workRequest.resolve({ session_id: "work-session" });
    await act(async () => { await workRequest.promise; });
    expect(chat.loadHistory).toHaveBeenCalledWith("work-session");
    expect(container.querySelector("[data-testid=wiki-hub]")?.getAttribute("data-kb")).toBe("work");
  });

  it("keeps session bindings isolated between two mounted Wiki pages", async () => {
    const requests = [
      deferred<{ session_id: string }>(),
      deferred<{ session_id: string }>(),
    ];
    let requestIndex = 0;
    apiMock.wikiAgentSession.mockImplementation(() => requests[requestIndex++]!.promise);
    const firstChat = makeChat();
    const secondChat = makeChat();
    const firstContainer = document.createElement("div");
    const secondContainer = document.createElement("div");
    document.body.append(firstContainer, secondContainer);
    const firstRoot = createRoot(firstContainer);
    const secondRoot = createRoot(secondContainer);

    await act(async () => {
      firstRoot.render(<WikiFeature {...makeProps(firstChat)} />);
      secondRoot.render(<WikiFeature {...makeProps(secondChat)} />);
    });
    requests[0].resolve({ session_id: "first-session" });
    requests[1].resolve({ session_id: "second-session" });
    await act(async () => { await Promise.resolve(); });

    // A second mount must request and receive its own binding; it cannot reuse
    // a module-global session from the first page instance.
    expect(apiMock.wikiAgentSession).toHaveBeenCalledTimes(2);
    expect(firstChat.loadHistory).toHaveBeenCalledWith("first-session");
    expect(secondChat.loadHistory).toHaveBeenCalledWith("second-session");
    firstRoot.unmount();
    secondRoot.unmount();
    firstContainer.remove();
    secondContainer.remove();
  });

  it("does not let a late new-session response overwrite a later explicit selection", async () => {
    const initial = deferred<{ session_id: string }>();
    const created = deferred<{ session_id: string }>();
    apiMock.wikiAgentSession
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(created.promise);
    const chat = makeChat();
    root = createRoot(container);

    await act(async () => { root.render(<WikiFeature {...makeProps(chat)} />); });
    initial.resolve({ session_id: "initial-session" });
    await act(async () => { await initial.promise; });
    expect(chat.loadHistory).toHaveBeenCalledWith("initial-session");

    await act(async () => {
      container.querySelector<HTMLButtonElement>("[data-new-session]")?.click();
      container.querySelector<HTMLButtonElement>("[data-select-session]")?.click();
    });
    expect(apiMock.wikiAgentSession).toHaveBeenLastCalledWith("default", { forceNew: true });
    expect(chat.loadHistory).toHaveBeenCalledWith("selected-session");
    created.resolve({ session_id: "created-late" });
    await act(async () => { await created.promise; });

    expect(container.querySelector("[data-testid=wiki-hub]")?.getAttribute("data-session")).toBe("selected-session");
  });

  it("does not let a late delete replacement overwrite a later explicit selection", async () => {
    const initial = deferred<{ session_id: string }>();
    const replacement = deferred<{ session_id: string }>();
    const deletion = deferred<void>();
    apiMock.wikiAgentSession
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(replacement.promise);
    apiMock.deleteSession.mockReturnValueOnce(deletion.promise);
    const chat = makeChat();
    root = createRoot(container);

    await act(async () => { root.render(<WikiFeature {...makeProps(chat)} />); });
    initial.resolve({ session_id: "initial-session" });
    await act(async () => { await initial.promise; });

    await act(async () => {
      container.querySelector<HTMLButtonElement>("[data-delete-session]")?.click();
      container.querySelector<HTMLButtonElement>("[data-select-session]")?.click();
    });
    deletion.resolve();
    await act(async () => { await deletion.promise; });
    replacement.resolve({ session_id: "replacement-late" });
    await act(async () => { await replacement.promise; });

    expect(container.querySelector("[data-testid=wiki-hub]")?.getAttribute("data-session")).toBe("selected-session");
  });
});
