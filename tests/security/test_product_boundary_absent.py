"""Keep Codex-only product surfaces absent unless the applicability ledger changes."""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "docs" / "security" / "codex-security-capability-baseline.md"
INVENTORY = ROOT / "docs" / "security" / "codex-security-na-inventory.json"
BASELINE_ID_RE = re.compile(r"^\| ([A-Z][A-Z0-9]*-[0-9]+) \|", re.MULTILINE)
VALID_DISPOSITIONS = {"N/A", "ACE_EQUIV"}
REQUIRED_STRONGER_NEGATIVE = {
    "ARG0-002",
    "CLOUD-001",
    "HOOK-001",
    "LNX-017",
    "NET-027",
    "WIN-021",
    "WIN-024",
}
REQUIRED_APPLICABLE_PRODUCT_SURFACES = {
    "PLUG-001",
    "PLUG-002",
    "PLUG-003",
}
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".rs"}


def _numbered_ids(prefix: str, first: int, last: int) -> set[str]:
    return {f"{prefix}-{number:03d}" for number in range(first, last + 1)}


T01_EXPECTED_DISPOSITIONS = {
    "N/A": set(
        {
            "ARCH-013",
            "ARG0-001",
            "ESCAL-001",
            "PROC-006",
            "NET-026",
            "NET-030",
            "AGID-003",
            "AWS-001",
            "CHAT-001",
            "RAP-001",
            "MCP-019",
            "IPC-002",
            "IPC-012",
            "IPC-015",
            "REMOTE-001",
            "REMOTE-002",
            "REMOTE-005",
            "REMOTE-006",
            "UDS-002",
            "PROD-001",
            "PROD-002",
            "PROD-009",
            "PROD-010",
        }
    ).union(
        _numbered_ids("MCFG", 1, 5),
        _numbered_ids("EXEC", 15, 16),
        _numbered_ids("WID", 1, 4),
        _numbered_ids("CMODE", 1, 10),
        _numbered_ids("MSRV", 1, 5),
    ),
    "ACE_EQUIV": {
        "AGID-001",
        "AGID-002",
        "AGID-004",
        "AGID-005",
        "REMOTE-003",
        "REMOTE-004",
        "UDS-001",
        "UPD-001",
        "UPD-003",
        "DATA-002",
        "PROD-003",
        "PROD-004",
        "PROD-005",
        "PROD-006",
        "PROD-007",
        "PROD-008",
        "PROD-011",
        "PROD-012",
    },
    "APPLICABLE": {"CLOUD-001"},
}


def _load_inventory() -> dict[str, Any]:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def _baseline_ids() -> set[str]:
    return set(BASELINE_ID_RE.findall(BASELINE.read_text(encoding="utf-8")))


def _baseline_statuses() -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        match = BASELINE_ID_RE.match(line)
        if match:
            statuses[match.group(1)] = line.split("|")[8].strip()
    return statuses


def _flatten_groups(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (capability_id, group)
        for group in data["disposition_groups"]
        for capability_id in group["ids"]
    ]


def _assert_nonempty_strings(value: object, field: str) -> None:
    assert isinstance(value, list) and value, f"{field} 必须是非空数组"
    assert all(isinstance(item, str) and item.strip() for item in value), (
        f"{field} 只能包含非空字符串"
    )


