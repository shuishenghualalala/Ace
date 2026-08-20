"""POST /api/work/references 对 browser_tab 类型与 snapshot_summary 的透传测试。"""

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


async def test_create_browser_tab_reference_via_api(tmp_path) -> None:
    """创建端点透传 browser_tab 类型与 snapshot_summary/source_link。"""
    app = _build(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=_auth_cookie("uid-a"),
    ) as client:
        sess = await client.post(
            "/api/work/sessions",
            json={"workspace_id": "default", "title": "会话"},
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["session_id"]

        created = await client.post(
            "/api/work/references",
            json={
                "target_session_id": session_id,
                "reference_type": "browser_tab",
                "source_id": "s0123-1",
                "source_link": "https://example.com/page",
                "snapshot_summary": "示例页面标题",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["reference_type"] == "browser_tab"
        assert body["source_id"] == "s0123-1"
        assert body["source_link"] == "https://example.com/page"
        assert body["snapshot_summary"] == "示例页面标题"

        listed = await client.get(f"/api/work/references?target_session_id={session_id}")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["reference_type"] == "browser_tab"
        assert items[0]["snapshot_summary"] == "示例页面标题"
        await client.post("/api/auth/logout")


async def test_create_reference_rejects_unknown_type(tmp_path) -> None:
    """非法 reference_type 仍然 422（新类型的加入不影响校验）。"""
    app = _build(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=_auth_cookie("uid-a"),
    ) as client:
        sess = await client.post(
            "/api/work/sessions",
            json={"workspace_id": "default", "title": "会话"},
        )
        session_id = sess.json()["session_id"]
        created = await client.post(
            "/api/work/references",
            json={
                "target_session_id": session_id,
                "reference_type": "not_a_type",
                "source_id": "x",
            },
        )
        assert created.status_code == 422
        await client.post("/api/auth/logout")
