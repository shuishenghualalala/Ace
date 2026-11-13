import { describe, expect, it } from "vitest";
import { UiFeatureRegistry } from "./ui-feature-registry";

type Context = { enabled: boolean };
type Id = "first" | "second";

const entry = (id: Id, order: number, isAvailable?: (context: Context) => boolean) => ({
  id,
  label: id,
  icon: id,
  order,
  isAvailable,
});

describe("UiFeatureRegistry", () => {
  it("projects contributions in deterministic order and applies availability", () => {
    const registry = new UiFeatureRegistry<Id, Context>();
    registry.register(entry("second", 20));
    registry.register(entry("first", 10, ({ enabled }) => enabled));

    expect(registry.project({ enabled: true }).map(({ id }) => id)).toEqual(["first", "second"]);
    expect(registry.project({ enabled: false }).map(({ id }) => id)).toEqual(["second"]);
  });

  it("fails loudly on duplicate ids and invalid order", () => {
    const registry = new UiFeatureRegistry<Id, Context>();
    registry.register(entry("first", 10));

    expect(() => registry.register(entry("first", 20))).toThrow(/already registered/);
    expect(() => registry.register(entry("second", Number.NaN))).toThrow(/invalid order/);
  });

  it("supports idempotent disposal without stale disposers removing a replacement", () => {
    const registry = new UiFeatureRegistry<Id, Context>();
    const disposeFirst = registry.register(entry("first", 10));
    disposeFirst();
    disposeFirst();
    const disposeReplacement = registry.register(entry("first", 20));

    disposeFirst();
    expect(registry.project({ enabled: true }).map(({ order }) => order)).toEqual([20]);
    expect(registry.unregister("first")).toBe(true);
    expect(registry.unregister("first")).toBe(false);
    disposeReplacement();
    expect(registry.project({ enabled: true })).toEqual([]);
  });
});
