"""WorkReference 的 browser_tab 类型与 snapshot_summary 透传测试。"""

from __future__ import annotations

import pytest

from crew.state.session_store import SQLiteSessionStore, SessionIncarnationChanged

from crew.work.items import WorkItemStore
from crew.work.models import ProductMode
from crew.work.references import ReferenceType, WorkReferenceStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "crew.db"
    # work_session_links/work_references 对 work_items 有外键，需同库建表。
    items = WorkItemStore(db_path)
    sessions = SQLiteSessionStore(str(tmp_path / "sessions.db"))
    sessions.ensure_session("sess-1", owner_account_id="owner")
    reference_store = WorkReferenceStore(db_path, session_store=sessions)
    reference_store.link_session(
        owner_account_id="owner",
        session_id="sess-1",
        product_mode=ProductMode.WORK,
    )
    yield reference_store
    reference_store.close()
    items.close()
    sessions.close()


def test_delayed_binding_cannot_replace_another_sessions_references(tmp_path, monkeypatch):
    work_db = tmp_path / "work.db"
    main_db = str(tmp_path / "sessions.db")
    sessions = SQLiteSessionStore(main_db)
    replacement_sessions = SQLiteSessionStore(main_db)
    items = WorkItemStore(work_db)
    refs = WorkReferenceStore(work_db, session_store=sessions)
    replacement_refs = WorkReferenceStore(work_db, session_store=replacement_sessions)
    owner, session_id = "owner", "same-id"
    try:
        old = sessions.ensure_session_incarnation(session_id, owner_account_id=owner)
        write = refs._writer.execute

        def allow_replacement_then_write(callback):
            replacement_sessions.clear(session_id, owner_account_id=owner, expected_incarnation=old)
            new = replacement_sessions.ensure_session_incarnation(session_id, owner_account_id=owner)
            assert new != old
            replacement_refs.link_session(
                owner_account_id=owner, session_id=session_id, product_mode="work",
                expected_incarnation=new,
            )
            replacement_refs.create_reference(
                owner_account_id=owner, target_session_id=session_id,
                reference_type="browser_tab", source_id="replacement-tab",
            )
            return write(callback)

        monkeypatch.setattr(refs._writer, "execute", allow_replacement_then_write)
        with pytest.raises(SessionIncarnationChanged):
            refs.link_session(
                owner_account_id=owner, session_id=session_id, product_mode="work",
                expected_incarnation=old,
            )
        assert [item.source_id for item in refs.list_references(owner, session_id)] == ["replacement-tab"]
    finally:
        refs.close()
        replacement_refs.close()
        items.close()
        sessions.close()
        replacement_sessions.close()


def test_browser_tab_reference_round_trip(store):
    """browser_tab 类型可创建并落库，标题/URL 走 snapshot_summary/source_link。"""
    ref = store.create_reference(
        owner_account_id="owner",
        target_session_id="sess-1",
        reference_type="browser_tab",
        source_id="s0123-1",
        source_link="https://example.com/page",
        snapshot_summary="示例页面标题",
    )

    assert ref.reference_type is ReferenceType.BROWSER_TAB
    assert ref.source_link == "https://example.com/page"
    assert ref.snapshot_summary == "示例页面标题"

    loaded = store.get_reference("owner", ref.reference_id)
    assert loaded.reference_type is ReferenceType.BROWSER_TAB
    assert loaded.snapshot_summary == "示例页面标题"
    assert loaded.source_link == "https://example.com/page"

    listed = store.list_references("owner", "sess-1")
    assert [item.reference_id for item in listed] == [ref.reference_id]


def test_browser_tab_reference_type_value():
    assert ReferenceType("browser_tab") is ReferenceType.BROWSER_TAB
    assert ReferenceType.BROWSER_TAB.value == "browser_tab"


def test_browser_tab_reference_defaults(store):
    """不传 snapshot_summary/source_link 时保持空串默认，兼容既有调用方。"""
    ref = store.create_reference(
        owner_account_id="owner",
        target_session_id="sess-1",
        reference_type=ReferenceType.BROWSER_TAB,
        source_id="s0123-2",
    )
    assert ref.snapshot_summary == ""
    assert ref.source_link == ""
