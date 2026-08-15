"""Data-flow registry contract: every local flow has an independent switch."""

from __future__ import annotations

import json
from pathlib import Path

from crew.state.config import Config

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs" / "security" / "data-flow-registry.json"


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def test_data_flow_registry_models_every_local_flow_independently():
    registry = _registry()
    assert registry["schema"] == "ace.data-flow-registry.v1"
    assert registry["prohibited_master_switch"] is True
    flows = registry["flows"]
    assert isinstance(flows, list) and len(flows) >= 4
    assert len({flow["id"] for flow in flows}) == len(flows)

    for flow in flows:
        for field in (
            "id",
            "status",
            "switch",
            "destination",
            "fields",
            "retention",
            "deletion",
        ):
            assert field in flow, f"{flow.get('id')} missing {field}"
        assert flow["status"] in {"active", "opt-in", "absent"}
        assert isinstance(flow["fields"], list)

    switches = {flow["id"]: flow["switch"] for flow in flows}
    assert "disable_all" not in switches
    assert switches["llm_trace"] != switches["session_history"]
    assert switches["feedback"] != switches["llm_trace"]


def test_data_flow_registry_matches_production_defaults():
    registry = _registry()
    llm = next(flow for flow in registry["flows"] if flow["id"] == "llm_trace")
    assert llm["status"] == "opt-in"
    assert Config().llm_trace is False

    absent = {flow["id"] for flow in registry["flows"] if flow["status"] == "absent"}
    assert {"analytics_upload", "otel_export", "rollout_telemetry"} <= absent
