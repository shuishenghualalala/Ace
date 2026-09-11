import { describe, expect, it, vi } from "vitest";
import {
  FeatureEventRegistry,
  parseFeatureEventBody,
} from "./feature-event-dispatcher";

describe("FeatureEventRegistry", () => {
  it("registers and dispatches a handler", () => {
    const registry = new FeatureEventRegistry();
    const handler = vi.fn(() => ({ statusHint: "running" as const }));
    registry.register({ feature: "test", event: "ping", version: 1, handler });

    const ctx = {
      sessionId: "s1",
      now: 1,
      startLocalTurn: () => 1,
      newId: () => "id",
      book: {} as any,
      messages: [],
    };
    const result = registry.dispatch("test", "ping", 1, { value: 42 }, ctx);

    expect(handler).toHaveBeenCalledWith({ value: 42 }, ctx);
    expect(result).toEqual({ statusHint: "running" });
  });

  it("throws on duplicate registration", () => {
    const registry = new FeatureEventRegistry();
    registry.register({ feature: "x", event: "y", version: 1, handler: () => null });
    expect(() =>
      registry.register({ feature: "x", event: "y", version: 1, handler: () => null }),
    ).toThrow("Feature event handler already registered: x/y@1");
  });

  it("disposer is idempotent and only unregisters its own registration", () => {
    const registry = new FeatureEventRegistry();
    const h1 = vi.fn(() => null);
    const h2 = vi.fn(() => null);
    const dispose1 = registry.register({ feature: "x", event: "y", version: 1, handler: h1 });

    dispose1();
    dispose1();
    const dispose2 = registry.register({ feature: "x", event: "y", version: 1, handler: h2 });

    const ctx = {
      sessionId: "s1",
      now: 1,
      startLocalTurn: () => 1,
      newId: () => "id",
      book: {} as any,
      messages: [],
    };
    registry.dispatch("x", "y", 1, {}, ctx);
    expect(h1).not.toHaveBeenCalled();
    expect(h2).toHaveBeenCalled();

    dispose2();
    expect(registry.dispatch("x", "y", 1, {}, ctx)).toBeNull();
  });

  it("warns and returns null for unhandled event", () => {
    const registry = new FeatureEventRegistry();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const ctx = {
      sessionId: "s1",
      now: 1,
      startLocalTurn: () => 1,
      newId: () => "id",
      book: {} as any,
      messages: [],
    };
    const result = registry.dispatch("unknown", "event", 1, {}, ctx);
    expect(result).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      "[feature-event] unhandled event: feature=unknown event=event version=1",
    );
    warn.mockRestore();
  });

  it("treats version mismatch as unhandled", () => {
    const registry = new FeatureEventRegistry();
    registry.register({ feature: "x", event: "y", version: 2, handler: () => null });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const ctx = {
      sessionId: "s1",
      now: 1,
      startLocalTurn: () => 1,
      newId: () => "id",
      book: {} as any,
      messages: [],
    };
    expect(registry.dispatch("x", "y", 1, {}, ctx)).toBeNull();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe("parseFeatureEventBody", () => {
  it("parses feature_event body with explicit payload", () => {
    expect(
      parseFeatureEventBody({
        feature: "wiki",
        event: "cards",
        version: 1,
        payload: { pages: [] },
      }),
    ).toEqual({ feature: "wiki", event: "cards", version: 1, payload: { pages: [] } });
  });

  it("defaults version to 1 and payload to {}", () => {
    expect(parseFeatureEventBody({ feature: "team", event: "internal" })).toEqual({
      feature: "team",
      event: "internal",
      version: 1,
      payload: {},
    });
  });

  it("returns null for invalid body", () => {
    expect(parseFeatureEventBody(null)).toBeNull();
    expect(parseFeatureEventBody({})).toBeNull();
    expect(parseFeatureEventBody({ feature: "", event: "x" })).toBeNull();
    expect(parseFeatureEventBody("invalid")).toBeNull();
  });

  it("parsed body dispatches through the registry in the feature/event namespace", () => {
    const registry = new FeatureEventRegistry();
    const handler = vi.fn(() => ({ statusHint: "running" as const }));
    registry.register({ feature: "wiki", event: "cards", version: 1, handler });

    const parsed = parseFeatureEventBody({
      feature: "wiki",
      event: "cards",
      version: 1,
      payload: { pages: [{ id: "p1" }] },
    });
    const ctx = {
      sessionId: "s1",
      now: 1,
      startLocalTurn: () => 1,
      newId: () => "id",
      book: {} as any,
      messages: [],
    };
    const result = registry.dispatch(parsed!.feature, parsed!.event, parsed!.version, parsed!.payload, ctx);

    expect(handler).toHaveBeenCalledWith({ pages: [{ id: "p1" }] }, ctx);
    expect(result).toEqual({ statusHint: "running" });
  });
});

describe("featureEventRegistry singleton", () => {
  it("is available as an isolated host registry", () => {
    expect(new FeatureEventRegistry()).toBeInstanceOf(FeatureEventRegistry);
  });
});
