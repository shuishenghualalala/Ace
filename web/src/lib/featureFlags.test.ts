import { describe, expect, it } from "vitest";
import {
  externalAgentsAvailable,
  EXTERNAL_AGENTS_FEATURE_ID,
  kanbanFeatureAvailable,
  KANBAN_FEATURE_ID,
  teamFeatureAvailable,
  TEAM_FEATURE_ID,
  wikiFeatureAvailable,
  WIKI_FEATURE_ID,
} from "./featureFlags";
import type { AppConfig, FeatureCapability } from "../types";

function capability(available: boolean, generation: string | null = null): FeatureCapability {
  return { state: available ? "enabled" : "disabled", available, generation };
}

function baseConfig(): AppConfig {
  return {
    model: "m",
    has_key: true,
    base_url: "",
    active_model_id: "m",
    models: [],
    wiki: { enabled: true },
  };
}

function configWithCapabilities(caps: Record<string, FeatureCapability>): AppConfig {
  return { ...baseConfig(), feature_capabilities: caps };
}

const FEATURES: Array<{ id: string; available: (config: AppConfig | null) => boolean }> = [
  { id: TEAM_FEATURE_ID, available: teamFeatureAvailable },
  { id: KANBAN_FEATURE_ID, available: kanbanFeatureAvailable },
  { id: EXTERNAL_AGENTS_FEATURE_ID, available: externalAgentsAvailable },
  // Wiki 能力条目与其它 feature 同一推导；旧 wiki.enabled 回落语义由 wiki-navigation 组合。
  { id: WIKI_FEATURE_ID, available: wikiFeatureAvailable },
];

describe("feature capability availability rules (ADR-0041 legacy-safe)", () => {
  for (const { id, available } of FEATURES) {
    describe(`feature id "${id}"`, () => {
      const ruleCases: Array<[string, AppConfig | null, boolean]> = [
        // config 未加载 → 可用，保持今日行为。
        ["config 为 null（加载中/暂未连接）→ 可用", null, true],
        // 旧后端：响应无 feature_capabilities 字段 → 可用，不降级为关闭。
        ["无 feature_capabilities 字段 → 可用", baseConfig(), true],
        // 字段存在但该项缺席 → 不可用（ADR-0040「缺席视为不可用」）。
        ["feature_capabilities 为空对象（项缺席）→ 不可用", configWithCapabilities({}), false],
        ["该项缺席但其它 feature 存在 → 不可用", configWithCapabilities({ "other.feature": capability(true) }), false],
        // available 显式真/假。
        ["项存在且 available: true → 可用", configWithCapabilities({ [id]: capability(true) }), true],
        ["项存在且 available: false → 不可用", configWithCapabilities({ [id]: capability(false) }), false],
        // available 缺省（undefined ≠ true）同样视为不可用。
        [
          "项存在但 available 缺省 → 不可用",
          configWithCapabilities({ [id]: { state: "enabled", available: undefined as unknown as boolean, generation: null } }),
          false,
        ],
        // 可用性只由 available 决定，state/generation 不影响结果。
        ["available: true 时 state/generation 不影响 → 可用", configWithCapabilities({ [id]: capability(true, "3") }), true],
      ];
      for (const [label, config, expected] of ruleCases) {
        it(label, () => {
          expect(available(config)).toBe(expected);
        });
      }
    });
  }
});

describe("team/kanban availability are independent", () => {
  it("team 可用 + kanban 不可用 → 分别推导互不影响", () => {
    const config = configWithCapabilities({
      [TEAM_FEATURE_ID]: capability(true),
      [KANBAN_FEATURE_ID]: capability(false),
    });
    expect(teamFeatureAvailable(config)).toBe(true);
    expect(kanbanFeatureAvailable(config)).toBe(false);
  });

  it("team 不可用 + kanban 可用 → 分别推导互不影响", () => {
    const config = configWithCapabilities({
      [TEAM_FEATURE_ID]: capability(false),
      [KANBAN_FEATURE_ID]: capability(true),
    });
    expect(teamFeatureAvailable(config)).toBe(false);
    expect(kanbanFeatureAvailable(config)).toBe(true);
  });

  it("一个 feature 缺席不拖累另一个显式可用的 feature", () => {
    const config = configWithCapabilities({
      [KANBAN_FEATURE_ID]: capability(true),
    });
    expect(teamFeatureAvailable(config)).toBe(false);
    expect(kanbanFeatureAvailable(config)).toBe(true);
  });
});

describe("external-agents availability is independent of team/kanban", () => {
  it("外援不可用 + team/kanban 可用 → 分别推导互不影响", () => {
    const config = configWithCapabilities({
      [EXTERNAL_AGENTS_FEATURE_ID]: capability(false),
      [TEAM_FEATURE_ID]: capability(true),
      [KANBAN_FEATURE_ID]: capability(true),
    });
    expect(externalAgentsAvailable(config)).toBe(false);
    expect(teamFeatureAvailable(config)).toBe(true);
    expect(kanbanFeatureAvailable(config)).toBe(true);
  });

  it("外援可用 + team/kanban 不可用 → 分别推导互不影响", () => {
    const config = configWithCapabilities({
      [EXTERNAL_AGENTS_FEATURE_ID]: capability(true),
      [TEAM_FEATURE_ID]: capability(false),
      [KANBAN_FEATURE_ID]: capability(false),
    });
    expect(externalAgentsAvailable(config)).toBe(true);
    expect(teamFeatureAvailable(config)).toBe(false);
    expect(kanbanFeatureAvailable(config)).toBe(false);
  });

  it("外援缺席不被显式可用的 team/kanban 拯救，也不拖累它们", () => {
    const config = configWithCapabilities({
      [TEAM_FEATURE_ID]: capability(true),
      [KANBAN_FEATURE_ID]: capability(true),
    });
    expect(externalAgentsAvailable(config)).toBe(false);
    expect(teamFeatureAvailable(config)).toBe(true);
    expect(kanbanFeatureAvailable(config)).toBe(true);
  });
});
