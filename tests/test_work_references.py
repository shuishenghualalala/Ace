"""WorkReference 的 browser_tab 类型与 snapshot_summary 透传测试。"""

from __future__ import annotations

import pytest

from crew.work.items import WorkItemStore
from crew.work.models import ProductMode
from crew.work.references import ReferenceType, WorkReferenceStore


class _FakeSessionStore:
    def session_belongs_to(self, session_id: str, owner_account_id: str) -> bool:
        return True

    def load(self, session_id: str, owner_account_id: str = "") -> list:
        return []


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "crew.db"
    # work_session_links/work_references 对 work_items 有外键，需同库建表。
    items = WorkItemStore(db_path)
    reference_store = WorkReferenceStore(
        db_path,
        session_store=_FakeSessionStore(),
    )
    reference_store.link_session(
        owner_account_id="owner",
        session_id="sess-1",
        product_mode=ProductMode.WORK,
    )
    yield reference_store
    reference_store.close()
    items.close()


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
