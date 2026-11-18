"""Prompt 文件读取缓存与线程化测试。"""

import os
import threading

import pytest

from crew.agent import instructions, prompt_builder
from crew.agent.compact.tokens import estimate_text_tokens
from crew.agent.prompt_budget import source_token_budgets
from crew.agent.prompt_builder import _clear_file_cache, _load_profile, build_prompt_parts
from crew.core.runctx import current_owner_account_id
from crew.state.home import get_owner_runtime_home


@pytest.fixture(autouse=True)
def _clean_cache():
    _clear_file_cache()
    instructions._read_source.cache_clear()
    yield
    _clear_file_cache()
    instructions._read_source.cache_clear()


def _patch_counting_profile_reads(monkeypatch) -> tuple[dict[str, int], list[int]]:
    """记录 profile 的有界文件读取及调用线程。"""
    real = prompt_builder.read_bounded_markdown
    counter = {"n": 0}
    idents: list[int] = []

    def counting(path, *args, **kwargs):
        counter["n"] += 1
        idents.append(threading.get_ident())
        return real(path, *args, **kwargs)

    monkeypatch.setattr(prompt_builder, "read_bounded_markdown", counting)
    return counter, idents


def test_profile_read_is_cached_by_mtime(tmp_path, monkeypatch):
    """mtime 未变不重复读盘；mtime 变化后重新读盘。"""
    counter, _ = _patch_counting_profile_reads(monkeypatch)
    profile = tmp_path / "profile.md"
    profile.write_text("v1", encoding="utf-8")

    assert _load_profile(str(profile)) == "v1"
    assert _load_profile(str(profile)) == "v1"
    assert counter["n"] == 1  # 第二次命中缓存

    # 修改内容并推进 mtime → 缓存失效，重新读盘
    st = profile.stat()
    profile.write_text("v2", encoding="utf-8")
    os.utime(profile, (st.st_atime + 10, st.st_mtime + 10))
    assert _load_profile(str(profile)) == "v2"
    assert counter["n"] == 2


async def test_project_rules_are_cached_off_loop_and_reloaded_after_replacement(tmp_path, monkeypatch):
    path = tmp_path / "CREW.md"
    path.write_text("规则甲", encoding="utf-8")
    opens = []
    real_open = instructions.open_verified_read

    def record_open(path):
        opens.append(threading.get_ident())
        return real_open(path)

    monkeypatch.setattr(instructions, "open_verified_read", record_open)
    for _ in range(2):
        parts = await build_prompt_parts(cwd=str(tmp_path))
        assert "## 项目规则（CREW.md）\n\n规则甲" in parts["user_reminder"]
    assert len(opens) == 1

    # Replace the file while preserving its size and modification time.
    previous = path.stat()
    replacement = tmp_path / "replacement.md"
    replacement.write_text("规则乙", encoding="utf-8")
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(path)
    parts = await build_prompt_parts(cwd=str(tmp_path))
    assert "规则乙" in parts["user_reminder"] and "规则甲" not in parts["user_reminder"]
    assert len(opens) == 2 and threading.get_ident() not in opens


@pytest.mark.parametrize("linked", [False, True])
def test_project_rules_prefer_nearest_directory_then_filename(tmp_path, linked):
    checkout = tmp_path / "checkout"
    cwd = checkout / "packages" / "child"
    cwd.mkdir(parents=True)
    marker = checkout / ".git"
    if linked:
        marker.write_text("gitdir: ../primary/.git/worktrees/checkout\n", encoding="utf-8")
    else:
        marker.mkdir()
    outside = tmp_path / "AGENTS.md"
    outside.write_text("outer-project", encoding="utf-8")
    root_rules = checkout / "AGENTS.md"
    root_rules.write_text("root-rules", encoding="utf-8")

    def load():
        return prompt_builder.build_context_files_prompt(cwd=str(cwd))

    assert "root-rules" in load()
    for name in ("CREW.md", ".crew.md", "AGENTS.md"):
        (cwd / name).write_text(f"nested-{name}", encoding="utf-8")
        prompt = load()
        assert f"## 项目规则（packages/child/{name}）\n\nnested-{name}" in prompt
        assert "root-rules" not in prompt
    for name in ("CREW.md", ".crew.md", "AGENTS.md"):
        (cwd / name).unlink()
    root_rules.unlink()
    assert "outer-project" not in load()


def test_non_git_and_managed_workspace_do_not_inherit_unrelated_ancestors(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("install-rules", encoding="utf-8")
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "app-data"))
    managed = get_owner_runtime_home(create=False) / "task_workspaces" / "default"
    managed.mkdir(parents=True)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    assert "install-rules" not in prompt_builder.build_context_files_prompt(str(unrelated))

    (tmp_path / ".git").mkdir()
    assert "install-rules" not in prompt_builder.build_context_files_prompt(str(managed))
    (managed / "AGENTS.md").write_text("managed-rules", encoding="utf-8")
    assert "managed-rules" in prompt_builder.build_context_files_prompt(str(managed))


