from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from crew.agent import skills
from crew.plugins import manager as plugin_manager
from crew.plugins.manager import PluginManager
from crew.plugins.security import PluginSecurityError
from crew.tools.file_utils import FileConflictError


def test_skill_manifest_read_rejects_ambiguous_hard_link(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    manifest = skill_dir / "SKILL.md"
    alias = skill_dir / "alias.md"
    manifest.write_text("---\nname: safe\n---\nbody\n", encoding="utf-8")
    try:
        os.link(manifest, alias)
    except OSError:
        pytest.skip("hard links unavailable")

    with pytest.raises(FileConflictError, match="硬链接"):
        skills.read_skill_text(manifest, skill_dir)


def test_skill_manifest_read_has_pre_read_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    manifest = skill_dir / "SKILL.md"
    manifest.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(skills, "_DISCOVERY_MAX_FILE_BYTES", 4)

    with pytest.raises(ValueError, match="读取上限"):
        skills.read_skill_text(manifest, skill_dir)


def test_plugin_discovery_skips_linked_or_reparse_directories(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "plugin.yaml").write_text("name: escaped\n", encoding="utf-8")
    linked = root / "escaped"

    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("junction creation unavailable")
    else:
        linked.symlink_to(outside, target_is_directory=True)

    try:
        assert PluginManager()._iter_plugin_dirs(root) == []
    finally:
        if os.name == "nt":
            os.rmdir(linked)
        else:
            linked.unlink()


def test_plugin_discovery_rejects_linked_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    plugin = outside / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: escaped\n", encoding="utf-8")
    linked = tmp_path / "linked-root"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("junction creation unavailable")
    else:
        linked.symlink_to(outside, target_is_directory=True)

    try:
        with pytest.raises(PluginSecurityError, match="root|reparse|link"):
            PluginManager()._iter_plugin_dirs(linked)
    finally:
        if os.name == "nt":
            os.rmdir(linked)
        else:
            linked.unlink()


def test_plugin_discovery_has_root_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
    monkeypatch.setattr(plugin_manager, "_PLUGIN_DISCOVERY_MAX_ROOTS", 1)

    with pytest.raises(PluginSecurityError, match="root"):
        PluginManager().discover_and_load(roots)


def test_plugin_manifest_read_rejects_hard_link(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    manifest = plugin_dir / "plugin.yaml"
    alias = plugin_dir / "alias.yaml"
    manifest.write_text(
        "\n".join(  # noqa: FLY002 - mirrors manifest fixtures used by plugin tests
            [
                "schema_version: crew.plugin.v1",
                "name: safe",
                "version: 1.0.0",
                "kind: standalone",
                "capabilities: [tools]",
            ]
        ),
        encoding="utf-8",
    )
    try:
        os.link(manifest, alias)
    except OSError:
        pytest.skip("hard links unavailable")

    loaded = PluginManager()._read_manifest(
        plugin_dir,
        key="safe",
        source="local",
    )

    assert loaded is None


def test_capability_discovery_has_root_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(skills, "_DISCOVERY_MAX_ROOTS", 1)
    monkeypatch.setattr(skills, "get_builtin_skills_dir", lambda: empty)
    monkeypatch.setattr(skills, "get_user_skills_dir", lambda: empty)
    monkeypatch.setattr(skills, "get_plugin_skill_roots", lambda: roots)

    with pytest.raises(skills.SkillDiscoveryLimitError, match="根目录"):
        skills.scan_skills()


def test_capability_discovery_has_file_and_bundle_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    for index in range(2):
        skill_dir = root / f"skill-{index}"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: skill-{index}\n---\nbody\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(skills, "_DISCOVERY_MAX_FILES", 1)
    with pytest.raises(skills.SkillDiscoveryLimitError, match="文件数量"):
        list(skills._iter_skill_files(root))

    for index in range(2):
        package_dir = root / f"package-{index}"
        package_dir.mkdir()
        (package_dir / "PACKAGE.md").write_text(
            f"---\nname: package-{index}\n---\nbody\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(skills, "_DISCOVERY_MAX_FILES", 100)
    monkeypatch.setattr(skills, "_DISCOVERY_MAX_BUNDLES", 1)
    with pytest.raises(skills.SkillDiscoveryLimitError, match="bundle"):
        list(skills._iter_package_skills(root))


def test_capability_discovery_detects_root_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "builtin"
    user = tmp_path / "user"
    old_root = tmp_path / "builtin-old"
    root.mkdir()
    user.mkdir()
    monkeypatch.setattr(skills, "get_builtin_skills_dir", lambda: root)
    monkeypatch.setattr(skills, "get_user_skills_dir", lambda: user)
    monkeypatch.setattr(skills, "get_plugin_skill_roots", list)
    original_scan = skills._scan_dir
    swapped = False

    def swap_root(path: Path, seen: set[str]) -> dict[str, dict]:
        nonlocal swapped
        result = original_scan(path, seen)
        if path == root and not swapped:
            root.rename(old_root)
            root.mkdir()
            swapped = True
        return result

    monkeypatch.setattr(skills, "_scan_dir", swap_root)
    with pytest.raises(skills.SkillDiscoveryLimitError, match="发生变化"):
        skills.scan_skills()

    assert swapped


def test_capability_discovery_freezes_success_per_request_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.core.runctx import (
        current_owner_account_id,
        current_request_id,
        current_session_id,
    )

    root = tmp_path / "builtin"
    user = tmp_path / "user"
    skill_dir = root / "demo"
    skill_dir.mkdir(parents=True)
    user.mkdir()
    manifest = skill_dir / "SKILL.md"
    manifest.write_text("---\nname: demo\n---\nfirst\n", encoding="utf-8")
    monkeypatch.setattr(skills, "get_builtin_skills_dir", lambda: root)
    monkeypatch.setattr(skills, "get_user_skills_dir", lambda: user)
    monkeypatch.setattr(skills, "get_plugin_skill_roots", list)
    monkeypatch.setattr(skills, "_cache", {})
    monkeypatch.setattr(skills, "_cache_key", ())
    skills._step_discovery_cache.clear()
    owner_token = current_owner_account_id.set("owner-cap")
    session_token = current_session_id.set("session-cap")
    request_token = current_request_id.set("request-one")
    try:
        first = skills.get_skills()["/demo"]["content"]
        manifest.write_text("---\nname: demo\n---\nsecond\n", encoding="utf-8")
        frozen = skills.get_skills()["/demo"]["content"]
        current_request_id.set("request-two")
        refreshed = skills.get_skills()["/demo"]["content"]
    finally:
        current_request_id.reset(request_token)
        current_session_id.reset(session_token)
        current_owner_account_id.reset(owner_token)
        skills._step_discovery_cache.clear()

    assert first == frozen == "first"
    assert refreshed == "second"


def test_capability_discovery_freezes_package_and_member_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "builtin"
    user = tmp_path / "user"
    package = root / "demo-package"
    member = package / "demo"
    member.mkdir(parents=True)
    user.mkdir()
    (package / "PACKAGE.md").write_text(
        "---\nname: demo-package\ndescription: demo\n---\npackage\n",
        encoding="utf-8",
    )
    (member / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills, "get_builtin_skills_dir", lambda: root)
    monkeypatch.setattr(skills, "get_user_skills_dir", lambda: user)
    monkeypatch.setattr(skills, "get_plugin_skill_roots", list)
    monkeypatch.setattr(skills, "_cache", {})
    monkeypatch.setattr(skills, "_cache_key", ())

    discovered = skills.get_skills()
    packages = skills.get_skill_packages()
    package_info = skills.get_package_info("/demo-package")
    members = skills.get_package_members("demo-package")

    assert discovered["/demo-package/demo"]["slug"] == "demo-package/demo"
    assert package_info is packages["/demo-package"]
    assert isinstance(skills._package_members["/demo-package"], tuple)
    with pytest.raises(TypeError):
        packages["/demo-package"]["name"] = "tampered"
    with pytest.raises(TypeError):
        package_info["description"] = "tampered"  # type: ignore[index]

    members.clear()
    assert len(skills.get_package_members("demo-package")) == 1


def test_capability_discovery_snapshot_is_recursively_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.core.runctx import current_request_id

    root = tmp_path / "builtin"
    user = tmp_path / "user"
    skill_dir = root / "demo"
    skill_dir.mkdir(parents=True)
    user.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(skills, "get_builtin_skills_dir", lambda: root)
    monkeypatch.setattr(skills, "get_user_skills_dir", lambda: user)
    monkeypatch.setattr(skills, "get_plugin_skill_roots", list)
    monkeypatch.setattr(skills, "_cache", {})
    monkeypatch.setattr(skills, "_cache_key", ())
    skills._step_discovery_cache.clear()
    token = current_request_id.set("request-immutable")
    try:
        snapshot = skills.get_skills()
        with pytest.raises(TypeError):
            snapshot["/demo"] = {}  # type: ignore[index]
        with pytest.raises(TypeError):
            snapshot["/demo"]["content"] = "tampered"  # type: ignore[index]
        with pytest.raises(TypeError):
            snapshot["/demo"]["aliases"] += ("tampered",)  # type: ignore[index]
        assert snapshot["/demo"]["aliases"] == ()
        assert skills.get_skills()["/demo"]["content"] == "body"
    finally:
        current_request_id.reset(token)
        skills._step_discovery_cache.clear()


def test_capability_discovery_memoizes_failure_per_request_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.core.runctx import current_request_id

    attempts = 0

    def fail_scan() -> dict[str, dict]:
        nonlocal attempts
        attempts += 1
        raise skills.SkillDiscoveryLimitError("budget")

    monkeypatch.setattr(skills, "_mtime_key", lambda: ("stable",))
    monkeypatch.setattr(skills, "scan_skills", fail_scan)
    monkeypatch.setattr(skills, "_cache", {})
    monkeypatch.setattr(skills, "_cache_key", ())
    skills._step_discovery_cache.clear()
    token = current_request_id.set("request-failure")
    try:
        with pytest.raises(skills.SkillDiscoveryLimitError, match="budget"):
            skills.get_skills()
        with pytest.raises(skills.SkillDiscoveryLimitError, match="budget"):
            skills.get_skills()
    finally:
        current_request_id.reset(token)
        skills._step_discovery_cache.clear()

    assert attempts == 1


def test_capability_discovery_is_synchronous_with_single_concurrency_slot() -> None:
    import inspect

    source = inspect.getsource(skills.scan_skills)
    assert skills._DISCOVERY_MAX_CONCURRENCY == 1
    assert "Executor" not in source
    assert "create_task" not in source
