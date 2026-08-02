"""本地 skill（~/.agents/skills）发现与软链安装测试。

覆盖：扫描发现、软链安装、scan 加载、source 判定、软链卸载（删链不删源）、
安全边界（软链到白名单外被拒）、已安装不重复展示。
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.agent import skills


@pytest.fixture
def isolated_skill_env(tmp_path, monkeypatch):
    """隔离 CREW_HOME 与 CREW_LOCAL_SKILLS_DIR，绝不污染真实 ~/.Crew / ~/.agents。"""
    crew_home = tmp_path / "crew-home"
    crew_home.mkdir()
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    local_dir = tmp_path / "agents-skills"
    local_dir.mkdir()
    monkeypatch.setenv("CREW_LOCAL_SKILLS_DIR", str(local_dir))

    skills._invalidate_cache()
    yield SimpleNamespace(crew_home=crew_home, local_dir=local_dir)
    skills._invalidate_cache()


def _make_skill(parent: Path, slug: str, *, name: str | None = None, desc: str = "test") -> Path:
    """在 parent 下建一个 skill 目录 + SKILL.md，返回 skill 目录。"""
    skill_dir = Path(parent) / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_name = name or slug
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: {desc}\n---\nbody\n",
        encoding="utf-8",
    )
    return skill_dir


def test_list_local_skills_discovers_local(isolated_skill_env):
    """list_local_skills 扫描本地源目录，source 为 local。"""
    _make_skill(isolated_skill_env.local_dir, "lark-doc", desc="飞书文档")
    skills._invalidate_cache()

    local = skills.list_local_skills()
    assert len(local) == 1
    assert local[0]["slug"] == "lark-doc"
    assert local[0]["source"] == "local"
    assert local[0]["skill_dir"]


def test_list_local_skills_excludes_installed(isolated_skill_env):
    """已安装（软链进 user dir）的 skill 不再出现在 local 列表。"""
    _make_skill(isolated_skill_env.local_dir, "lark-doc")
    assert skills.install_skill("lark-doc", operator_account_id="test", source="test")
    skills._invalidate_cache()

    local = skills.list_local_skills()
    assert not any(s["slug"] == "lark-doc" for s in local)


def test_install_local_skill_creates_symlink(isolated_skill_env):
    """install 在 user dir 建软链指向本地源目录。"""
    src = _make_skill(isolated_skill_env.local_dir, "lark-doc")
    user_dir = skills.get_user_skills_dir()

    assert skills.install_skill("lark-doc", operator_account_id="test", source="test")
    dst = user_dir / "lark-doc"
    assert dst.is_symlink()
    assert dst.resolve() == src.resolve()


def test_scan_loads_symlink_skill(isolated_skill_env):
    """安装后 scan_skills 能加载软链 skill，对话中可 resolve。"""
    _make_skill(isolated_skill_env.local_dir, "lark-doc")
    assert skills.install_skill("lark-doc", operator_account_id="test", source="test")
    skills._invalidate_cache()

    loaded = skills.get_skills()
    assert "/lark-doc" in loaded
    assert skills.resolve_skill("lark-doc") == "/lark-doc"


def test_list_skills_source_user_not_builtin(isolated_skill_env):
    """软链 skill 在 list_skills 中 source=user，不误判 builtin。"""
    _make_skill(isolated_skill_env.local_dir, "lark-doc")
    assert skills.install_skill("lark-doc", operator_account_id="test", source="test")
    skills._invalidate_cache()

    lst = skills.list_skills()
    item = next(s for s in lst if s["slug"] == "lark-doc")
    assert item["source"] == "user"


def test_uninstall_removes_symlink_keeps_source(isolated_skill_env):
    """uninstall 删软链本身，源目录完好。"""
    src = _make_skill(isolated_skill_env.local_dir, "lark-doc")
    user_dir = skills.get_user_skills_dir()
    assert skills.install_skill("lark-doc", operator_account_id="test", source="test")
    dst = user_dir / "lark-doc"

    assert skills.uninstall_skill("lark-doc", operator_account_id="test", source="test")
    assert not os.path.lexists(dst), "软链应已删除"
    assert src.exists(), "源目录应完好"


def test_untrusted_symlink_rejected(isolated_skill_env, tmp_path):
    """软链到受信任根（~/.agents/skills）外的目录被拒（安全边界不破）。"""
    user_dir = skills.get_user_skills_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    evil_src = tmp_path / "evil"
    evil_src.mkdir()
    (evil_src / "SKILL.md").write_text(
        "---\nname: evil\ndescription: x\n---\n", encoding="utf-8"
    )

    dst = user_dir / "evil"
    os.symlink(evil_src, dst)
    try:
        with pytest.raises(skills.SkillPathError):
            skills._validate_skill_tree(dst, user_dir)
        with pytest.raises(skills.SkillPathError):
            skills.resolve_skill_path(dst, user_dir)
    finally:
        os.unlink(dst)


def test_install_local_skill_not_found(isolated_skill_env):
    """install 不存在的 slug 返回 False。"""
    assert not skills.install_skill("nope", operator_account_id="test", source="test")
