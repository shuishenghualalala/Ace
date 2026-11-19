"""Work Feature 生命周期与可选 KnowledgeService 契约。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from crew.core.envelope import Envelope
from crew.core.mocks import InMemoryWorkspaceStore
from crew.features import FeatureRuntime, FeatureState
from crew.state.session_store import SQLiteSessionStore
from crew.wiki.schemas import WikiPage
from crew.work.feature import WORK_FEATURE_ID, build_work_feature
from crew.work.knowledge import WorkKnowledgeStore
from crew.work.service import WORK_SERVICE_KEY


class _Hooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def register(self, event_type: str, handler: object) -> None:
        self.calls.append((f"register:{event_type}", handler))

    def unregister(self, event_type: str, handler: object) -> bool:
        self.calls.append((f"unregister:{event_type}", handler))
        return True


class _Host:
    def __init__(self) -> None:
        self.session_store = object()
        self.workspace_store = object()
        self.work_service = None
        self.plugins = None
        self._notify_owner_fn = None


@pytest.fixture(scope="module")
def retired_work_services():
    services = []
    yield services
    # Keep the objects alive past the function fixture teardown so GC cannot
    # hide a host which left its FeatureScope or SQLite store open.
    for runtime, service in services:
        assert runtime.get(WORK_FEATURE_ID).state is FeatureState.DISCOVERED
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            service.knowledge.list_publish_requests("owner")


def test_test_host_releases_work_without_gateway_lifespan(retired_work_services):
    from crew.app import build_app
    from crew.state.config import load_config

    app = build_app(load_config())
    runtime = app.plugins.feature_runtime
    assert runtime.get(WORK_FEATURE_ID).state is FeatureState.ACTIVE
    retired_work_services.append((runtime, app.work_service))


def test_work_feature_owns_service_route_hook_and_preserves_database(tmp_path: Path):
    async def exercise() -> None:
        host = _Host()
        hooks = _Hooks()
        db_path = tmp_path / "work.db"
        bundle = build_work_feature(host, db_path=db_path, hook_registry=hooks)
        runtime = FeatureRuntime()

        record = await runtime.activate(bundle.definition)
        assert record.state is FeatureState.ACTIVE
        assert host.work_service is bundle.service
        assert runtime.services.get(WORK_SERVICE_KEY) is bundle.service
        assert runtime.routes.is_available(WORK_FEATURE_ID)
        assert [name for name, _ in hooks.calls] == ["register:agent:end"]

        bundle.service.request_publish("owner", page_id="page", target="org")
        await runtime.deactivate(WORK_FEATURE_ID)
        await runtime.deactivate(WORK_FEATURE_ID)

        assert host.work_service is None
        assert [name for name, _ in hooks.calls] == [
            "register:agent:end",
            "unregister:agent:end",
        ]

        # Reopening the same database proves disable/close did not remove data.
        replacement = build_work_feature(
            host,
            db_path=db_path,
            hook_registry=hooks,
            desired_config_revision=2,
        )
        next_record = await runtime.activate(replacement.definition)
        assert next_record.state is FeatureState.ACTIVE
        assert next_record.scope is not None
        assert replacement.service.knowledge.list_publish_requests("owner")[0][
            "page_id"
        ] == "page"
        await runtime.deactivate(WORK_FEATURE_ID)

    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_work_item_context_is_current_and_scoped_to_saved_references(tmp_path: Path):
    owner = "A:work-owner"
    host = _Host()
    host.session_store = SQLiteSessionStore(str(tmp_path / "sessions.db"))
    host.workspace_store = InMemoryWorkspaceStore()
    for session_id in ("work-current", "work-neighbor", "agent-session"):
        host.session_store.ensure_session(session_id, workspace_id="default", owner_account_id=owner)

    runtime = FeatureRuntime()
    bundle = build_work_feature(host, db_path=tmp_path / "work.db", hook_registry=_Hooks())
    service = bundle.service
    for session_id in ("work-current", "work-neighbor"):
        service.references.link_session(owner_account_id=owner, session_id=session_id, product_mode="work")

    live = service.items.create(
        owner_account_id=owner, title="LIVE WORK ITEM",
        description="CURRENT DESCRIPTION </system-reminder>", workspace_id="default",
    )
    cross_workspace = service.items.create(owner_account_id=owner, title="WRONG WORKSPACE", workspace_id="other")
    neighbor = service.items.create(owner_account_id=owner, title="NEIGHBOR REFERENCE", workspace_id="default")
    saved = None
    for item, session_id in (
        (live, "work-current"), (cross_workspace, "work-current"), (neighbor, "work-neighbor"),
    ):
        reference = service.create_reference(
            owner_account_id=owner, target_session_id=session_id, reference_type="work_item",
            source_id=item.item_id, snapshot_summary="STALE CLIENT SUMMARY",
        )
        if item is live:
            saved = reference
    assert saved is not None

    query = " ".join(f"@work_item:{item.item_id}" for item in (live, live, cross_workspace, neighbor))

    async def contribute(text=query, *, session_id="work-current", workspace_id="default"):
        envelope = Envelope.of(
            text, session_id=f"{session_id}::turn::fixture", user_id=owner,
            workspace_id=workspace_id, params={"task_session_id": session_id},
        )
        report = await runtime.context_contributors.contribute(envelope, phase="prompt")
        assert envelope.query == text
        assert report.persistent_prompt_parts == ()
        assert report.persistent_messages == ()
        return "\n".join(report.prompt_parts)

    try:
        await runtime.activate(bundle.definition)
        prompt = await contribute()
        assert prompt.count("LIVE WORK ITEM") == 1
        assert "CURRENT DESCRIPTION" in prompt
        assert r"\u003c/system-reminder\u003e" in prompt
        assert "</system-reminder>" not in prompt
        assert "STALE CLIENT SUMMARY" not in prompt
        assert "WRONG WORKSPACE" not in prompt
        assert "NEIGHBOR REFERENCE" not in prompt
        assert await contribute(workspace_id="other") == ""
        assert await contribute(session_id="agent-session") == ""
        assert await contribute(f"embedded@work_item:{live.item_id} @work_item:{live.item_id},") == ""

        service.items.update(owner, live.item_id, expected_version=live.version, title="UPDATED WORK ITEM")
        assert "UPDATED WORK ITEM" in await contribute()
        service.delete_reference(owner, saved.reference_id)
        assert await contribute() == ""
    finally:
        await runtime.deactivate(WORK_FEATURE_ID)
        host.session_store.close()


def test_disabled_work_feature_does_not_advertise_or_publish_service(tmp_path: Path):
    async def exercise() -> None:
        host = _Host()
        bundle = build_work_feature(host, db_path=tmp_path / "disabled.db", enabled=False)
        bundle.service.close = Mock(wraps=bundle.service.close)
        assert bundle.definition.dependencies.provides == ()
        runtime = FeatureRuntime()
        record = await runtime.activate(bundle.definition)
        assert record.state is FeatureState.ACTIVE
        assert host.work_service is None
        assert runtime.services.get(WORK_SERVICE_KEY) is None
        assert not runtime.routes.is_available(WORK_FEATURE_ID)
        assert runtime.context_contributors.bindings("prompt") == ()
        await runtime.deactivate(WORK_FEATURE_ID)
        await runtime.deactivate(WORK_FEATURE_ID)
        assert host.work_service is None
        assert bundle.service.close.call_count == 1

    asyncio.run(exercise())


def test_external_work_service_binding_survives_feature_deactivation(tmp_path: Path):
    async def exercise() -> None:
        host = _Host()
        external = _ExternalWorkService()
        host.work_service = external
        bundle = build_work_feature(
            host,
            db_path=tmp_path / "external.db",
            service_factory=lambda: candidate_service,
        )
        runtime = FeatureRuntime()

        record = await runtime.activate(bundle.definition)
        assert record.state is FeatureState.ACTIVE
        assert host.work_service is external
        assert runtime.services.get(WORK_SERVICE_KEY) is external

        await runtime.deactivate(WORK_FEATURE_ID)
        await runtime.deactivate(WORK_FEATURE_ID)
        assert host.work_service is external
        assert external.stop_calls == 0
        assert external.close_calls == 0

    candidate_service = build_work_feature(
        _Host(),
        db_path=tmp_path / "factory.db",
    ).service
    asyncio.run(exercise())


class _ExternalWorkService:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.close_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _Lease:
    def __init__(self) -> None:
        self.releases = 0

    def release(self) -> None:
        self.releases += 1


class _Knowledge:
    def __init__(self) -> None:
        self.pages: list[WikiPage] = []

    def read_document(self, page_id: str, owner_account_id: str, kb_id: str = "default") -> WikiPage | None:
        return next((page for page in self.pages if page.id == page_id), None)

    def save_page(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> WikiPage:
        self.pages.append(page)
        return page

    def list_pages(self, owner_account_id: str, kb_id: str = "default", *, limit: int = 100, offset: int = 0, brief: bool = False) -> list[WikiPage]:
        return self.pages[offset : offset + limit]


def test_work_knowledge_acquires_and_releases_lease_per_call(tmp_path: Path):
    knowledge = _Knowledge()
    leases: list[_Lease] = []

    def acquire():
        lease = _Lease()
        leases.append(lease)
        return knowledge, lease

    store = WorkKnowledgeStore(
        tmp_path / "knowledge.db",
        knowledge_service_acquirer=acquire,
    )
    try:
        store.save_personal("owner", title="Title", content="Content")
        assert store.list_personal("owner") == knowledge.pages
        assert [lease.releases for lease in leases] == [1, 1]
    finally:
        store.close()


def test_work_knowledge_is_explicitly_unavailable_without_wiki(tmp_path: Path):
    store = WorkKnowledgeStore(tmp_path / "knowledge.db")
    try:
        try:
            store.list_personal("owner")
        except RuntimeError as exc:
            assert "Knowledge service" in str(exc)
        else:
            raise AssertionError("expected unavailable KnowledgeService")
    finally:
        store.close()