def test_inventory_schema_and_436_id_ledger() -> None:
    data = _load_inventory()
    assert data["schema_version"] == 1

    baseline = data["baseline"]
    assert baseline == {
        "path": "docs/security/codex-security-capability-baseline.md",
        "expected_id_count": 436,
        "default_disposition": "APPLICABLE",
        "override_id_count": 70,
        "na_id_count": 49,
        "ace_equiv_id_count": 21,
    }

    baseline_ids = _baseline_ids()
    assert len(baseline_ids) == baseline["expected_id_count"]

    groups = data["disposition_groups"]
    assert isinstance(groups, list) and groups
    group_names = [group.get("name") for group in groups]
    assert len(group_names) == len(set(group_names))

    rule_ids = {rule["id"] for rule in data["absence_rules"]}
    assert len(rule_ids) == len(data["absence_rules"])

    flattened = _flatten_groups(data)
    override_ids = [capability_id for capability_id, _group in flattened]
    duplicates = sorted(
        capability_id for capability_id, count in Counter(override_ids).items() if count > 1
    )
    assert not duplicates, f"适用性覆盖 ID 重复: {duplicates}"
    assert set(override_ids) <= baseline_ids
    assert not (REQUIRED_APPLICABLE_PRODUCT_SURFACES & set(override_ids)), (
        "Ace 已有对应生产入口的产品面不得标为 N/A/ACE_EQUIV"
    )

    counts = Counter(group["disposition"] for _capability_id, group in flattened)
    assert counts == Counter({"N/A": 49, "ACE_EQUIV": 21})
    assert len(override_ids) == baseline["override_id_count"]

    for group in groups:
        assert set(group) == {
            "name",
            "surface",
            "disposition",
            "ids",
            "reason",
            "evidence",
            "forbidden_symbol_rules",
            "equivalent_ace_ids",
            "review_triggers",
        }
        assert isinstance(group["name"], str) and group["name"].strip()
        assert isinstance(group["surface"], str) and group["surface"].strip()
        assert group["disposition"] in VALID_DISPOSITIONS
        _assert_nonempty_strings(group["ids"], f"{group['name']}.ids")
        assert isinstance(group["reason"], str) and group["reason"].strip()
        _assert_nonempty_strings(group["evidence"], f"{group['name']}.evidence")
        _assert_nonempty_strings(
            group["review_triggers"], f"{group['name']}.review_triggers"
        )
        assert isinstance(group["forbidden_symbol_rules"], list)
        assert set(group["forbidden_symbol_rules"]) <= rule_ids

        equivalents = group["equivalent_ace_ids"]
        assert isinstance(equivalents, list)
        assert len(equivalents) == len(set(equivalents))
        if group["disposition"] == "N/A":
            assert not equivalents
            assert group["forbidden_symbol_rules"], (
                f"{group['name']} 标为 N/A 时必须有可执行的缺面禁符号规则"
            )
        else:
            _assert_nonempty_strings(
                equivalents, f"{group['name']}.equivalent_ace_ids"
            )
            assert all(item.startswith("ACE-") for item in equivalents)
            assert set(equivalents) <= baseline_ids

    # Every baseline ID has exactly one effective disposition: an explicit
    # boundary override or the fail-safe APPLICABLE default.
    ledger = dict.fromkeys(baseline_ids, baseline["default_disposition"])
    for capability_id, group in flattened:
        ledger[capability_id] = group["disposition"]
    assert len(ledger) == 436
    assert set(ledger) == baseline_ids
    assert Counter(ledger.values()) == Counter(
        {"APPLICABLE": 366, "N/A": 49, "ACE_EQUIV": 21}
    )


def test_t01_scope_disposes_all_60_missing_ids_without_hiding_weak_failure() -> None:
    data = _load_inventory()
    scope = data["task_scopes"]["T01"]
    assert set(scope) == {
        "baseline_status_counts",
        "expected_dispositions",
        "notes",
    }
    assert scope["baseline_status_counts"] == {"欠缺": 60, "不全面": 8}
    assert isinstance(scope["notes"], list) and scope["notes"]

    expected = {
        disposition: set(ids)
        for disposition, ids in scope["expected_dispositions"].items()
    }
    assert expected == T01_EXPECTED_DISPOSITIONS
    t01_ids = set().union(*expected.values())
    assert len(t01_ids) == 68

    baseline_ids = _baseline_ids()
    statuses = _baseline_statuses()
    assert t01_ids <= baseline_ids
    assert {statuses[capability_id] for capability_id in t01_ids} == {
        "欠缺",
        "不全面",
    }
    assert Counter(statuses[capability_id] for capability_id in t01_ids) == Counter(
        {"欠缺": 60, "不全面": 8}
    )

    effective = dict.fromkeys(t01_ids, data["baseline"]["default_disposition"])
    for capability_id, group in _flatten_groups(data):
        if capability_id in effective:
            effective[capability_id] = group["disposition"]
    assert {effective[capability_id] for capability_id in t01_ids} == {
        "APPLICABLE",
        "N/A",
        "ACE_EQUIV",
    }
    for disposition, capability_ids in expected.items():
        assert {capability_id for capability_id in t01_ids if effective[capability_id] == disposition} == capability_ids

    missing_ids = {
        capability_id for capability_id in t01_ids if statuses[capability_id] == "欠缺"
    }
    assert len(missing_ids) == 60
    assert not (missing_ids & expected["APPLICABLE"])
    assert effective["CLOUD-001"] == "APPLICABLE"
    assert "CLOUD-001" in {
        entry["id"] for entry in data["required_stronger_negative"]
    }