async def test_owner_instructions_are_scoped_and_precede_workspace_settings_and_files(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("checkout-scope", encoding="utf-8")
    for owner in ("A:project-a", "A:project-b"):
        (get_owner_runtime_home(owner) / "AGENTS.md").write_text(f"only-{owner}", encoding="utf-8")

    for owner, other in (("A:project-a", "A:project-b"), ("A:project-b", "A:project-a")):
        token = current_owner_account_id.set(owner)
        try:
            reminder = (await build_prompt_parts(cwd=str(workspace), workspace_instructions="ui-scope"))["user_reminder"]
            assert reminder.index(f"only-{owner}") < reminder.index("ui-scope") < reminder.index("checkout-scope")
            assert f"only-{other}" not in reminder
            child = (await build_prompt_parts(cwd=str(workspace), lightweight=True))["user_reminder"]
            assert f"only-{owner}" not in child and "checkout-scope" not in child
        finally:
            current_owner_account_id.reset(token)


def test_project_instruction_read_has_utf8_safe_byte_limit(tmp_path, caplog):
    prefix = b"a" * (instructions._MAX_SOURCE_BYTES - 1)
    (tmp_path / "AGENTS.md").write_bytes(prefix + "中not-visible".encode())
    with caplog.at_level("INFO", logger=instructions.__name__):
        prompt = prompt_builder.build_context_files_prompt(str(tmp_path))
        assert prompt_builder.build_context_files_prompt(str(tmp_path)) == prompt
    assert "a" * len(prefix) in prompt
    assert "中not-visible" not in prompt and "\ufffd" not in prompt
    assert "只载入前 100 KiB" in prompt
    assert caplog.text.count("agent_instruction_truncated") == 1
    assert str(tmp_path) not in caplog.text


def test_project_instruction_read_failure_leaves_workspace_prompt_usable(tmp_path, caplog):
    (tmp_path / "AGENTS.md").write_bytes(b"private-instruction\xff")
    prompt = prompt_builder.build_context_files_prompt(str(tmp_path), workspace_instructions="settings-rule")
    assert "settings-rule" in prompt and "private-instruction" not in prompt
    assert "agent_instruction_read_failed" in caplog.text
    assert "file=AGENTS.md error=UnicodeDecodeError" in caplog.text
    assert "private-instruction" not in caplog.text and str(tmp_path) not in caplog.text


async def test_build_prompt_parts_reads_off_main_thread(tmp_path, monkeypatch):
    """build_prompt_parts 的读盘在工作线程执行，不在调用方（事件循环）线程。"""
    counter, idents = _patch_counting_profile_reads(monkeypatch)
    main_ident = threading.get_ident()
    profile = tmp_path / "profile.md"
    profile.write_text("画像", encoding="utf-8")

    parts = await build_prompt_parts(profile_path=str(profile), cwd=None)
    assert "画像" in parts["system_static"]
    assert counter["n"] >= 1
    assert len(idents) == counter["n"]
    assert all(t != main_ident for t in idents)


async def test_model_window_bounds_independent_sources_and_keeps_rule_provenance(tmp_path, monkeypatch):
    checkout = tmp_path / "workspace"
    checkout.mkdir()
    (checkout / "AGENTS.md").write_text("项目首部\n" + "中🚀" * 40_000 + "\n项目末部", encoding="utf-8")
    isolated_home = tmp_path / "prompt-fixture-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CREW_HOME", str(isolated_home))
    # Build every file under pytest's temporary fixture, independently of the shell profile.
    monkeypatch.setattr(instructions, "get_owner_runtime_home", lambda **_: isolated_home)
    import crew.state.home as state_home
    monkeypatch.setattr(state_home, "get_owner_runtime_home", lambda **_: isolated_home)
    (isolated_home / "AGENTS.md").write_text("用户首部\n" + "规则" * 40_000 + "\n用户末部", encoding="utf-8")
    paths = isolated_home / "memories"
    paths.mkdir()
    for name in ("MEMORY", "USER"):
        (paths / f"{name}.md").write_text(f"{name}-start\n" + "事实" * 50_000 + f"\n{name}-end", encoding="utf-8")
    monkeypatch.setattr(prompt_builder, "build_skills_index_prompt", lambda **_: "")
    kwargs = dict(
        cwd=str(checkout), workspace_instructions="设置首部\n" + "配置" * 40_000 + "\n设置末部",
        memory_text="召回首部\n" + "历史" * 40_000 + "\n召回末部",
        available_tool_names=frozenset({"file_read"}),
    )
    small = (await build_prompt_parts(**kwargs, context_window=4_096))["user_reminder"]
    large = (await build_prompt_parts(**kwargs, context_window=64_000))["user_reminder"]
    headings = [
        "## 用户级指令（AGENTS.md）", "# 项目提示词", "## 项目规则（AGENTS.md）",
        "# 持久记忆", "# 用户画像", "# 会话记忆",
    ]
    for first, next_heading in zip(headings, headings[1:] + ["当前时间："]):
        assert small.index(first) < small.index(next_heading)
    for marker in ("用户首部", "设置首部", "项目首部", "MEMORY-start", "USER-start", "召回首部"):
        assert marker in small
    for marker in ("用户末部", "设置末部", "项目末部", "MEMORY-end", "USER-end", "召回末部"):
        assert marker not in small and marker not in large
    assert small.count("已截断") == len(headings)
    assert "\ufffd" not in small
    display = small[small.index(headings[0]):small.index("当前时间：")].strip()
    assert estimate_text_tokens(display) <= 4_096 // 8
    assert abs(sum(source_token_budgets(None).values()) - 8_192) < 40
    assert len(large) > len(small) * 3


async def test_workspace_facts_and_truncation_do_not_advertise_hidden_tools(tmp_path):
    (tmp_path / "AGENTS.md").write_text("规则" * 20_000, encoding="utf-8")
    parts = await build_prompt_parts(
        cwd=str(tmp_path), context_window=8_192, available_tool_names=frozenset({"grep"}),
    )
    reminder = parts["user_reminder"]
    assert "grep" in reminder and "file_read" not in reminder and "terminal" not in reminder
    assert "用户拒绝任一安全审批" in reminder and "runtime_protocol_mismatch" in reminder
    assert "其余规则" in reminder and "记忆工具" not in parts["system_static"]
    assert "子智能体委派纪律" not in parts["system_static"]
