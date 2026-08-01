"""Skills 模块测试：扫描、加载、消息构建、模板替换、resolve_skill_any、handle_skill_view。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from crew.agent.skills import (
    _parse_frontmatter,
    _preprocess_content,
    _slugify,
    audit_skills,
    build_skill_message,
    build_skills_index_prompt,
    get_builtin_skills_dir,
    install_skill,
    list_optional_skills,
    list_skills,
    repair_skills,
    resolve_skill,
    resolve_skill_any,
    scan_skills,
    uninstall_skill,
)


@pytest.fixture(autouse=True)
def _isolate_plugin_skill_roots():
    """清空并还原插件 skill roots 提供方。

    configure_plugin_skill_roots 是模块级全局：同进程里先跑过 build_app 的测试
    （如 gateway 集成测试）会把 plugins/browser 的 skills 根留在这里，导致本文件
    对 scan_skills 结果的精确断言随运行顺序漂移（多出 /browser-use）。
    """
    import crew.agent.skills as skills_mod

    saved = skills_mod._plugin_skill_roots_provider
    skills_mod.configure_plugin_skill_roots(None)
    yield
    skills_mod.configure_plugin_skill_roots(saved)


def _symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    """创建测试链接；Windows 未启用开发者模式时跳过对应测试。"""
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"当前环境不能创建符号链接: {exc}")


# ── Frontmatter 解析 ──────────────────────────────────────────────────────


def test_parse_frontmatter_basic():
    content = "---\nname: test-skill\ndescription: 测试\n---\n正文内容"
    fm, body = _parse_frontmatter(content)
    assert fm["name"] == "test-skill"
    assert fm["description"] == "测试"
    assert body.strip() == "正文内容"


def test_parse_frontmatter_no_frontmatter():
    content = "只有正文，没有 frontmatter"
    fm, body = _parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_parse_frontmatter_empty_body():
    content = "---\nname: empty\n---\n"
    fm, body = _parse_frontmatter(content)
    assert fm["name"] == "empty"
    assert body.strip() == ""


# ── Slug 化 ───────────────────────────────────────────────────────────────


def test_slugify_basic():
    assert _slugify("My Skill") == "my-skill"
    assert _slugify("test_skill") == "test-skill"
    assert _slugify("Test/Skill") == "testskill"
    assert _slugify("  coding  ") == "coding"


def test_slugify_multi_hyphen():
    assert _slugify("my--skill") == "my-skill"


# ── 模板变量替换 ───────────────────────────────────────────────────────────


def test_preprocess_content_skill_dir():
    content = "脚本路径：${CREW_SKILL_DIR}/scripts/run.sh"
    skill_dir = Path("/tmp/my_skill")
    result = _preprocess_content(content, skill_dir)
    assert result == "脚本路径：/tmp/my_skill/scripts/run.sh"


def test_preprocess_content_session_id():
    content = "会话：${CREW_SESSION_ID}"
    result = _preprocess_content(content, Path("/tmp"), session_id="abc-123")
    assert result == "会话：abc-123"


def test_preprocess_content_no_session_id_kept():
    content = "会话：${CREW_SESSION_ID}"
    result = _preprocess_content(content, Path("/tmp"), session_id=None)
    # 未提供 session_id 时保留原始占位符
    assert "${CREW_SESSION_ID}" in result


def test_scan_skills_skips_external_symlink(tmp_path, monkeypatch):
    """discovery 不得跟随 skills 根目录内指向根外的目录链接。"""
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    outside = tmp_path / "outside"
    builtin_dir.mkdir()
    user_dir.mkdir()
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: escaped\n---\nsecret", encoding="utf-8")
    _symlink_or_skip(outside, builtin_dir / "escaped", directory=True)
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    assert "/escaped" not in scan_skills()


def test_scan_skills_prunes_directory_symlink_cycle(tmp_path, monkeypatch):
    """root 内目录环必须被剪枝，合法 Skill 仍只发现一次。"""
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    skill_dir = builtin_dir / "safe"
    skill_dir.mkdir(parents=True)
    user_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: safe\n---\nbody", encoding="utf-8")
    _symlink_or_skip(builtin_dir, skill_dir / "loop", directory=True)
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    assert list(scan_skills()) == ["/safe"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction 定向测试")
def test_scan_skills_skips_external_windows_junction(tmp_path, monkeypatch):
    """Windows junction/reparse point 指向 skills 根外时必须 fail closed。"""
    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    outside = tmp_path / "outside"
    builtin_dir.mkdir()
    user_dir.mkdir()
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: escaped-junction\n---\nsecret", encoding="utf-8")
    junction = builtin_dir / "escaped-junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"当前环境不能创建 junction: {result.stderr or result.stdout}")
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)
    try:
        assert "/escaped-junction" not in scan_skills()
    finally:
        os.rmdir(junction)


# ── 扫描与加载 ────────────────────────────────────────────────────────────


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    """创建临时 skills 目录并隔离内置/用户目录。"""
    user_dir = tmp_path / "user_skills"
    user_dir.mkdir()
    builtin_dir = tmp_path / "builtin_skills"
    builtin_dir.mkdir()

    # 内置 skill
    (builtin_dir / "greet").mkdir()
    (builtin_dir / "greet" / "SKILL.md").write_text(
        "---\nname: greet\nfeatured: true\ndescription: 问候技能\n---\n你好！我是 Crew。",
        encoding="utf-8",
    )

    # 用户 skill
    (user_dir / "custom").mkdir()
    (user_dir / "custom" / "SKILL.md").write_text(
        "---\nname: custom\ndescription: 自定义技能\n---\n这是自定义内容。",
        encoding="utf-8",
    )

    # 同名 skill（用户覆盖内置）
    (builtin_dir / "greet2").mkdir()
    (builtin_dir / "greet2" / "SKILL.md").write_text(
        "---\nname: override-test\ndescription: 内置版本\n---\n内置内容",
        encoding="utf-8",
    )
    # 带中文显示名的 skill（resolve_skill 按中文名解析的回归用例）
    (builtin_dir / "translate").mkdir()
    (builtin_dir / "translate" / "SKILL.md").write_text(
        "---\nname: translate\ndisplay_name: 翻译助手\ndescription: 翻译技能\n---\n翻译内容",
        encoding="utf-8",
    )
    (user_dir / "override").mkdir()
    (user_dir / "override" / "SKILL.md").write_text(
        "---\nname: override-test\ndescription: 用户版本\n---\n用户内容",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    # 清除缓存
    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()
    skills_mod._skills_index_cache.clear()

    yield tmp_path


def test_scan_finds_builtin_and_user_skills(skills_dir):
    result = scan_skills()
    assert "/greet" in result
    assert "/custom" in result
    assert result["/greet"]["featured"] is True
    assert result["/custom"]["featured"] is False


def test_get_skills_refreshes_when_skill_md_changes(skills_dir):
    from crew.agent.skills import get_skills

    initial = get_skills()
    assert initial["/greet"]["description"] == "问候技能"

    skill_md = Path(initial["/greet"]["skill_md_path"])
    skill_md.write_text(
        "---\nname: greet\nfeatured: true\ndescription: 更新后的问候技能\n---\n你好！",
        encoding="utf-8",
    )

    refreshed = get_skills()
    assert refreshed["/greet"]["description"] == "更新后的问候技能"


def test_user_skill_overrides_builtin(skills_dir):
    result = scan_skills()
    # override-test 应该是用户版本
    assert "/override-test" in result
    assert result["/override-test"]["description"] == "用户版本"


def test_resolve_skill_with_slash(skills_dir):
    scan_skills()
    assert resolve_skill("/greet") == "/greet"


def test_resolve_skill_without_slash(skills_dir):
    scan_skills()
    assert resolve_skill("greet") == "/greet"


def test_resolve_skill_underscore_to_hyphen(skills_dir):
    # 若 skill 名为 my-skill，resolve_skill("my_skill") 应能找到
    scan_skills()
    assert resolve_skill("custom") == "/custom"


def test_resolve_skill_not_found(skills_dir):
    scan_skills()
    assert resolve_skill("nonexistent") is None


def test_resolve_skill_by_display_name(skills_dir):
    """中文显示名（chip 显示中文名、直接发送 /中文名）也能解析到 skill key。"""
    scan_skills()
    assert resolve_skill("翻译助手") == "/translate"
    # ws.py 已经 strip 掉前导 /，这里覆盖带 / 的容错
    assert resolve_skill("/翻译助手") == "/translate"
    # 大小写不敏感（英文名）
    assert resolve_skill("Translate") == "/translate"


def test_resolve_skill_duplicate_display_name_is_ambiguous(skills_dir):
    """重复中文显示名不应随机解析到第一个 skill。"""
    builtin_dir = skills_dir / "builtin_skills"
    for slug in ("dup-a", "dup-b"):
        (builtin_dir / slug).mkdir()
        (builtin_dir / slug / "SKILL.md").write_text(
            f"---\nname: {slug}\ndisplay_name: 重复助手\ndescription: {slug}\n---\n内容",
            encoding="utf-8",
        )

    scan_skills()

    assert resolve_skill("重复助手") is None
    assert resolve_skill("dup-a") == "/dup-a"
    assert resolve_skill("dup-b") == "/dup-b"


# ── 消息构建 ──────────────────────────────────────────────────────────────


def test_build_skill_message_basic(skills_dir):
    scan_skills()
    msg = build_skill_message("/greet")
    assert msg is not None
    assert "greet" in msg.lower() or "问候" in msg
    assert "你好！我是 Crew。" in msg


def test_build_skill_message_with_instruction(skills_dir):
    scan_skills()
    msg = build_skill_message("/greet", "请用英文回复")
    assert msg is not None
    assert "请用英文回复" in msg


def test_build_skill_message_not_found(skills_dir):
    scan_skills()
    assert build_skill_message("/nonexistent") is None


def test_build_skill_message_template_var(skills_dir):
    scan_skills()
    msg = build_skill_message("/greet", session_id="sess-999")
    assert msg is not None
    # skill 目录路径应被注入
    assert "Skill 目录" in msg


# ── 索引 prompt ───────────────────────────────────────────────────────────


def test_build_skills_index_prompt(skills_dir):
    scan_skills()
    prompt = build_skills_index_prompt()
    assert "/greet" in prompt
    assert "/custom" in prompt
    assert "问候技能" in prompt
    assert "compact skill index" in prompt
    assert "skill_view" in prompt


def test_build_skills_index_prompt_uses_cache(skills_dir):
    import crew.agent.skills as skills_mod

    scan_skills()
    first = build_skills_index_prompt()
    second = build_skills_index_prompt()
    assert first == second
    assert skills_mod._skills_index_cache


def test_build_skills_index_prompt_with_enabled(skills_dir):
    scan_skills()
    prompt = build_skills_index_prompt(enabled=["greet"])
    assert "/greet" in prompt
    assert "/custom" not in prompt


def test_build_skills_index_prompt_with_disabled(skills_dir):
    scan_skills()
    prompt = build_skills_index_prompt(disabled=["greet"])
    assert "/greet" not in prompt
    assert "/custom" in prompt


def test_build_skills_index_prompt_star_means_all(skills_dir):
    scan_skills()
    prompt = build_skills_index_prompt(enabled=["*"])
    assert "/greet" in prompt
    assert "/custom" in prompt


def test_build_skills_index_prompt_empty_enabled_blocks_all(skills_dir):
    scan_skills()
    prompt = build_skills_index_prompt(enabled=[])
    assert "/greet" not in prompt
    assert "/custom" not in prompt


def test_build_skills_index_prompt_star_disabled_blocks_all(skills_dir):
    scan_skills()
    prompt = build_skills_index_prompt(disabled=["*"])
    assert "/greet" not in prompt
    assert "/custom" not in prompt



def test_build_skills_index_prompt_empty(monkeypatch):
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: Path("/nonexistent"))
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: Path("/nonexistent"))
    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()
    prompt = build_skills_index_prompt()
    assert prompt == ""


# ── list_skills ───────────────────────────────────────────────────────────


def test_list_skills(skills_dir):
    scan_skills()
    items = list_skills()
    names = {s["name"] for s in items}
    assert "greet" in names
    assert "custom" in names
    # source 字段
    sources = {s["name"]: s["source"] for s in items}
    assert sources["greet"] == "builtin"
    assert sources["custom"] == "user"
    featured = {s["name"]: s["featured"] for s in items}
    assert featured["greet"] is True
    assert featured["custom"] is False
    categories = {s["name"]: s["category"] for s in items}
    assert categories["greet"] == "通用办公"
    assert categories["custom"] == "通用办公"


def test_list_skills_category_from_frontmatter(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()
    office = user_dir / "mail"
    office.mkdir()
    (office / "SKILL.md").write_text(
        "---\nname: mail\ncategory: 办公\ndescription: 邮件技能\n---\n正文",
        encoding="utf-8",
    )
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    items = list_skills()
    assert len(items) == 1
    assert items[0]["category"] == "通用办公"


def test_list_skills_with_filters(skills_dir):
    scan_skills()
    items = list_skills(enabled=["greet"])
    assert all(s["slug"] == "greet" for s in items)

    items = list_skills(disabled=["greet"])
    assert all(s["slug"] != "greet" for s in items)


def test_list_skills_exposes_chinese_display_metadata(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()
    skill_dir = user_dir / "mail"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: mail-assistant\n"
        "description: English fallback\n"
        "metadata:\n"
        "  skillCategoryName: 通用办公\n"
        "  zh_name: 邮箱助手\n"
        "  zh_description: 查询和发送电子邮箱邮件。\n"
        "  query_examples:\n"
        "    - 帮我查一下今天的未读邮件\n"
        "    - 给张三发送会议通知\n"
        "---\n"
        "正文",
        encoding="utf-8",
    )
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    items = list_skills()
    item = next(s for s in items if s["slug"] == "mail-assistant")
    assert item["display_name"] == "邮箱助手"
    assert item["description_zh"] == "查询和发送电子邮箱邮件。"
    assert item["query_examples"] == ["帮我查一下今天的未读邮件", "给张三发送会议通知"]
    assert item["category"] == "通用办公"


def test_audit_skills_reports_missing_metadata(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()

    legacy = user_dir / "legacy"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text(
        "---\nname: legacy\ndescription: Legacy skill\n---\n"
        "技能正文。\n",
        encoding="utf-8",
    )
    (legacy / "script.py").write_text(
        "print('demo')\n",
        encoding="utf-8",
    )

    good = user_dir / "good"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\n"
        "name: good\n"
        "description: 中文描述\n"
        "metadata:\n"
        "  skillCategoryName: 通用办公\n"
        "  zh_name: 好技能\n"
        "  zh_description: 用于测试的中文技能。\n"
        "  query_examples:\n"
        "    - 帮我测试这个技能\n"
        "---\n"
        "正文",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    report = audit_skills()
    legacy_item = next(s for s in report["skills"] if s["slug"] == "legacy")
    good_item = next(s for s in report["skills"] if s["slug"] == "good")
    codes = {f["code"] for f in legacy_item["findings"]}

    assert report["ok"] is False
    assert "missing_metadata_zh_name" in codes
    assert "missing_metadata_zh_description" in codes
    assert "missing_metadata_query_examples" in codes
    assert "missing_or_invalid_metadata_skill_category" in codes
    assert good_item["ok"] is True


async def test_repair_skills_generates_metadata(tmp_path, monkeypatch):
    from crew.agent.skills import repair_skills

    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()

    skill = user_dir / "legacy-mail"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: legacy-mail\ndescription: Mail helper\n---\n"
        "处理邮件。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    async def fake_generate(skill_md, frontmatter, body):  # noqa: ANN001
        return {
            "zh_name": "邮箱助手",
            "zh_description": "用于查询和处理邮箱消息。",
            "query_examples": ["帮我查一下未读邮件", "给张三发一封通知邮件"],
            "skillCategoryName": "通用办公",
        }

    monkeypatch.setattr("crew.agent.skills.generate_skill_metadata_with_model", fake_generate)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    result = await repair_skills(only="legacy-mail")

    assert result["ok"] is True
    skill_text = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert "zh_name: 邮箱助手" in skill_text
    assert "zh_description: 用于查询和处理邮箱消息。" in skill_text
    assert "帮我查一下未读邮件" in skill_text
    assert "skillCategoryName: 通用办公" in skill_text
    assert result["repaired"][0]["metadata_patch"].startswith("--- a/SKILL.md")
    assert result["repaired"][0]["path_changes"] == []


async def test_repair_skills_dry_run_does_not_call_model_or_write(tmp_path, monkeypatch):
    from crew.agent.skills import repair_skills

    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()
    skill = user_dir / "legacy"
    skill.mkdir()
    skill_md = skill / "SKILL.md"
    skill_md.write_text(
        "---\nname: legacy\ndescription: Legacy\n---\n技能正文",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    async def fail_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("dry_run 不应调用模型")

    monkeypatch.setattr("crew.agent.skills.generate_skill_metadata_with_model", fail_generate)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    result = await repair_skills(only="legacy", dry_run=True)

    assert result["dry_run"] is True
    assert result["repaired_count"] == 1
    assert "技能正文" in skill_md.read_text(encoding="utf-8")


async def test_repair_skills_asks_model_to_judge_top_level_description(tmp_path, monkeypatch):
    from crew.agent.skills import repair_skills

    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()
    skill = user_dir / "cn-mail"
    skill.mkdir()
    skill_md = skill / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: 中文邮箱\n"
        "description: 用于查询和发送邮件。\n"
        "category: 办公\n"
        "examples:\n"
        "  - 帮我查一下未读邮件\n"
        "---\n"
        "读取邮箱消息。",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    async def fake_generate(skill_md, frontmatter, body):  # noqa: ANN001
        assert frontmatter["description"] == "用于查询和发送邮件。"
        return {
            "zh_name": "中文邮箱",
            "zh_description": frontmatter["description"],
            "query_examples": ["帮我查一下未读邮件"],
            "skillCategoryName": "通用办公",
        }

    monkeypatch.setattr("crew.agent.skills.generate_skill_metadata_with_model", fake_generate)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    result = await repair_skills(only="cn-mail")

    assert result["ok"] is True
    content = skill_md.read_text(encoding="utf-8")
    assert "zh_name: 中文邮箱" in content
    assert "zh_description: 用于查询和发送邮件。" in content
    assert "帮我查一下未读邮件" in content
    assert "skillCategoryName: 通用办公" in content
    sources = result["repaired"][0]["metadata_sources"]
    assert {"field": "metadata.zh_name", "source": "existing_frontmatter"} in sources
    assert {"field": "metadata.query_examples", "source": "existing_frontmatter"} in sources
    assert {"field": "metadata.skillCategoryName", "source": "existing_frontmatter"} in sources
    assert {"field": "metadata.zh_description", "source": "llm"} in sources


async def test_repair_skills_does_not_copy_mixed_english_description(tmp_path, monkeypatch):
    from crew.agent.skills import repair_skills

    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()
    skill = user_dir / "mixed-desc"
    skill.mkdir()
    skill_md = skill / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: mixed-desc\n"
        "description: Use 秒译 API to translate documents.\n"
        "metadata:\n"
        "  zh_name: 文档翻译\n"
        "  query_examples:\n"
        "    - 帮我翻译这个PDF\n"
        "  skillCategoryName: 通用办公\n"
        "---\n"
        "调用秒译 API 翻译文档。",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    async def fake_generate(skill_md, frontmatter, body):  # noqa: ANN001
        assert frontmatter["description"] == "Use 秒译 API to translate documents."
        return {
            "zh_name": "文档翻译",
            "zh_description": "用于翻译 PDF、Word、Excel 等文档。",
            "query_examples": ["帮我翻译这个PDF"],
            "skillCategoryName": "通用办公",
        }

    monkeypatch.setattr("crew.agent.skills.generate_skill_metadata_with_model", fake_generate)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    result = await repair_skills(only="mixed-desc")

    assert result["ok"] is True
    content = skill_md.read_text(encoding="utf-8")
    assert "zh_description: 用于翻译 PDF、Word、Excel 等文档。" in content
    assert "zh_description: Use 秒译 API" not in content
    sources = result["repaired"][0]["metadata_sources"]
    assert {"field": "metadata.zh_description", "source": "llm"} in sources


async def test_repair_skills_reports_metadata_generation_failure(tmp_path, monkeypatch):
    from crew.agent.skills import repair_skills

    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()
    skill = user_dir / "broken-json"
    skill.mkdir()
    skill_md = skill / "SKILL.md"
    skill_md.write_text(
        "---\nname: broken-json\ndescription: Legacy helper\n---\n技能正文",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    async def fail_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ValueError("模型返回中没有 JSON object")

    monkeypatch.setattr("crew.agent.skills.generate_skill_metadata_with_model", fail_generate)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    result = await repair_skills(only="broken-json")

    assert result["error_count"] == 0
    assert result["ok"] is False
    repaired = result["repaired"][0]
    assert repaired["metadata_errors"][0]["code"] == "metadata_generation_failed"
    assert repaired["path_changes"] == []
    assert "技能正文" in skill_md.read_text(encoding="utf-8")


async def test_repair_skills_failure_keeps_original_tree(tmp_path, monkeypatch):
    """repair 的全部写入先落 staging；中途失败不能污染已发布 Skill。"""
    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()
    skill = user_dir / "legacy"
    skill.mkdir()
    skill_md = skill / "SKILL.md"
    original = (
        "---\n"
        "name: legacy\n"
        "description: English description\n"
        "metadata:\n"
        "  zh_name: 旧技能\n"
        "  query_examples:\n"
        "    - 帮我验证事务\n"
        "  skillCategoryName: 通用办公\n"
        "---\n"
        "正文"
    )
    skill_md.write_text(original, encoding="utf-8")

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    def fail_metadata_write(path, before, after, *, allowed_root):  # noqa: ANN001
        path.write_text("partial", encoding="utf-8")
        raise OSError("simulated repair failure")

    monkeypatch.setattr("crew.agent.skills._write_text_via_patch", fail_metadata_write)

    async def generate_missing_metadata(*args, **kwargs):  # noqa: ANN002, ANN003
        return {"zh_description": "用于验证事务回滚。"}

    monkeypatch.setattr(
        "crew.agent.skills.generate_skill_metadata_with_model",
        generate_missing_metadata,
    )

    import crew.agent.skills as skills_mod

    skills_mod._cache = {}
    skills_mod._cache_key = ()
    result = await repair_skills(only="legacy", operator_account_id="account-a")

    assert result["error_count"] == 1
    assert skill_md.read_text(encoding="utf-8") == original
    assert not list(user_dir.glob(".legacy-repair-*"))


def test_parse_metadata_json_response_extracts_json_from_text():
    from crew.agent.skills import _parse_metadata_json_response

    data = _parse_metadata_json_response(
        "可以，结果如下：\n"
        '{"zh_name":"邮箱助手","zh_description":"用于查询邮件。","query_examples":["帮我查未读邮件"]}\n'
        "已完成。"
    )

    assert data["zh_name"] == "邮箱助手"


# ── Optional skills ───────────────────────────────────────────────────────


@pytest.fixture()
def optional_env(tmp_path, monkeypatch):
    """隔离的 optional + user skills 测试环境。"""
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    opt_dir = tmp_path / "optional"
    opt_dir.mkdir()

    (opt_dir / "opt-skill").mkdir()
    (opt_dir / "opt-skill" / "SKILL.md").write_text(
        "---\n"
        "name: opt-skill\n"
        "description: 可安装技能\n"
        "metadata:\n"
        "  skillCategoryName: 设计与开发\n"
        "---\n"
        "内容",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: tmp_path / "builtin_empty")
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)
    monkeypatch.setattr("crew.agent.skills.get_optional_skills_dir", lambda: opt_dir)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()
    yield {"user_dir": user_dir, "opt_dir": opt_dir}
    # 清理
    skills_mod._cache = {}
    skills_mod._cache_key = ()


def test_list_optional_skills_not_installed(optional_env):
    scan_skills()
    optional = list_optional_skills()
    assert any(s["slug"] == "opt-skill" for s in optional)


def test_list_optional_skills_category(optional_env):
    scan_skills()
    optional = list_optional_skills()
    skill = next(s for s in optional if s["slug"] == "opt-skill")
    assert skill["category"] == "设计与开发"


def test_install_skill_creates_user_dir(optional_env):
    scan_skills()
    result = install_skill("opt-skill")
    assert result is True
    user_dir = optional_env["user_dir"]
    assert (user_dir / "opt-skill" / "SKILL.md").exists()


def test_install_skill_now_in_active(optional_env):
    scan_skills()
    install_skill("opt-skill")
    # 缓存已失效，重新扫描后应在 active skills 里
    assert "/opt-skill" in scan_skills()


def test_install_skill_removed_from_optional(optional_env):
    scan_skills()
    install_skill("opt-skill")
    optional = list_optional_skills()
    assert not any(s["slug"] == "opt-skill" for s in optional)


def test_install_skill_idempotent(optional_env):
    scan_skills()
    install_skill("opt-skill")
    result = install_skill("opt-skill")  # 再装一次
    assert result is False  # 已安装，返回 False


def test_install_skill_rejects_optional_tree_with_external_symlink(optional_env, tmp_path):
    """optional 安装不得通过 copytree 把根外文件复制进全局 Skill 集。"""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    skill_dir = optional_env["opt_dir"] / "opt-skill"
    _symlink_or_skip(outside, skill_dir / "leak.txt")
    scan_skills()

    assert install_skill("opt-skill") is False
    assert not (optional_env["user_dir"] / "opt-skill").exists()


def test_install_nonexistent_skill(optional_env):
    scan_skills()
    assert install_skill("nonexistent") is False


def test_uninstall_user_skill(optional_env):
    scan_skills()
    install_skill("opt-skill")
    result = uninstall_skill("opt-skill")
    assert result is True
    user_dir = optional_env["user_dir"]
    assert not (user_dir / "opt-skill").exists()


def test_uninstall_restores_optional(optional_env):
    scan_skills()
    install_skill("opt-skill")
    uninstall_skill("opt-skill")
    optional = list_optional_skills()
    assert any(s["slug"] == "opt-skill" for s in optional)


def test_global_skill_mutations_are_shared_and_audited(optional_env):
    """不同 Active Owner 操作的是同一宿主安装事实，审计保留各自操作者。"""
    import json

    scan_skills()
    assert install_skill("opt-skill", operator_account_id="account-a", source="test") is True
    assert "/opt-skill" in scan_skills()
    assert uninstall_skill("opt-skill", operator_account_id="account-b", source="test") is True

    audit_path = optional_env["user_dir"].parent / "logs" / "global-skills-audit.jsonl"
    events = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    successes = [event for event in events if event["result"] == "success"]
    assert [(event["action"], event["operator_account_id"]) for event in successes] == [
        ("install", "account-a"),
        ("uninstall", "account-b"),
    ]
    assert all("content" not in event and "token" not in event for event in events)


def test_install_audit_commit_failure_removes_published_tree(optional_env, monkeypatch):
    """成功审计无法持久化时，已 rename 发布的目录必须从全局名称回滚。"""
    import crew.agent.skills as skills_mod

    real_append = skills_mod._append_global_skill_audit

    def fail_success_audit(**event):  # noqa: ANN003
        if event["result"] == "success":
            raise OSError("simulated audit commit failure")
        real_append(**event)

    monkeypatch.setattr(skills_mod, "_append_global_skill_audit", fail_success_audit)

    scan_skills()
    assert install_skill("opt-skill", operator_account_id="account-a") is False
    assert not (optional_env["user_dir"] / "opt-skill").exists()
    assert "/opt-skill" not in scan_skills()


def test_uninstall_publishes_absence_before_best_effort_cleanup(optional_env, monkeypatch):
    """旧树清理失败只留下隐藏 tombstone，不能让已卸载 Skill 重新可见。"""
    import crew.agent.skills as skills_mod

    scan_skills()
    assert install_skill("opt-skill") is True
    real_rmtree = skills_mod.shutil.rmtree

    def fail_tombstone_cleanup(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if Path(path).name.startswith(".opt-skill.removed-"):
            raise OSError("simulated cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skills_mod.shutil, "rmtree", fail_tombstone_cleanup)

    assert uninstall_skill("opt-skill") is True
    assert not (optional_env["user_dir"] / "opt-skill").exists()
    assert "/opt-skill" not in scan_skills()
    assert list(optional_env["user_dir"].glob(".opt-skill.removed-*"))


def test_uninstall_rejects_skill_root_linked_outside_user_dir(tmp_path, monkeypatch):
    """卸载只可删除 user skills 根内的真实目录，不能跟随根外链接。"""
    user_dir = tmp_path / "user"
    outside = tmp_path / "outside"
    user_dir.mkdir()
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: escaped\n---\nbody", encoding="utf-8")
    linked = user_dir / "escaped"
    _symlink_or_skip(outside, linked, directory=True)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)
    monkeypatch.setattr(
        "crew.agent.skills.get_skills",
        lambda: {"/escaped": {"skill_dir": str(linked)}},
    )

    assert uninstall_skill("escaped") is False
    assert (outside / "SKILL.md").exists()


# ── 开源版本内置 skills ───────────────────────────────────────────────────


def test_builtin_skills_are_generic_only():
    builtin_dir = get_builtin_skills_dir()
    skill_mds = {
        path.relative_to(builtin_dir).as_posix()
        for path in builtin_dir.rglob("SKILL.md")
    }
    assert skill_mds == {
        "agent-guide/SKILL.md",
        "crew-wiki-curator/SKILL.md",
        "cua-driver/SKILL.md",
        "image-understanding/SKILL.md",
        "video-understanding/SKILL.md",
    }
    for relative_path in skill_mds:
        content = (builtin_dir / relative_path).read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(content)
        assert "name" in fm, f"{relative_path} 缺少 name 字段"
        assert "description" in fm, f"{relative_path} 缺少 description 字段"
        assert body.strip(), f"{relative_path} 正文为空"


def test_bundled_core_skills_can_be_viewed():
    import json

    from crew.tools.skills_tools import handle_skill_view

    skills = scan_skills()
    assert "/crew-guide" in skills
    assert "/crew-wiki-curator" in skills
    assert "/cua-driver" in skills

    guide = json.loads(handle_skill_view({
        "name": "crew-guide",
        "file_path": "references/install-skill.md",
    }))
    assert guide["success"] is True
    assert guide["name"] == "crew-guide"
    assert "CREW_HOME/skills/" in guide["content"]

    curator = json.loads(handle_skill_view({"name": "crew-wiki-curator"}))
    assert curator["success"] is True
    assert curator["name"] == "crew-wiki-curator"
    assert "wiki.ingest.auto_apply=true" in curator["content"]

    cua = json.loads(handle_skill_view({
        "name": "cua-driver",
        "file_path": "references/setup.md",
    }))
    assert cua["success"] is True
    assert cua["name"] == "cua-driver"
    assert "cua-driver__*" in cua["content"]
    assert "不包含 CUA Driver 的第三方可执行程序" in cua["content"]


# ── resolve_skill_any ─────────────────────────────────────────────────────


@pytest.fixture()
def mismatch_env(tmp_path, monkeypatch):
    """目录名 ≠ frontmatter name 场景。

    目录名: ppt
    frontmatter name: presentation-template-assistant
    slug: /presentation-template-assistant
    """
    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()

    skill_dir = user_dir / "ppt"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: presentation-template-assistant\ndescription: 通用 PPT 模板助手\n---\n# PPT 技能正文",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()
    scan_skills()
    yield {"skill_dir": skill_dir, "user_dir": user_dir}
    skills_mod._cache = {}
    skills_mod._cache_key = ()


def test_resolve_skill_any_by_slug(mismatch_env):
    """slug（带 /）命中。"""
    info = resolve_skill_any("/presentation-template-assistant")
    assert info is not None
    assert info["name"] == "presentation-template-assistant"


def test_resolve_skill_any_by_slug_no_slash(mismatch_env):
    """slug（不带 /）命中。"""
    info = resolve_skill_any("presentation-template-assistant")
    assert info is not None
    assert info["name"] == "presentation-template-assistant"


def test_resolve_skill_any_by_frontmatter_name(mismatch_env):
    """frontmatter name 命中（目录名与 name 不同，修复核心 bug）。"""
    info = resolve_skill_any("presentation-template-assistant")
    assert info is not None
    assert Path(info["skill_dir"]).name == "ppt"  # 目录名确实是 ppt


def test_resolve_skill_any_by_dir_name(mismatch_env):
    """目录名命中。"""
    info = resolve_skill_any("ppt")
    assert info is not None
    assert info["name"] == "presentation-template-assistant"


def test_resolve_skill_any_not_found(mismatch_env):
    """不存在时返回 None。"""
    assert resolve_skill_any("nonexistent-skill-xyz") is None


# ── handle_skill_view 委托测试 ─────────────────────────────────────────────


def test_handle_skill_view_by_frontmatter_name(mismatch_env):
    """传 frontmatter name（目录名≠name）时成功返回 content 和 skill_dir。"""
    import json
    from crew.tools.skills_tools import handle_skill_view

    result = json.loads(handle_skill_view({"name": "presentation-template-assistant"}))
    assert result["success"] is True
    assert result["name"] == "presentation-template-assistant"
    assert "skill_dir" in result
    assert Path(result["skill_dir"]).name == "ppt"
    assert "PPT 技能正文" in result["content"]


def test_handle_skill_view_not_found_shows_available(mismatch_env):
    """找不到技能时报错且错误信息包含可用技能名列表。"""
    from crew.core.errors import ToolError
    from crew.tools.skills_tools import handle_skill_view

    with pytest.raises(ToolError) as exc_info:
        handle_skill_view({"name": "totally-nonexistent"})
    msg = str(exc_info.value)
    assert "未找到技能" in msg
    # 错误信息里应包含可用技能（presentation-template-assistant）
    assert "presentation-template-assistant" in msg


def test_handle_skill_view_path_traversal_blocked(mismatch_env):
    """file_path 含 .. 路径穿越时被拒绝。"""
    from crew.core.errors import ToolError
    from crew.tools.skills_tools import handle_skill_view

    with pytest.raises(ToolError) as exc_info:
        handle_skill_view({
            "name": "presentation-template-assistant",
            "file_path": "../../etc/passwd",
        })
    msg = str(exc_info.value)
    assert "越权" in msg or "不存在" in msg


def test_handle_skill_view_rejects_file_symlink_outside_skill(mismatch_env, tmp_path):
    """指定文件读取必须校验最终 resolved target，而不只检查 ``..``。"""
    from crew.core.errors import ToolError
    from crew.tools.skills_tools import handle_skill_view

    outside = tmp_path / "secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    _symlink_or_skip(outside, mismatch_env["skill_dir"] / "leak.txt")

    with pytest.raises(ToolError) as exc_info:
        handle_skill_view({
            "name": "presentation-template-assistant",
            "file_path": "leak.txt",
        })
    assert "越权" in str(exc_info.value)


def test_handle_skill_view_allows_file_symlink_within_skill(mismatch_env):
    """安全底线不提前决定禁链政策：最终目标仍在当前 Skill 根内时可读取。"""
    import json

    from crew.tools.skills_tools import handle_skill_view

    target = mismatch_env["skill_dir"] / "notes.txt"
    target.write_text("internal note", encoding="utf-8")
    _symlink_or_skip(target, mismatch_env["skill_dir"] / "notes-link.txt")

    result = json.loads(handle_skill_view({
        "name": "presentation-template-assistant",
        "file_path": "notes-link.txt",
    }))

    assert result["content"] == "internal note"


async def test_repair_skills_never_reads_or_writes_external_symlink(tmp_path, monkeypatch):
    """audit/repair 应报告越界链接，且根外旧路径文本保持字节不变。"""
    user_dir = tmp_path / "user"
    builtin_dir = tmp_path / "builtin"
    skill_dir = user_dir / "safe"
    user_dir.mkdir()
    builtin_dir.mkdir()
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: safe\n"
        "description: 安全技能\n"
        "metadata:\n"
        "  zh_name: 安全技能\n"
        "  zh_description: 安全技能描述\n"
        "  query_examples:\n"
        "    - 帮我执行安全技能\n"
        "  skillCategoryName: 通用办公\n"
        "---\nbody",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.py"
    original = 'PATH = "/outside/config.env"\n'
    outside.write_text(original, encoding="utf-8")
    _symlink_or_skip(outside, skill_dir / "leak.py")
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    audited = audit_skills(only="safe")
    repaired = await repair_skills(only="safe")

    codes = {finding["code"] for finding in audited["skills"][0]["findings"]}
    assert "skill_path_outside" in codes
    assert repaired["ok"] is False
    assert outside.read_text(encoding="utf-8") == original


def test_handle_skills_list_respects_current_scope(mismatch_env):
    """skills_list 应返回当前任务上下文允许的技能，而非全量。"""
    import json
    from crew.core.runctx import current_skill_scope
    from crew.tools.skills_tools import handle_skills_list

    token = current_skill_scope.set((["presentation-template-assistant"], []))
    try:
        result = json.loads(handle_skills_list({}))
        slugs = {s["slug"] for s in result["skills"]}
        assert "presentation-template-assistant" in slugs
        # ppt-mismatch 不在本环境（mismatch_env 只创建 presentation-template-assistant），
        # scope 设为它时不应凭空出现
        assert "ppt-mismatch" not in slugs
    finally:
        current_skill_scope.reset(token)


def test_handle_skill_view_respects_current_scope(mismatch_env):
    """skill_view 应拒绝当前任务上下文未启用的技能。"""
    from crew.core.errors import ToolError
    from crew.core.runctx import current_skill_scope
    from crew.tools.skills_tools import handle_skill_view

    token = current_skill_scope.set((["ppt-mismatch"], []))
    try:
        with pytest.raises(ToolError) as exc_info:
            handle_skill_view({"name": "presentation-template-assistant"})
        assert "在当前任务上下文中不可用" in str(exc_info.value)
    finally:
        current_skill_scope.reset(token)


# ── SkillPackage 测试 ──────────────────────────────────────────────────────


@pytest.fixture()
def package_env(tmp_path, monkeypatch):
    """隔离的 package 测试环境：一个 business-travel package 含两个 skills，一个独立 skill。"""
    user_dir = tmp_path / "user_skills"
    builtin_dir = tmp_path / "builtin_skills"
    user_dir.mkdir()
    builtin_dir.mkdir()

    pkg_dir = builtin_dir / "business-travel"
    pkg_dir.mkdir()
    (pkg_dir / "PACKAGE.md").write_text(
        "---\n"
        "name: business-travel\n"
        "description: Business travel package\n"
        "metadata:\n"
        "  zh_name: 商旅出行\n"
        "  zh_description: 企业商旅查询能力包\n"
        "  skillCategoryName: 通用办公\n"
        "---\n"
        "# 商旅出行\n",
        encoding="utf-8",
    )

    flight_dir = pkg_dir / "query-flights"
    flight_dir.mkdir()
    (flight_dir / "SKILL.md").write_text(
        "---\n"
        "name: query-flights\n"
        "description: Query flights\n"
        "metadata:\n"
        "  zh_name: 航班查询\n"
        "  zh_description: 查询航班信息\n"
        "  skillCategoryName: 通用办公\n"
        "---\n"
        "航班查询正文",
        encoding="utf-8",
    )

    hotel_dir = pkg_dir / "query-hotel-order"
    hotel_dir.mkdir()
    (hotel_dir / "SKILL.md").write_text(
        "---\n"
        "name: query-hotel-order\n"
        "description: Query hotel orders\n"
        "metadata:\n"
        "  zh_name: 酒店订单查询\n"
        "  zh_description: 查询酒店订单\n"
        "  skillCategoryName: 通用办公\n"
        "---\n"
        "酒店订单查询正文",
        encoding="utf-8",
    )

    standalone_dir = builtin_dir / "mail-assistant"
    standalone_dir.mkdir()
    (standalone_dir / "SKILL.md").write_text(
        "---\n"
        "name: mail-assistant\n"
        "description: Mail skill\n"
        "metadata:\n"
        "  zh_name: 邮箱助手\n"
        "  zh_description: 邮件处理\n"
        "  skillCategoryName: 通用办公\n"
        "---\n"
        "邮件正文",
        encoding="utf-8",
    )

    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)

    import crew.agent.skills as skills_mod

    skills_mod._cache = {}
    skills_mod._cache_key = ()
    skills_mod._skills_index_cache.clear()
    scan_skills()
    yield {"builtin_dir": builtin_dir, "user_dir": user_dir, "pkg_dir": pkg_dir}
    skills_mod._cache = {}
    skills_mod._cache_key = ()
    skills_mod._skills_index_cache.clear()


def test_scan_finds_package_and_members(package_env):
    from crew.agent.skills import get_package_members, get_skill_packages

    packages = get_skill_packages()
    assert "/business-travel" in packages
    assert packages["/business-travel"]["slug"] == "business-travel"

    members = get_package_members("business-travel")
    slugs = {m["slug"] for m in members}
    assert slugs == {"business-travel/query-flights", "business-travel/query-hotel-order"}


def test_package_member_canonical_slug_and_alias(package_env):
    from crew.agent.skills import get_skills

    skills = get_skills()
    assert "/business-travel/query-flights" in skills
    info = skills["/business-travel/query-flights"]
    assert info["base_slug"] == "query-flights"
    assert "query-flights" in info["aliases"]
    assert info["package"] == "business-travel"


def test_build_skills_index_prompt_default_hides_package_members(package_env):
    prompt = build_skills_index_prompt()
    assert "# 可用 Skill Packages" in prompt
    assert "- /business-travel:" in prompt
    assert "# 其他可用 Skills" in prompt
    assert "- /mail-assistant:" in prompt
    # package members 默认不应出现
    assert "/business-travel/query-flights" not in prompt
    assert "/business-travel/query-hotel-order" not in prompt


def test_build_skills_index_prompt_expands_active_package(package_env):
    import crew.agent.skills as skills_mod
    from crew.core.runctx import current_active_skill_packages

    token = current_active_skill_packages.set({"business-travel"})
    try:
        # 清除缓存，确保重新生成
        skills_mod._skills_index_cache.clear()
        prompt = build_skills_index_prompt()
        assert "/business-travel/query-flights" in prompt
        assert "/business-travel/query-hotel-order" in prompt
        assert "查询航班信息" in prompt
        assert "查询酒店订单" in prompt
    finally:
        current_active_skill_packages.reset(token)


def test_resolve_skill_package_skill_path(package_env):
    assert resolve_skill("business-travel/query-flights") == "/business-travel/query-flights"


def test_resolve_skill_old_alias_still_works(package_env):
    assert resolve_skill("query-flights") == "/business-travel/query-flights"


def test_resolve_skill_any_package_skill_path(package_env):
    info = resolve_skill_any("business-travel/query-flights")
    assert info is not None
    assert info["slug"] == "business-travel/query-flights"


def test_resolve_skill_any_old_alias(package_env):
    info = resolve_skill_any("query-flights")
    assert info is not None
    assert info["slug"] == "business-travel/query-flights"


def test_resolve_skill_any_returns_package(package_env):
    info = resolve_skill_any("query-flights")
    assert info is not None
    assert info["package"] == "business-travel"
    assert "query-flights" in (info.get("aliases") or [])


def test_resolve_package_by_slug(package_env):
    from crew.agent.skills import resolve_package

    pkg = resolve_package("business-travel")
    assert pkg is not None
    assert pkg["slug"] == "business-travel"


def test_resolve_package_by_name(package_env):
    from crew.agent.skills import resolve_package

    pkg = resolve_package("business-travel")
    assert pkg is not None


def test_skill_package_open_tool(package_env):
    import json
    from crew.core.runctx import current_active_skill_packages
    from crew.tools.skills_tools import handle_skill_package_open

    assert current_active_skill_packages.get() == set()
    result = json.loads(handle_skill_package_open({"name": "business-travel"}))
    assert result["success"] is True
    assert result["package"] == "business-travel"
    assert len(result["members"]) == 2
    assert current_active_skill_packages.get() == {"business-travel"}


def test_skill_view_reads_package(package_env):
    import json
    from crew.tools.skills_tools import handle_skill_view

    result = json.loads(handle_skill_view({"name": "business-travel"}))
    assert result["success"] is True
    assert result["type"] == "package"
    assert result["slug"] == "business-travel"
    assert "商旅出行" in result["content"]


def test_skill_view_reads_package_skill(package_env):
    import json
    from crew.tools.skills_tools import handle_skill_view

    result = json.loads(handle_skill_view({"name": "business-travel/query-flights"}))
    assert result["success"] is True
    assert result["type"] == "skill"
    assert result["slug"] == "business-travel/query-flights"
    assert "航班查询正文" in result["content"]


def test_skill_view_old_alias_still_works(package_env):
    import json
    from crew.tools.skills_tools import handle_skill_view

    result = json.loads(handle_skill_view({"name": "query-flights"}))
    assert result["success"] is True
    assert result["type"] == "skill"
    assert result["slug"] == "business-travel/query-flights"


def test_skill_activation_uses_same_resolved_skill_metadata(tmp_path, monkeypatch):
    from crew.agent.skills import (
        SkillActivation,
        skill_activations_from_params,
        build_skill_activation,
        scan_skills,
    )

    builtin_dir = tmp_path / "builtin"
    user_dir = tmp_path / "user"
    skill_dir = builtin_dir / "test-search"
    skill_dir.mkdir(parents=True)
    user_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-search\n"
        "description: Test search skill\n"
        "metadata:\n"
        "  crew:\n"
        "    requires:\n"
        "      tools: [credential_request]\n"
        "      env: [TEST_AUTH_TOKEN, TEST_USER_ID]\n"
        "    entrypoints:\n"
        "      - id: search\n"
        "        path: scripts/search.py\n"
        "---\n"
        "Run the test search entrypoint.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("crew.agent.skills.get_builtin_skills_dir", lambda: builtin_dir)
    monkeypatch.setattr("crew.agent.skills.get_user_skills_dir", lambda: user_dir)
    scan_skills()

    context = build_skill_activation(
        "/test-search",
        "find a record",
        "session-skill",
    )

    assert context is not None
    assert context.skill_id == "test-search"
    assert context.required_tools == ("credential_request",)
    assert context.required_env == ("TEST_AUTH_TOKEN", "TEST_USER_ID")
    assert [(item.id, item.path) for item in context.entrypoints] == [
        ("search", "scripts/search.py"),
    ]
    assert "find a record" in context.instruction
    restored = skill_activations_from_params(
        {"active_skills": [context.to_dict()]}
    )
    assert restored == (context,)
    assert isinstance(restored[0], SkillActivation)
