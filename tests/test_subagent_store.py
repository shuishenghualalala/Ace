"""Subagent 自有三表（runs/edges/descriptors）持久层契约测试（阶段 B1/B2）。"""

from __future__ import annotations

import time

import pytest

from crew.agent.subagent.store import (
    DESCRIPTOR_VERSION,
    SUBAGENT_SCHEMA_FEATURE,
    SubagentStore,
    validate_descriptor_payload,
)


@pytest.fixture()
def store(tmp_path):
    instance = SubagentStore(tmp_path / "subagent.db")
    yield instance
    instance.close()


def _descriptor(subagent_id: str = "sub::a::1", **overrides) -> dict:
    payload = {
        "version": DESCRIPTOR_VERSION,
        "lifecycle": "one_shot",
        "provider": "inprocess-spawn",
        "parent_session_id": "parent-1",
        "label": "a",
        "model": None,
        "persona": None,
        "tool_filter": [],
        "delegation_depth": 1,
        "subagent_id": subagent_id,
    }
    payload.update(overrides)
    return payload


def test_schema_version_stamp_is_idempotent(store, tmp_path):
    # 同库重复构造（重启模拟）不抬版本、不炸 schema
    reopened = SubagentStore(tmp_path / "subagent.db")
    reopened.close()


def test_descriptor_write_is_first_wins(store):
    assert store.write_descriptor(_descriptor(model="first")) is True
    # 同名后续声明不能改写出生证明
    assert store.write_descriptor(_descriptor(model="hijack")) is False
    assert store.read_descriptor("sub::a::1")["model"] == "first"


def test_descriptor_rejects_unknown_fields_and_wrong_version(store):
    with pytest.raises(ValueError, match="unknown fields"):
        store.write_descriptor(_descriptor(sandbox="unrestricted"))
    with pytest.raises(ValueError, match="version"):
        store.write_descriptor(_descriptor(version=99))
    # 读侧同样拒绝：库里被污染的超前版本不静默进入运行时
    import json

    store._writer.execute(
        lambda conn: conn.execute(
            "INSERT INTO subagent_descriptors VALUES(?, ?, ?, ?)",
            ("sub::bad::1", 99, json.dumps({"version": 99, "oops": 1}), time.time()),
        )
    )
    with pytest.raises(ValueError):
        store.read_descriptor("sub::bad::1")


def test_run_lifecycle_persists_and_settles(store):
    events = [{"event": "run/start", "data": {"depth": 1}}]
    store.record_run_start(
        subagent_id="sub::a::1",
        parent_session_id="parent-1",
        owner_account_id="local",
        workspace_id="default",
        depth=1,
        mode="one_shot",
        provider="inprocess-spawn",
        label="a",
        events=events,
    )
    store.settle_run(
        "sub::a::1",
        stop_reason="completed",
        events=[
            *events,
            {"event": "run/settle", "data": {"stop_reason": "completed"}},
        ],
    )
    run = store.get_run("sub::a::1")
    assert run is not None
    assert run["status"] == "settled"
    assert run["stop_reason"] == "completed"
    assert run["run_log"][0]["event"] == "run/start"
    assert store.list_runs(parent_session_id="parent-1", owner_account_id="local")


def test_edges_are_soft_deleted_and_child_unique(store):
    store.open_edge("sub::a::1", "parent-1")
    store.open_edge("sub::a::1", "parent-other")  # 同 child 二次开边幂等
    edge = store.get_edge("sub::a::1")
    assert edge["parent_id"] == "parent-1"
    assert edge["status"] == "open"

    store.close_edge("sub::a::1")
    store.close_edge("sub::a::1")  # 幂等
    edge = store.get_edge("sub::a::1")
    assert edge["status"] == "closed"
    assert edge["closed_at"] is not None


def test_purge_expired_removes_runs_descriptors_edges(store):
    now = time.time()
    for index in range(3):
        sid = f"sub::a::{index}"
        store.record_run_start(
            subagent_id=sid,
            parent_session_id="parent-1",
            owner_account_id="local",
            workspace_id=None,
            depth=1,
            mode="one_shot",
            provider="inprocess-spawn",
            label="a",
        )
        store.write_descriptor(_descriptor(sid))
        store.open_edge(sid, "parent-1")
    # 两条已结算超过保留期，一条仍在保留期内
    old = now - 40 * 86400
    store._writer.execute(
        lambda conn: conn.executemany(
            "UPDATE subagent_runs SET settled_at = ? WHERE subagent_id = ?",
            [(old, "sub::a::0"), (old, "sub::a::1")],
        )
    )
    removed = store.purge_expired(30, now=now)
    assert removed == 2
    assert store.get_run("sub::a::0") is None
    assert store.read_descriptor("sub::a::1") is None
    assert store.get_edge("sub::a::0") is None
    assert store.get_run("sub::a::2") is not None


def test_restart_keeps_rows_readable(tmp_path):
    path = tmp_path / "subagent.db"
    first = SubagentStore(path)
    first.record_run_start(
        subagent_id="sub::a::1",
        parent_session_id="parent-1",
        owner_account_id="local",
        workspace_id=None,
        depth=2,
        mode="one_shot",
        provider="inprocess-spawn",
        label="a",
    )
    first.write_descriptor(_descriptor())
    first.open_edge("sub::a::1", "parent-1")
    first.close()

    second = SubagentStore(path)
    assert second.get_run("sub::a::1")["depth"] == 2
    assert second.read_descriptor("sub::a::1")["subagent_id"] == "sub::a::1"
    assert second.get_edge("sub::a::1")["status"] == "open"
    second.close()


def test_validate_descriptor_payload_is_defensive():
    payload = validate_descriptor_payload(_descriptor())
    assert payload["version"] == DESCRIPTOR_VERSION
    with pytest.raises(ValueError, match="mapping"):
        validate_descriptor_payload("not-a-mapping")  # type: ignore[arg-type]


def test_schema_feature_registered_in_migration_registry():
    from crew.state._migration import SUBAGENT_DB_TABLES

    assert set(SUBAGENT_DB_TABLES) == {
        "subagent_runs",
        "subagent_edges",
        "subagent_descriptors",
        "subagent_messages",
    }
    assert SUBAGENT_SCHEMA_FEATURE == "subagent"


def test_continuable_inbox_survives_store_reopen(tmp_path):
    path = tmp_path / "subagent.db"
    first = SubagentStore(path)
    message_id = first.enqueue_message(
        subagent_id="sub::a::1",
        parent_session_id="parent-1",
        owner_account_id="local",
        body="queued",
    )
    assert message_id.startswith("msg::")
    assert first.has_pending_messages("sub::a::1", owner_account_id="local")
    first.close()

    second = SubagentStore(path)
    rows = second.take_messages(
        subagent_id="sub::a::1",
        parent_session_id="parent-1",
        owner_account_id="local",
    )
    assert [row["body"] for row in rows] == ["queued"]
    assert not second.has_pending_messages("sub::a::1", owner_account_id="local")
    second.close()
