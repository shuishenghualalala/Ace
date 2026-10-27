"""Pure subagent policy algebra tests."""

from __future__ import annotations

from dataclasses import dataclass

from crew.agent.subagent.policy import (
    apply_model_capability_policy,
    apply_preset_tool_policy,
    resolve_subagent_tool_filter,
)


@dataclass
class _Catalog:
    by_toolset: dict[str, list[str]]

    def names(self) -> list[str]:
        return [name for names in self.by_toolset.values() for name in names]

    def names_for_toolset(self, toolset: str) -> list[str]:
        return list(self.by_toolset.get(toolset, ()))

    def toolset_for(self, name: str) -> str | None:
        for toolset, names in self.by_toolset.items():
            if name in names:
                return toolset
        return None


def test_subagent_policy_is_parent_snapshot_intersection_and_blacklist():
    catalog = _Catalog(
        {
            "file": ["file_read", "file_write"],
            "terminal": ["terminal"],
            "subagent": ["delegate_task"],
            "feishu.send": ["feishu_send"],
        }
    )
    assert resolve_subagent_tool_filter(
        catalog,
        ["file_read", "file_write", "terminal", "delegate_task", "feishu_send"],
        requested_toolsets="file",
        blocked_toolsets={"subagent"},
        blocked_toolset_prefixes=("feishu",),
    ) == ["file_read", "file_write"]


def test_preset_policy_can_narrow_without_expanding_parent():
    catalog = _Catalog({"file": ["file_read"], "terminal": ["terminal"]})
    assert apply_preset_tool_policy(
        catalog,
        ["file_read"],
        {"toolsets": ["terminal"], "toolset_additions": ["terminal"]},
    ) == []
    assert apply_preset_tool_policy(
        catalog,
        ["file_read"],
        {"toolsets": ["terminal"], "toolset_additions": ["terminal"]},
        addition_ceiling=["file_read", "terminal"],
    ) == ["terminal"]


def test_model_capability_policy_is_deterministic():
    names = ["file_read", "browser_vision"]
    assert apply_model_capability_policy(names, None) == names
    assert apply_model_capability_policy(names, ["text"]) == []
    assert apply_model_capability_policy(names, ["text", "tools", "vision"]) == names
    assert apply_model_capability_policy(names, ["text"]) is not names
