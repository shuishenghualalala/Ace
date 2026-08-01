"""上下文服务测试：gateway/context.py"""

import os

from crew.gateway.context import (
    _classify_file,
    complete_path,
    resolve_structured_path_references,
    save_upload,
)
from crew.state.home import owner_path_segment


def test_classify_file():
    assert _classify_file("photo.png") == "image"
    assert _classify_file("doc.jpg") == "image"
    assert _classify_file("script.py") == "file"
    assert _classify_file("data.json") == "file"


def test_save_upload():
    meta = save_upload("test.txt", b"hello world")
    assert meta["name"] == "test.txt"
    assert meta["type"] == "file"
    assert meta["size"] == 11
    assert meta["id"].startswith("att_")
    # 清理
    os.remove(meta["path"])


def test_save_upload_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    meta = save_upload("test.txt", b"hello world", owner_account_id="owner:user-a")
    assert "accounts" in meta["path"]
    assert owner_path_segment("owner:user-a") in meta["path"]
    os.remove(meta["path"])


def test_complete_path(tmp_path, monkeypatch):
    # 创建测试文件结构
    crew_home = tmp_path / ".crew"
    crew_home.mkdir()
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    (crew_home / "src").mkdir()
    (crew_home / "src" / "main.py").write_text("print('hi')")
    (crew_home / "readme.md").write_text("# Test")

    results = complete_path("src", cwd=str(crew_home))
    assert any(r["display"] == "src" for r in results)

    results = complete_path("@file:readme", cwd=str(crew_home))
    assert any("readme" in r["display"] for r in results)


def test_resolve_structured_path_references_only_accepts_workspace_tokens(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("print('ok')", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    refs = resolve_structured_path_references(
        "检查 @file:src/main.py 和 @folder:docs 忽略 /tmp/raw 与 @file:../secret.txt",
        workspace_root=str(root),
    )

    assert refs == [
        {"path": str(source.resolve()), "resource_type": "file"},
        {"path": str(docs.resolve()), "resource_type": "directory"},
    ]
