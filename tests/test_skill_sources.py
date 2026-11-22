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
    assert entry["path"] == str(market.resolve())
    assert entry["label"] == "Team Skills"
    assert skills.list_skill_sources() == [entry]

    registry = isolated_skill_env.crew_home / "skill_market_sources.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["directories"][0]["path"] == str(market.resolve())

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
