import { describe, expect, it } from "vitest";
import type { ReactNode } from "react";
import { UiFeatureRegistry } from "./ui-feature-registry";
import { WIKI_FEATURE_ID } from "./featureFlags";
import {
  canNavigateToSidebarView,
  registerWikiNavigation,
  resolveSidebarViewAfterCapabilitiesChange,
  wikiNavigationEnabled,
} from "./wiki-navigation";
import type { AppConfig, FeatureCapability } from "../types";

function baseConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    model: "m",
    has_key: true,
    base_url: "",
    active_model_id: "m",
    models: [],
    wiki: { enabled: true },
    ...overrides,
  };
}

function configWithCapabilities(
  caps: Record<string, FeatureCapability>,
  wikiEnabled = true,
): AppConfig {
  return { ...baseConfig({ wiki: { enabled: wikiEnabled } }), feature_capabilities: caps };
}

function failedWikiCapability(): Record<string, FeatureCapability> {
  return { [WIKI_FEATURE_ID]: { state: "failed", available: false, generation: null } };
}

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
    expect(wikiNavigationEnabled(baseConfig())).toBe(true);
    expect(wikiNavigationEnabled(baseConfig({ wiki: { enabled: false } }))).toBe(false);
    expect(canNavigateToSidebarView("chat", baseConfig({ wiki: { enabled: false } }))).toBe(true);
    expect(canNavigateToSidebarView("wiki", baseConfig({ wiki: { enabled: false } }))).toBe(false);
  });

  it("falls back from Wiki only when the capability becomes disabled", () => {
    expect(resolveSidebarViewAfterCapabilitiesChange("wiki", baseConfig({ wiki: { enabled: false } }))).toBe("chat");
    expect(resolveSidebarViewAfterCapabilitiesChange("wiki", null)).toBe("wiki");
    expect(resolveSidebarViewAfterCapabilitiesChange("skills", baseConfig({ wiki: { enabled: false } }))).toBe("skills");
  });

  it("treats the unified capability snapshot as authoritative over the legacy flag", () => {
    // wiki.enabled=true 但 product.wiki available=false（state=failed）→ 入口不可用。
    expect(wikiNavigationEnabled(configWithCapabilities(failedWikiCapability()))).toBe(false);
    expect(canNavigateToSidebarView("wiki", configWithCapabilities(failedWikiCapability()))).toBe(false);
    expect(resolveSidebarViewAfterCapabilitiesChange("wiki", configWithCapabilities(failedWikiCapability()))).toBe("chat");
  });

  it("restores the entry once the capability recovers", () => {
    const config = configWithCapabilities({
      [WIKI_FEATURE_ID]: { state: "active", available: true, generation: "g1" },
    });
    expect(wikiNavigationEnabled(config)).toBe(true);
    expect(canNavigateToSidebarView("wiki", config)).toBe(true);
    expect(resolveSidebarViewAfterCapabilitiesChange("wiki", config)).toBe("wiki");
  });

  it("treats a missing entry in an existing snapshot as unavailable", () => {
    expect(wikiNavigationEnabled(configWithCapabilities({}))).toBe(false);
    expect(
      wikiNavigationEnabled(
        configWithCapabilities({ "other.feature": { state: "enabled", available: true, generation: null } }),
      ),
    ).toBe(false);
  });
});