def test_codex_weak_failures_remain_applicable_and_require_stronger_negatives() -> None:
    data = _load_inventory()
    baseline_ids = _baseline_ids()
    override_ids = {capability_id for capability_id, _group in _flatten_groups(data)}
    entries = data["required_stronger_negative"]

    registered_ids = [entry.get("id") for entry in entries]
    assert len(registered_ids) == len(set(registered_ids))
    assert REQUIRED_STRONGER_NEGATIVE <= set(registered_ids)
    assert set(registered_ids) <= baseline_ids
    assert not (set(registered_ids) & override_ids), (
        "Codex 弱失败项不得用 N/A/ACE_EQUIV 从 Ace 负测责任中移除"
    )

    for entry in entries:
        assert set(entry) == {
            "id",
            "reason",
            "required_negative_test",
            "ace_guard_ids",
        }
        assert isinstance(entry["reason"], str) and entry["reason"].strip()
        assert (
            isinstance(entry["required_negative_test"], str)
            and entry["required_negative_test"].strip()
        )
        _assert_nonempty_strings(entry["ace_guard_ids"], f"{entry['id']}.ace_guard_ids")
        assert all(item.startswith("ACE-") for item in entry["ace_guard_ids"])
        assert set(entry["ace_guard_ids"]) <= baseline_ids


@cache
def _source_files(relative_root: str) -> tuple[Path, ...]:
    root = ROOT / relative_root
    assert root.exists(), f"禁符号扫描根不存在: {relative_root}"
    return tuple(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
    )


def _rule_files(rule: dict[str, Any]) -> list[Path]:
    return sorted(
        {
            path
            for relative_root in rule["roots"]
            for path in _source_files(relative_root)
        }
    )


@cache
def _source_lines(path: Path) -> tuple[str, ...]:
    return tuple(path.read_text(encoding="utf-8-sig", errors="replace").splitlines())


def test_forbidden_product_entrypoints_are_absent_with_exact_session_mcp_exception() -> None:
    data = _load_inventory()

    for rule in data["absence_rules"]:
        assert set(rule) == {
            "id",
            "description",
            "roots",
            "patterns",
            "allowed_matches",
        }
        _assert_nonempty_strings(rule["roots"], f"{rule['id']}.roots")
        _assert_nonempty_strings(rule["patterns"], f"{rule['id']}.patterns")
        assert isinstance(rule["allowed_matches"], list)

        allowed_counts = Counter()
        unexpected: list[str] = []
        compiled = [re.compile(pattern, re.IGNORECASE) for pattern in rule["patterns"]]
        for path in _rule_files(rule):
            relative = path.relative_to(ROOT).as_posix()
            for line_number, line in enumerate(_source_lines(path), start=1):
                if not any(pattern.search(line) for pattern in compiled):
                    continue
                matched_allow = False
                for allowed in rule["allowed_matches"]:
                    if (
                        relative == allowed["path"]
                        and re.fullmatch(allowed["line_regex"], line)
                    ):
                        allowed_counts[allowed["name"]] += 1
                        matched_allow = True
                        break
                if not matched_allow:
                    unexpected.append(f"{relative}:{line_number}: {line.strip()}")

        assert not unexpected, (
            f"{rule['id']} 出现未登记产品入口/禁符号:\n" + "\n".join(unexpected)
        )
        expected_counts = {
            allowed["name"]: allowed["expected_count"]
            for allowed in rule["allowed_matches"]
        }
        assert dict(allowed_counts) == expected_counts

    # The two allowed servers are session/interaction facades, not a Codex-style
    # inbound execution API. Keep caller-controlled execution-policy fields out
    # of every @mcp.tool schema in the exact allowlisted module.
    mcp_path = ROOT / "crew" / "gateway" / "mcp_server.py"
    tree = ast.parse(mcp_path.read_text(encoding="utf-8"), filename=str(mcp_path))
    forbidden_parameters = {
        "model",
        "cwd",
        "approval",
        "approval_policy",
        "sandbox",
        "sandbox_policy",
        "instructions",
        "analytics",
        "config",
        "config_overrides",
    }
    exposed_forbidden: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_mcp_tool = any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "mcp"
            for decorator in node.decorator_list
        )
        if not is_mcp_tool:
            continue
        parameters = {
            argument.arg
            for argument in (
                node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            )
        }
        for parameter in sorted(parameters & forbidden_parameters):
            exposed_forbidden.append(f"{node.name}({parameter}=...)")
    assert not exposed_forbidden, (
        "Ace 白名单 session/interaction MCP 不得扩展成 caller-controlled "
        f"Codex 执行配置入口: {exposed_forbidden}"
    )
