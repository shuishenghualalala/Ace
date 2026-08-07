"""context.complete_path 路径补全的安全与正确性测试（G2）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.gateway.auth import AccountContext
from crew.gateway.context import complete_path
from crew.gateway.routers.misc import create_misc_router
from crew.state.home import owner_path_segment


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    """隔离的 Crew home，含若干文件/目录用于补全。"""
    home = tmp_path / ".crew"
    (home / "task_workspaces" / "default").mkdir(parents=True)
    (home / "uploads").mkdir(parents=True)
    # 一些可补全的条目
    (home / "README.md").write_text("hi", encoding="utf-8")
    (home / "config.yaml").write_text("k: v", encoding="utf-8")
    (home / "subdir").mkdir()
    (home / "subdir" / "a.txt").write_text("a", encoding="utf-8")
    (home / "subdir" / "b.txt").write_text("b", encoding="utf-8")
    monkeypatch.setenv("CREW_HOME", str(home))
    # load_config 会写 CREW_TASK_WORKSPACE_ROOT 到 os.environ，不清理会导致
    # task_workspace_path 指向别的测试的目录，影响 complete_path 的 workspace_id 分支。
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    # context.py 在模块导入时已读 _UPLOAD_DIR，但 complete_path 内部每调用都
    # get_crew_home()，故 setenv 即可生效。
    return home


def test_complete_out_of_crew_home_returns_empty(crew_home, monkeypatch):
    """cwd 指向 crew_home 之外（如系统目录）时，补全必须返回空，不能枚举外部文件。"""
    # /etc 在 Windows 上一般不存在；用 tmp_path 下的一个非 crew 目录模拟
    outside = crew_home.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    results = complete_path("", cwd=str(outside))
    assert results == []


def test_complete_dotdot_escape_rejected(crew_home):
    """query 含 .. 试图逃出 base 时返回空。"""
    assert complete_path("../", cwd=str(crew_home)) == []
    assert complete_path("subdir/../../..", cwd=str(crew_home)) == []


def test_complete_cwd_dotdot_rejected(crew_home):
    """cwd 本身含 .. 片段时返回空（不跟随到父目录）。"""
    cwd_with_dotdot = str(Path(crew_home) / ".." / "outside")
    assert complete_path("", cwd=cwd_with_dotdot) == []


def test_complete_within_crew_home_returns_entries(crew_home):
    """正常情况：cwd 在 crew_home 内，返回补全条目，meta 为相对路径。"""
    results = complete_path("", cwd=str(crew_home))
    names = {r["display"] for r in results}
    assert {"README.md", "config.yaml", "subdir", "uploads", "task_workspaces"} <= names
    # meta 必须是相对 base 的路径，而不是绝对路径
    for r in results:
        assert not Path(r["meta"]).is_absolute(), r
        assert "/" not in r["meta"].lstrip("\\") or "\\" not in r["meta"]  # 相对片段
    # 绝对路径不应出现在 meta 中
    assert all(not r["meta"].startswith(str(crew_home)) for r in results)


def test_complete_prefix_filter(crew_home):
    """pattern 前缀过滤生效。"""
    results = complete_path("config", cwd=str(crew_home))
    assert len(results) == 1
    assert results[0]["display"] == "config.yaml"


def test_complete_workspace_id_uses_task_workspace(crew_home):
    """未传 cwd 时 workspace_id 定位到 task_workspaces/{id}/。"""
    ws_dir = crew_home / "task_workspaces" / "ws_test"
    ws_dir.mkdir(parents=True)
    (ws_dir / "notes.md").write_text("x", encoding="utf-8")
    results = complete_path("", workspace_id="ws_test")
    names = {r["display"] for r in results}
    assert "notes.md" in names


def test_complete_nested_workspace_path_query(crew_home):
    """@ 支持在当前 workspace 内按多级路径继续补全。"""
    ws_dir = crew_home / "task_workspaces" / "ws_test" / "src" / "pkg"
    ws_dir.mkdir(parents=True)
    (ws_dir / "agent.py").write_text("x", encoding="utf-8")

    results = complete_path("src/pkg/a", workspace_id="ws_test")

    assert results == [{
        "text": "@file:src/pkg/agent.py",
        "display": "agent.py",
        "meta": "src/pkg/agent.py",
        "type": "file",
    }]


def test_complete_workspace_id_is_owner_scoped(crew_home):
    ws_a = crew_home / "accounts" / owner_path_segment("A:uid-a") / "task_workspaces" / "same"
    ws_b = crew_home / "accounts" / owner_path_segment("B:uid-b") / "task_workspaces" / "same"
    ws_a.mkdir(parents=True)
    ws_b.mkdir(parents=True)
    (ws_a / "a.txt").write_text("a", encoding="utf-8")
    (ws_b / "b.txt").write_text("b", encoding="utf-8")

    results_a = complete_path("", workspace_id="same", owner_account_id="A:uid-a")
    results_b = complete_path("", workspace_id="same", owner_account_id="B:uid-b")

    assert {r["display"] for r in results_a} == {"a.txt"}
    assert {r["display"] for r in results_b} == {"b.txt"}


def test_complete_local_workspace_root_outside_crew_home(tmp_path, monkeypatch):
    """绑定本地目录的工作空间可在 crew_home 外补全。"""
    home = tmp_path / ".crew"
    home.mkdir()
    monkeypatch.setenv("CREW_HOME", str(home))
    outside = tmp_path / "projects" / "demo"
    outside.mkdir(parents=True)
    (outside / "main.py").write_text("print(1)", encoding="utf-8")
    results = complete_path("", workspace_root_path=str(outside))
    assert any(r["display"] == "main.py" for r in results)


def test_complete_subdir_entries_relative_meta(crew_home):
    """以子目录为 base 补全时，meta 是相对该 base 的相对路径（仍落在 crew_home 内）。"""
    subdir = crew_home / "subdir"
    results = complete_path("a", cwd=str(subdir))
    assert len(results) == 1
    assert results[0]["display"] == "a.txt"
    # meta 相对 base（subdir），不含绝对路径
    assert not Path(results[0]["meta"]).is_absolute()
    assert "a.txt" in results[0]["meta"]


def test_complete_prefix_search_includes_nested_entries(crew_home):
    """在根目录输入文件名前缀时，也能命中子目录并保留相对路径。"""
    results = complete_path("a", cwd=str(crew_home))

    assert results == [{
        "text": "@file:subdir/a.txt",
        "display": "a.txt",
        "meta": "subdir/a.txt",
        "type": "file",
    }]


def test_complete_prefix_search_skips_generated_trees(crew_home):
    """递归补全不应被依赖/构建目录拖慢或污染结果。"""
    (crew_home / "node_modules" / "nested").mkdir(parents=True)
    (crew_home / "node_modules" / "nested" / "desktop.txt").write_text("x", encoding="utf-8")
    (crew_home / "src" / "desktop.txt").parent.mkdir()
    (crew_home / "src" / "desktop.txt").write_text("x", encoding="utf-8")

    results = complete_path("desktop", cwd=str(crew_home))

    assert [result["meta"] for result in results] == ["src/desktop.txt"]


def test_complete_route_default_workspace_ignores_configured_root_path(crew_home, tmp_path):
    """默认「对话」只补全 default task workspace，不应误读项目 workspace root_path。"""
    owner_id = "A:uid-a"
    default_dir = crew_home / "accounts" / owner_path_segment(owner_id) / "task_workspaces" / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "chat.txt").write_text("chat", encoding="utf-8")
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "project-only.txt").write_text("secret", encoding="utf-8")

    class WorkspaceStore:
        def get(self, workspace_id: str, owner_account_id: str = "") -> dict:
            assert workspace_id == "default"
            return {"id": "default", "root_path": str(project_root)}

    app = FastAPI()

    @app.middleware("http")
    async def inject_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(create_misc_router(SimpleNamespace(workspace_store=WorkspaceStore())))

    rows = TestClient(app).get("/api/complete", params={"workspace_id": "default"}).json()

    names = {r["display"] for r in rows}
    assert "chat.txt" in names
    assert "project-only.txt" not in names
