/** @vitest-environment happy-dom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SessionGoalPanel } from "./SessionGoal";

let host: HTMLElement;
let root: Root;
const objective = "Reconcile a synthetic invoice";
const active = { objective, status: "active" };
const path = "/api/session/session-one/goal";
const step = (state: string, code = "") => ({ request_id: "goal_fixture", state, code });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.useFakeTimers();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function button(label: string): HTMLButtonElement {
  const match = Array.from(host.querySelectorAll("button")).find((item) => item.textContent === label);
  if (!match) throw new Error(`missing button: ${label}`);
  return match;
}

async function render(sessionId = "session-one", initialObjective = "") {
  await act(async () => root.render(<SessionGoalPanel sessionId={sessionId} initialObjective={initialObjective} onClose={() => {}} />));
}

it("sends one versioned advance and shows the server's separate step and objective states", async () => {
  const writes: unknown[] = [];
  const fetch = vi.fn(async (url, options?: RequestInit) => {
    expect(url).toBe(path);
    if (options?.method === "POST") {
      writes.push(JSON.parse(String(options.body)));
      return json({ version: "accepted", supports_advance: true, goal: active, run: step("accepted") }, 202);
    }
    return writes.length
      ? json({ version: "accepted", supports_advance: true, goal: active, run: step("finished") })
      : json({ version: "initial", supports_advance: true, goal: active });
  });
  vi.stubGlobal("fetch", fetch);
  await render();
  await act(async () => { const advance = button("推进一轮"); advance.click(); advance.click(); });
  expect(writes).toEqual([{ action: "advance", version: "initial" }]);
  expect(host.textContent).toContain("本轮已接收");
  expect(button("暂停目标").disabled).toBe(false);
  expect(button("推进一轮").disabled).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(2_000));
  expect(host.textContent).toContain("本轮已结束");
  expect(host.textContent).toContain("会话目标 · 已启用");
  expect(button("标记目标完成").disabled).toBe(false);
  await act(async () => vi.advanceTimersByTimeAsync(8_000));
  expect(fetch).toHaveBeenCalledTimes(3);
});

it("does not let a stale observation undo an exact pause, even with a replacement draft", async () => {
  let deliver!: (result: Response) => void;
  const older = new Promise<Response>((resolve) => { deliver = resolve; });
  const writes: unknown[] = [];
  let reads = 0;
  const fetch = vi.fn(async (_url, options?: RequestInit) => {
    if (options?.method === "POST") {
      writes.push(JSON.parse(String(options.body)));
      return json({ version: "paused", goal: { objective, status: "paused" }, run: step("stopping") });
    }
    reads += 1;
    if (reads === 2) return older;
    if (reads === 3) return json({ version: "paused", goal: { objective, status: "paused" }, run: step("stopped") });
    return json({ version: "working", goal: active, run: step("running") });
  });
  vi.stubGlobal("fetch", fetch);
  await render("session-one", "Retain this replacement draft");
  expect(button("保存目标").disabled).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(2_000));
  const signal = fetch.mock.calls[1]?.[1]?.signal;
  await act(async () => button("暂停目标").click());
  expect(signal?.aborted).toBe(true);
  expect(writes).toEqual([{ action: "pause", version: "working" }]);
  await act(async () => deliver(json({ version: "working", goal: active, run: step("running") })));
  expect(host.textContent).toContain("正在停止本轮");
  expect(host.querySelector("textarea")?.value).toBe("Retain this replacement draft");
  await act(async () => vi.advanceTimersByTimeAsync(2_000));
  expect(host.textContent).toContain("本轮已停止");
  expect(host.textContent).toContain("会话目标 · 已暂停");
  expect(button("保存目标").disabled).toBe(false);
});

it("reconciles a lost update response only when the user requests a read", async () => {
  let reads = 0;
  let writes = 0;
  const fetch = vi.fn(async (_url, options?: RequestInit) => {
    if (options?.method === "POST") {
      writes += 1;
      throw new TypeError("connection lost");
    }
    return reads++ === 0
      ? json({ version: "original", supports_advance: true, goal: active })
      : json({ version: "next", supports_advance: true, goal: active, run: step("finished") });
  });
  vi.stubGlobal("fetch", fetch);
  await render();
  await act(async () => button("推进一轮").click());
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("无法确认目标更新");
  expect(button("推进一轮").disabled).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(reads).toBe(1);
  await act(async () => button("重新读取目标").click());
  expect(host.textContent).toContain("本轮已结束");
  expect(writes).toBe(1);
});

it("keeps the Gateway reason for a refused advance without retrying the mutation", async () => {
  const fetch = vi.fn(async (_url, options?: RequestInit) => options?.method === "POST"
    ? json({ code: "goal_plan_active", error: "请先退出计划模式，再推进目标" }, 409)
    : json({ version: "current", supports_advance: true, goal: active }));
  vi.stubGlobal("fetch", fetch);
  await render();
  await act(async () => button("推进一轮").click());
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("请先退出计划模式");
  expect(fetch.mock.calls.filter(([, opt]) => opt?.method === "POST")).toHaveLength(1);
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(host.querySelector("[data-goal-run]")).toBeNull();
});

it("prevents a pending save from consuming text edited while waiting for its receipt", async () => {
  let deliver!: (result: Response) => void;
  const pending = new Promise<Response>((resolve) => { deliver = resolve; });
  const fetch = vi.fn(async (_url, options?: RequestInit) => options?.method === "POST"
    ? pending : json({ version: "old", supports_advance: true, goal: active }));
  vi.stubGlobal("fetch", fetch);
  await render("session-one", "New draft");
  const editor = host.querySelector("textarea")!;
  act(() => button("保存目标").click());
  expect(editor.readOnly).toBe(true);
  await act(async () => deliver(json({ version: "new", goal: { objective: "New draft", status: "active" } })));
  expect(host.querySelector("textarea")).toBeNull();
});

it("stops observing after a read failure and shows the interrupted state after an explicit refresh", async () => {
  const fetch = vi.fn<(_url: string, options?: RequestInit) => Promise<Response>>()
    .mockResolvedValueOnce(json({ version: "live", goal: active, run: step("running") }))
    .mockResolvedValueOnce(json({ error: "正在初始化" }, 503))
    .mockResolvedValueOnce(json({ version: "new", goal: { objective, status: "paused" }, run: step("interrupted", "gateway_restarted") }));
  vi.stubGlobal("fetch", fetch);
  await render();
  await act(async () => vi.advanceTimersByTimeAsync(2_000));
  expect(host.textContent).toContain("上次确认：正在推进本轮");
  expect(button("暂停目标").disabled).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(16_000));
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async () => button("重新读取目标").click());
  expect(host.textContent).toContain("本轮已中断 · 网关重启后未重放本轮。");
  expect(button("恢复目标").disabled).toBe(false);
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
});

it("leaves older gateways manual and ignores observations from the previous session", async () => {
  let deliver!: (result: Response) => void;
  const withheld = new Promise<Response>((resolve) => { deliver = resolve; });
  let primaryReads = 0;
  const fetch = vi.fn(async (url, _options?: RequestInit) => {
    if (url !== path) return json({ version: "old-gateway", goal: { objective: "Different goal", status: "active" } });
    if (++primaryReads === 1) return json({ version: "new", goal: active, run: step("running") });
    return withheld;
  });
  vi.stubGlobal("fetch", fetch);
  await render();
  await act(async () => vi.advanceTimersByTimeAsync(2_000));
  const signal = fetch.mock.calls[1]?.[1]?.signal;
  await render("session-two");
  expect(signal?.aborted).toBe(true);
  await act(async () => deliver(json({ version: "new", goal: active, run: step("interrupted", "gateway_restarted") })));
  expect(host.textContent).toContain("Different goal");
  expect(host.textContent).not.toContain("推进一轮");
  expect(host.textContent).not.toContain("本轮已中断");
  expect(button("暂停目标").disabled).toBe(false);
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(fetch).toHaveBeenCalledTimes(3);
});
