"""文件上传安全测试（G3）：体积上限、TOCTOU 去重、非法文件名。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway.context import save_upload
from crew.gateway.server import create_app
from crew.state.home import owner_path_segment


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    return tmp_path / ".crew"


# ---------------- save_upload 直接测试 ----------------

def test_save_upload_concurrent_same_name_distinct_ids(crew_home):
    """两次同名上传落不同文件（uuid），互不覆盖；返回 name 仍是原始名。"""
    a = save_upload("note.txt", b"aaa")
    b = save_upload("note.txt", b"bbb")
    assert a["name"] == "note.txt" and b["name"] == "note.txt"
    # 落地文件不同（路径不同）
    assert a["path"] != b["path"]
    # 各自读回自己的内容（未被对方覆盖）
    assert Path(a["path"]).read_bytes() == b"aaa"
    assert Path(b["path"]).read_bytes() == b"bbb"
    # id 不同
    assert a["id"] != b["id"]


def test_save_upload_owner_scoped_paths(crew_home):
    a = save_upload("note.txt", b"aaa", owner_account_id="A:uid-a")
    b = save_upload("note.txt", b"bbb", owner_account_id="B:uid-b")
    seg_a = owner_path_segment("A:uid-a")
    seg_b = owner_path_segment("B:uid-b")
    assert "accounts" in a["path"] and seg_a in a["path"]
    assert "accounts" in b["path"] and seg_b in b["path"]
    assert Path(a["path"]).parent != Path(b["path"]).parent


def test_save_upload_rejects_path_separator(crew_home):
    with pytest.raises(ValueError):
        save_upload("../evil.txt", b"x")
    with pytest.raises(ValueError):
        save_upload("a/b.txt", b"x")
    with pytest.raises(ValueError):
        save_upload("win\\evil.txt", b"x")


def test_save_upload_rejects_nul(crew_home):
    with pytest.raises(ValueError):
        save_upload("evil\x00.txt", b"x")


# ---------------- 路由层 413 体积上限 ----------------

@pytest.mark.asyncio
async def test_upload_oversized_returns_413(crew_home, auth_headers):
    """超过体积上限的 base64 在 b64decode 前即被拒，返回 413。"""
    crew = build_app(enable_team=False)
    app = create_app(crew)
    # 构造 > 28 MiB 的 base64 串（约 21+ MiB 解码后）
    big = base64.b64encode(b"x" * (22 * 1024 * 1024)).decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/upload", json={"filename": "big.bin", "content": big})
    assert resp.status_code == 413
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_upload_normal_works(crew_home, auth_headers):
    """正常体积上传成功（回归）。"""
    crew = build_app(enable_team=False)
    app = create_app(crew)
    content = base64.b64encode(b"hello world").decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/upload", json={"filename": "hi.txt", "content": content})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "hi.txt"
    assert data["size"] == len(b"hello world")
    assert "accounts" in data["path"]
