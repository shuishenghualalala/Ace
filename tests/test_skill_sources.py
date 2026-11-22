"""Local skill market source registration, listing and copy install."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import crew.agent.skills as skills


@pytest.fixture
def isolated_skill_env(tmp_path, monkeypatch):
    crew_home = tmp_path / "crew-home"
    crew_home.mkdir()
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    local_dir = tmp_path / "agents-skills"
    local_dir.mkdir()
    monkeypatch.setenv("CREW_LOCAL_SKILLS_DIR", str(local_dir))
    skills._invalidate_cache()
    yield SimpleNamespace(crew_home=crew_home, local_dir=local_dir, tmp=tmp_path)
    skills._invalidate_cache()


def _make_skill(parent: Path, slug: str, *, desc: str = "market test") -> Path:
    skill_dir = Path(parent) / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: {desc}\n---\nbody\n",
        encoding="utf-8",
    )
    return skill_dir


def test_market_source_registry_round_trip(isolated_skill_env):
    market = isolated_skill_env.tmp / "team-market"
    market.mkdir()
    entry = skills.add_skill_source(str(market), label="Team Skills")
    assert entry["kind"] == "dir"
    assert entry["path"] == str(market.resolve())
    assert entry["label"] == "Team Skills"
    assert skills.list_skill_sources() == [entry]

    registry = isolated_skill_env.crew_home / "skill_market_sources.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["sources"][0]["path"] == str(market.resolve())

    with pytest.raises(skills.SkillSourceError, match="已登记"):
        skills.add_skill_source(str(market))
    assert skills.remove_skill_source(str(market)) is True
    assert skills.list_skill_sources() == []
    assert skills.remove_skill_source(str(market)) is False


@pytest.mark.parametrize("value", ["", "relative/market"])
def test_market_source_rejects_invalid_paths(isolated_skill_env, value):
    with pytest.raises(skills.SkillSourceError):
        skills.add_skill_source(value)


def test_market_source_rejects_a_missing_directory(isolated_skill_env):
    with pytest.raises(skills.SkillSourceError) as exc:
        skills.add_skill_source(str(isolated_skill_env.tmp / "missing-market"))
    assert exc.value.code == "source_not_directory"


def test_market_source_cannot_overlap_skill_install_directories(isolated_skill_env):
    user_dir = skills.get_user_skills_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(skills.SkillSourceError) as exc:
        skills.add_skill_source(str(user_dir))
    assert exc.value.code == "source_overlaps_skills"
    nested = isolated_skill_env.local_dir / "nested"
    nested.mkdir()
    with pytest.raises(skills.SkillSourceError) as exc:
        skills.add_skill_source(str(nested))
    assert exc.value.code == "source_overlaps_skills"


def test_market_source_lists_skills_with_origin_and_deduplicates(isolated_skill_env):
    market = isolated_skill_env.tmp / "team-market"
    _make_skill(market, "market-doc", desc="来自团队市场")
    _make_skill(isolated_skill_env.local_dir, "market-doc", desc="固定本地同名")
    _make_skill(isolated_skill_env.local_dir, "local-only")
    skills.add_skill_source(str(market), label="Team")
    skills._invalidate_cache()

    local = skills.list_local_skills()
    by_slug = {item["slug"]: item for item in local}
    assert set(by_slug) == {"market-doc", "local-only"}
    assert by_slug["market-doc"]["description"] == "固定本地同名"
    assert "market_source" not in by_slug["market-doc"]
    assert by_slug["local-only"]["source"] == "local"


def test_market_source_only_skill_reports_origin(isolated_skill_env):
    market = isolated_skill_env.tmp / "team-market"
    _make_skill(market, "team-only")
    skills.add_skill_source(str(market), label="Team")
    skills._invalidate_cache()

    item = next(item for item in skills.list_local_skills() if item["slug"] == "team-only")
    assert item["source"] == "local"
    assert item["market_source"] == str(market.resolve())
    assert item["market_label"] == "Team"


def test_install_from_market_source_copies_the_tree(isolated_skill_env):
    market = isolated_skill_env.tmp / "team-market"
    src = _make_skill(market, "team-doc")
    (src / "extra.txt").write_text("snapshot", encoding="utf-8")
    skills.add_skill_source(str(market))

    assert skills.install_skill("team-doc", operator_account_id="test", source="test")
    dst = skills.get_user_skills_dir() / "team-doc"
    assert dst.is_dir() and not dst.is_symlink()
    assert (dst / "extra.txt").read_text(encoding="utf-8") == "snapshot"

    (src / "extra.txt").write_text("changed later", encoding="utf-8")
    assert (dst / "extra.txt").read_text(encoding="utf-8") == "snapshot"
    skills._invalidate_cache()
    assert not any(item["slug"] == "team-doc" for item in skills.list_local_skills())


def test_install_from_market_source_can_be_uninstalled_without_touching_source(isolated_skill_env):
    market = isolated_skill_env.tmp / "team-market"
    src = _make_skill(market, "team-doc")
    skills.add_skill_source(str(market))
    assert skills.install_skill("team-doc", operator_account_id="test", source="test")

    assert skills.uninstall_skill("team-doc", operator_account_id="test", source="test")
    assert src.is_dir() and (src / "SKILL.md").is_file()
    assert not (skills.get_user_skills_dir() / "team-doc").exists()


def test_removed_market_source_disappears_from_store(isolated_skill_env):
    market = isolated_skill_env.tmp / "team-market"
    _make_skill(market, "team-doc")
    skills.add_skill_source(str(market))
    skills._invalidate_cache()
    assert any(item["slug"] == "team-doc" for item in skills.list_local_skills())

    assert skills.remove_skill_source(str(market)) is True
    skills._invalidate_cache()
    assert not any(item["slug"] == "team-doc" for item in skills.list_local_skills())


def test_url_market_source_lists_and_installs_inline_skill(isolated_skill_env, monkeypatch):
    import crew.agent.skills.sources as source_module

    url = "https://skills.example.com/manifest.json"
    monkeypatch.setattr(source_module, "_validate_remote_url", lambda value: value)
    monkeypatch.setattr(source_module, "_fetch_manifest", lambda value: [{
        "slug": "remote-doc",
        "name": "Remote document skill",
        "description": "Synthetic remote skill",
        "content": "---\nname: remote-doc\ndescription: Synthetic remote skill\n---\nbody\n",
    }])

    entry = skills.add_skill_source(url, label="Remote Team")
    assert entry["kind"] == "url"
    assert "cache_dir" not in entry
    item = next(item for item in skills.list_local_skills() if item["slug"] == "remote-doc")
    assert item["source"] == "local"
    assert item["market_source"] == url
    assert item["market_label"] == "Remote Team"

    assert skills.install_skill("remote-doc", operator_account_id="test", source="test")
    dst = skills.get_user_skills_dir() / "remote-doc"
    assert (dst / "SKILL.md").is_file() and not dst.is_symlink()
    assert skills.remove_skill_source(url) is True
    assert skills.list_skill_sources() == []


def test_refresh_url_source_replaces_cached_content(isolated_skill_env, monkeypatch):
    import crew.agent.skills.sources as source_module

    url = "https://skills.example.com/manifest.json"
    contents = ["first body", "second body"]
    monkeypatch.setattr(source_module, "_validate_remote_url", lambda value: value)
    monkeypatch.setattr(source_module, "_fetch_manifest", lambda value: [{
        "slug": "remote-doc", "name": "Remote", "description": "d",
        "content": f"---\nname: remote-doc\n---\n{contents[0]}\n",
    }])
    skills.add_skill_source(url)
    first = next(item for item in skills.list_local_skills() if item["slug"] == "remote-doc")
    assert "first body" in Path(first["skill_dir"], "SKILL.md").read_text(encoding="utf-8")

    monkeypatch.setattr(source_module, "_fetch_manifest", lambda value: [{
        "slug": "remote-doc", "name": "Remote", "description": "d",
        "content": f"---\nname: remote-doc\n---\n{contents[1]}\n",
    }])
    refreshed = skills.refresh_skill_source(url)
    assert refreshed["kind"] == "url" and "cache_dir" not in refreshed
    second = next(item for item in skills.list_local_skills() if item["slug"] == "remote-doc")
    assert "second body" in Path(second["skill_dir"], "SKILL.md").read_text(encoding="utf-8")
    assert first["skill_dir"] != second["skill_dir"]


def test_refresh_url_source_failure_keeps_previous_cache(isolated_skill_env, monkeypatch):
    import crew.agent.skills.sources as source_module

    url = "https://skills.example.com/manifest.json"
    monkeypatch.setattr(source_module, "_validate_remote_url", lambda value: value)
    monkeypatch.setattr(source_module, "_fetch_manifest", lambda value: [{
        "slug": "remote-doc", "name": "Remote", "description": "d", "content": "stable body",
    }])
    skills.add_skill_source(url)
    before = next(item for item in skills.list_local_skills() if item["slug"] == "remote-doc")

    def fail(_value):
        raise source_module.SkillSourceError("source_fetch_failed", "offline")

    monkeypatch.setattr(source_module, "_fetch_manifest", fail)
    with pytest.raises(skills.SkillSourceError, match="offline"):
        skills.refresh_skill_source(url)
    after = next(item for item in skills.list_local_skills() if item["slug"] == "remote-doc")
    assert after["skill_dir"] == before["skill_dir"]
    assert Path(after["skill_dir"], "SKILL.md").is_file()


def test_url_market_source_rejects_insecure_and_private_hosts(isolated_skill_env):
    import crew.agent.skills.sources as source_module

    with pytest.raises(skills.SkillSourceError) as exc:
        skills.add_skill_source("http://skills.example.com/manifest.json")
    assert exc.value.code == "source_insecure"
    with pytest.raises(skills.SkillSourceError) as exc:
        source_module._validate_remote_url("https://127.0.0.1/manifest.json")
    assert exc.value.code == "source_private_host"


def test_market_manifest_validation_rules():
    import crew.agent.skills.sources as source_module

    with pytest.raises(skills.SkillSourceError):
        source_module._parse_manifest({"version": 2, "skills": []})
    with pytest.raises(skills.SkillSourceError):
        source_module._parse_manifest({"version": 1, "skills": [{"slug": "../bad", "name": "x", "content": "x"}]})
    with pytest.raises(skills.SkillSourceError):
        source_module._parse_manifest({"version": 1, "skills": [
            {"slug": "same", "name": "a", "content": "x"},
            {"slug": "same", "name": "b", "content": "y"},
        ]})
    parsed = source_module._parse_manifest({"version": 1, "skills": [
        {"slug": "good", "name": "Good", "description": "ok", "content": "body"},
    ]})
    assert parsed[0]["slug"] == "good"


def test_https_manifest_fetch_decodes_bounded_json(monkeypatch):
    import httpx

    import crew.agent.skills.sources as source_module

    class FakeResponse:
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def iter_bytes(self):
            yield b'{"version":1,"skills":[{"slug":"remote","name":"Remote","content":"body"}]}'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    parsed = source_module._fetch_manifest("https://skills.example.com/manifest.json")
    assert parsed[0]["slug"] == "remote"


def test_legacy_registry_directories_still_load(isolated_skill_env):
    market = isolated_skill_env.tmp / "legacy-market"
    market.mkdir()
    registry = isolated_skill_env.crew_home / "skill_market_sources.json"
    registry.write_text(json.dumps({
        "version": 1,
        "directories": [{"path": str(market.resolve()), "label": "Legacy", "added_at": ""}],
    }), encoding="utf-8")
    sources = skills.list_skill_sources()
    assert sources == [{"kind": "dir", "path": str(market.resolve()), "label": "Legacy", "added_at": ""}]
