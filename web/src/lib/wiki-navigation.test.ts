import { describe, expect, it } from "vitest";
import type { ReactNode } from "react";
import { UiFeatureRegistry } from "./ui-feature-registry";
import {
  canNavigateToSidebarView,
  registerWikiNavigation,
  resolveSidebarViewAfterCapabilitiesChange,
  wikiNavigationEnabled,
} from "./wiki-navigation";

describe("Wiki sidebar navigation contribution", () => {
  it("registers Wiki explicitly and projects it when enabled", () => {
    const registry = new UiFeatureRegistry<"wiki", { wikiEnabled: boolean }, ReactNode>();
    registerWikiNavigation(registry);
    const ids = registry
      .project({ wikiEnabled: true })
      .map(({ id }) => id);

    expect(ids).toEqual(["wiki"]);
  });

  it("hides Wiki while disabled", () => {
    const registry = new UiFeatureRegistry<"wiki", { wikiEnabled: boolean }, ReactNode>();
    registerWikiNavigation(registry);
    const ids = registry
      .project({ wikiEnabled: false })
      .map(({ id }) => id);

    expect(ids).toEqual([]);
  });

  it("uses the legacy-safe default while configuration is unavailable", () => {
    expect(wikiNavigationEnabled(null)).toBe(true);
    expect(wikiNavigationEnabled({})).toBe(true);
    expect(wikiNavigationEnabled({ wiki: { enabled: false } })).toBe(false);
    expect(canNavigateToSidebarView("chat", { wiki: { enabled: false } })).toBe(true);
    expect(canNavigateToSidebarView("wiki", { wiki: { enabled: false } })).toBe(false);
  });

  it("falls back from Wiki only when the capability becomes disabled", () => {
    expect(resolveSidebarViewAfterCapabilitiesChange("wiki", { wiki: { enabled: false } })).toBe("chat");
    expect(resolveSidebarViewAfterCapabilitiesChange("wiki", null)).toBe("wiki");
    expect(resolveSidebarViewAfterCapabilitiesChange("skills", { wiki: { enabled: false } })).toBe("skills");
  });
});
