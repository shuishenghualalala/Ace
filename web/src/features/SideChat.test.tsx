/** @vitest-environment happy-dom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { useSideChat } from "./SideChat";

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function Harness({ parentId, loadHistory }: { parentId: string; loadHistory: (id: string) => Promise<void> }) {
  const { pane, open, close } = useSideChat(parentId, loadHistory);
  return <>
    <button onClick={open}>open</button>
    <button onClick={close}>close</button>
    <output>{pane?.status === "ready" ? pane.sessionId : pane?.status}</output>
  </>;
}

const output = () => host.querySelector("output")?.textContent;
const open = () => host.querySelector("button")!.click();

it("drops a late child from a previous parent even if its HTTP request finishes after abort", async () => {
  let deliver!: (result: Awaited<ReturnType<typeof api.createSideChat>>) => void;
  const request = new Promise<Awaited<ReturnType<typeof api.createSideChat>>>((resolve) => { deliver = resolve; });
  const create = vi.spyOn(api, "createSideChat").mockReturnValueOnce(request).mockResolvedValueOnce({
    ok: true, source_session_id: "new-parent", session_id: "new-child", workspace_id: "workspace",
  });
  const loadHistory = vi.fn(async () => {});
  await act(async () => root.render(<Harness parentId="old-parent" loadHistory={loadHistory} />));
  act(() => { open(); open(); });
  expect(create).toHaveBeenCalledTimes(1);
  expect(output()).toBe("opening");

  await act(async () => root.render(<Harness parentId="new-parent" loadHistory={loadHistory} />));
  expect(create.mock.calls[0]![1].aborted).toBe(true);
  await act(async () => open());
  expect(output()).toBe("new-child");
  await act(async () => deliver({ ok: true, source_session_id: "old-parent", session_id: "old-child", workspace_id: "workspace" }));
  expect(output()).toBe("new-child");
  expect(loadHistory.mock.calls).toEqual([["new-child"]]);
});

it("aborts an opening side chat and ignores its late response", async () => {
  let deliver!: (result: Awaited<ReturnType<typeof api.createSideChat>>) => void;
  const request = new Promise<Awaited<ReturnType<typeof api.createSideChat>>>((resolve) => { deliver = resolve; });
  const create = vi.spyOn(api, "createSideChat").mockReturnValueOnce(request);
  const loadHistory = vi.fn(async () => {});
  await act(async () => root.render(<Harness parentId="parent" loadHistory={loadHistory} />));
  await act(async () => open());
  const buttons = host.querySelectorAll("button");
  await act(async () => buttons[1]!.click());
  expect(output()).toBe("");
  expect(create.mock.calls[0]![1].aborted).toBe(true);
  await act(async () => deliver({ ok: true, source_session_id: "parent", session_id: "child", workspace_id: "default" }));
  expect(output()).toBe("");
  expect(loadHistory).not.toHaveBeenCalled();
});

it("refuses an invalid response that points back to the parent", async () => {
  vi.spyOn(api, "createSideChat").mockResolvedValue({ ok: true, source_session_id: "parent", session_id: "parent", workspace_id: "default" });
  const loadHistory = vi.fn(async () => {});
  await act(async () => root.render(<Harness parentId="parent" loadHistory={loadHistory} />));
  await act(async () => open());
  expect(output()).toBe("failed");
  expect(loadHistory).not.toHaveBeenCalled();
});
