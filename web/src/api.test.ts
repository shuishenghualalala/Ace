import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

const draft = (description: string) => ({
  description,
  workflow: "Leader 拆解并汇总",
  slots: [],
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("external team draft streams", () => {
  it("applies the initial snapshot before the optimized snapshot", async () => {
    const encoder = new TextEncoder();
    const initial = JSON.stringify({ type: "draft", phase: "initial", draft: draft("本地草案") });
    const delta = JSON.stringify({ type: "description_delta", text: "LLM 草" });
    const optimized = JSON.stringify({
      type: "draft",
      phase: "optimized",
      draft: draft("LLM 草案"),
      llm_elapsed_ms: 3240,
      cache_hit: false,
    });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(`${initial}\n${delta}\n${optimized.slice(0, 18)}`));
        controller.enqueue(encoder.encode(`${optimized.slice(18)}\n`));
        controller.close();
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200 })));
    const snapshots: string[] = [];
    const deltas: string[] = [];
    const elapsed: number[] = [];

    const result = await api.draftExternalTeamDescription(
      { name: "测试团队" },
      {
        onDraft: (value, _phase, meta) => {
          snapshots.push(value.description);
          if (meta.llmElapsedMs != null) elapsed.push(meta.llmElapsedMs);
        },
        onDescriptionDelta: (text) => deltas.push(text),
      },
    );

    expect(snapshots).toEqual(["本地草案", "LLM 草案"]);
    expect(deltas).toEqual(["LLM 草"]);
    expect(elapsed).toEqual([3240]);
    expect(result.description).toBe("LLM 草案");
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe("/api/external-teams/draft/description");
  });

  it("passes AbortController cancellation to fetch", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })));
    const controller = new AbortController();
    const pending = api.draftExternalTeamDescription({ name: "旧团队" }, { signal: controller.signal });

    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("uses the independent formation endpoint", async () => {
    const encoder = new TextEncoder();
    const event = JSON.stringify({ type: "draft", phase: "initial", draft: draft("保留描述") });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(`${event}\n`));
        controller.close();
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200 })));

    await api.draftExternalTeamFormation({
      name: "测试团队",
      description: "保留描述",
      leader_agent_id: "agent_a",
    });

    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe("/api/external-teams/draft/formation");
  });
});
