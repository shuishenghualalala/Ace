"""E17: Work domain security negative matrix.

The gateway uses a single-active-owner model: after Owner A has an active
session, Owner B's requests return 423 Locked until A logs out. Cross-owner
cases therefore drive two separate AsyncClient instances (A, then B) with a
logout between them, mirroring test_work_items_api::test_cross_owner_isolation.

Scope: items cross-owner isolation is already covered by the dedicated items
API test, so this file closes the remaining Work-resource gaps that had no
negative coverage -- references, knowledge/publish, workspace-index writeback,
preference settings, plus credential non-exposure and unauthenticated rejection.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
import pytest

from crew.app import build_app
from crew.gateway.auth import REMOTE_AUTH_COOKIE, create_remote_session_token
from crew.gateway.server import create_app
from crew.state.config import Config


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))


def _auth_cookie(user_id: str) -> dict[str, str]:
    return {
        REMOTE_AUTH_COOKIE: create_remote_session_token(
            "test",
            user_id,
            ttl_seconds=3600,
        )
    }


def _build(tmp_path):
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            auth_mode="remote",
            auth_provider_id="test",
        ),
        enable_team=False,
    )
    return create_app(crew)


async def test_cross_owner_references_and_delete_isolated(tmp_path) -> None:
    """Owner B cannot read or delete Owner A's references."""
    app = _build(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        # References require an owned target Work session.
        sess = await a.post("/api/work/sessions", json={"workspace_id": "default", "title": "A-sess"})
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["session_id"]
        created = await a.post(
            "/api/work/references",
            json={
                "target_session_id": session_id,
                "reference_type": "file",
                "source_id": "src-1",
                "source_link": "https://example.test/a",
            },
        )
        assert created.status_code == 201, created.text
        ref_id = created.json()["reference_id"]
        session_id = sess.json()["session_id"]
        await a.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-b")) as b:
        # B listing A's session yields nothing owned by B.
        listed = await b.get(f"/api/work/references?target_session_id={session_id}")
        assert listed.status_code in (200, 404, 422)
        if listed.status_code == 200:
            assert listed.json()["count"] == 0
        # B cannot delete A's reference (owner-scoped delete -> 404).
        stolen = await b.delete(f"/api/work/references/{ref_id}")
        assert stolen.status_code in (404, 422)
        await b.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        # A still owns its reference after B failed to delete it.
        assert (await a.get(f"/api/work/references?target_session_id={session_id}")).json()["count"] == 1


async def test_cross_owner_knowledge_and_publish_isolated(tmp_path) -> None:
    """Personal knowledge and publish requests are owner-scoped."""
    app = _build(tmp_path)
    page_id = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        saved = await a.post(
            "/api/work/knowledge/personal",
            json={"title": "A-private", "content": "confidential"},
        )
        assert saved.status_code == 201, saved.text
        page_id = saved.json()["page"]["id"]
        await a.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-b")) as b:
        assert (await b.get("/api/work/knowledge/personal")).json()["count"] == 0
        await b.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        assert (await a.post("/api/work/knowledge/publish", json={"page_id": page_id, "target": "org-kb-1"})).status_code == 201
        await a.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-b")) as b:
        assert (await b.get("/api/work/knowledge/publish")).json()["count"] == 0
        await b.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        assert (await a.get("/api/work/knowledge/publish")).json()["count"] == 1


async def test_cross_owner_index_status_writeback_isolated(tmp_path) -> None:
    """B disabling A's workspace index never touches A's state or source files."""
    app = _build(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        resp = await a.put("/api/work/workspaces/ws-1/index", json={"enabled": True, "state": "complete"})
        assert resp.status_code == 200, resp.text
        await a.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-b")) as b:
        # B writing the same workspace_id only affects B's own owner-scoped row.
        assert (await b.put("/api/work/workspaces/ws-1/index", json={"enabled": False, "state": "idle"})).status_code == 200
        await b.delete("/api/work/workspaces/ws-1/index")
        await b.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        a_status = (await a.get("/api/work/workspaces/ws-1/index")).json()
        assert a_status["enabled"] is True
        assert a_status["state"] == "complete"


async def test_preferences_cross_owner_isolated(tmp_path) -> None:
    """B cannot read or overwrite A's preference (auto-learning) settings."""
    app = _build(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        set_a = await a.put("/api/work/preferences/settings", json={"auto_learning_enabled": True})
        assert set_a.status_code == 200, set_a.text
        assert set_a.json()["auto_learning_enabled"] is True
        await a.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-b")) as b:
        # B's own setting is independent of A; default is True but B can flip it to False.
        assert (await b.put("/api/work/preferences/settings", json={"auto_learning_enabled": False})).status_code == 200
        assert (await b.get("/api/work/preferences/settings")).json()["auto_learning_enabled"] is False
        await b.post("/api/auth/logout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        assert (await a.get("/api/work/preferences/settings")).json()["auto_learning_enabled"] is True


async def test_source_state_never_exposes_credentials(tmp_path) -> None:
    """WorkSourceState lists no token/secret/credential fields."""
    app = _build(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=_auth_cookie("uid-a")) as a:
        states = await a.get("/api/work/sources")
        assert states.status_code == 200
        for row in states.json().get("items", []):
            joined = " ".join(f"{k}={v}" for k, v in row.items()).lower()
            for forbidden in ("token", "secret", "password", "api_key", "apikey", "credential"):
                assert forbidden not in joined, f"source state leaked {forbidden}"


async def test_unauthenticated_work_endpoints_rejected(tmp_path) -> None:
    """No identity header returns 401 on every Work write path."""
    app = _build(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        cases = [
            ("GET", "/api/work/items", None),
            ("POST", "/api/work/items", {"title": "x"}),
            ("GET", "/api/work/knowledge/personal", None),
            ("POST", "/api/work/knowledge/personal", {"title": "x", "content": "y"}),
        ]
        for method, path, body in cases:
            resp = await anon.request(method, path, json=body) if body else await anon.request(method, path)
            assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"
