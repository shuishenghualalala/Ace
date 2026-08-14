"""工具系统：注册表 + 内置工具真实执行。"""

import base64
import json

import pytest

from crew.core.runctx import current_agent_workdir
from crew.core.types import ToolCall
from crew.tools.registry import FunctionTool, Registry, register_builtin_tools, tool_result


@pytest.fixture
def registry():
    r = Registry()
    register_builtin_tools(r)
    return r


async def test_terminal_tool_executes(registry):
    tc = ToolCall(id="c1", name="terminal", arguments={"command": "echo hi-crew"})
    result = await registry.execute(tc)
    assert not result.is_error
    assert "hi-crew" in result.content


async def test_file_write_then_read(registry, tmp_path):
    p = tmp_path / "demo.txt"
    w = await registry.execute(ToolCall("c1", "file_write", {"path": str(p), "content": "你好"}))
    assert not w.is_error
    r = await registry.execute(ToolCall("c2", "file_read", {"path": str(p)}))
    assert not r.is_error
    payload = json.loads(r.content)
    assert payload["content"] == "你好"


async def test_file_delete_removes_one_exact_file(registry, tmp_path):
    target = tmp_path / "remove-me.txt"
    target.write_text("temporary", encoding="utf-8")

    deleted = await registry.execute(
        ToolCall("c3", "file_delete", {"path": str(target)})
    )

    assert not deleted.is_error
    assert json.loads(deleted.content) == {
        "success": True,
        "path": str(target),
        "deleted": True,
        "bytes_deleted": 9,
    }
    assert not target.exists()


async def test_file_delete_refuses_directories_and_symlinks(registry, tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    refused_directory = await registry.execute(
        ToolCall("d1", "file_delete", {"path": str(directory)})
    )
    assert refused_directory.is_error
    assert directory.is_dir()

    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前平台无法创建符号链接")
    refused_link = await registry.execute(
        ToolCall("d2", "file_delete", {"path": str(link)})
    )
    assert refused_link.is_error
    assert target.read_text(encoding="utf-8") == "keep"


async def test_builtin_file_tools_resolve_relative_paths_from_agent_workdir(registry, tmp_path):
    token = current_agent_workdir.set(str(tmp_path))
    try:
        w = await registry.execute(ToolCall("c1", "file_write", {"path": "demo.txt", "content": "隔离"}))
        assert not w.is_error
        assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "隔离"

        r = await registry.execute(ToolCall("c2", "file_read", {"path": "demo.txt"}))
        assert not r.is_error
        payload = json.loads(r.content)
        assert payload["content"] == "隔离"
    finally:
        current_agent_workdir.reset(token)


async def test_terminal_runs_inside_agent_workdir(registry, tmp_path):
    token = current_agent_workdir.set(str(tmp_path))
    try:
        result = await registry.execute(ToolCall("c1", "terminal", {"command": "pwd > marker.txt"}))
        assert not result.is_error
        assert (tmp_path / "marker.txt").read_text(encoding="utf-8").strip() == str(tmp_path)
        payload = json.loads(result.content)
        assert payload["cwd"] == str(tmp_path)
        assert payload["exit_code"] == 0
    finally:
        current_agent_workdir.reset(token)


async def test_unknown_tool_returns_error(registry):
    r = await registry.execute(ToolCall("c1", "nope", {}))
    assert r.is_error


def test_list_schemas_filter(registry):
    only = registry.list_schemas(["terminal"])
    assert len(only) == 1
    assert only[0]["function"]["name"] == "terminal"


def test_tool_ui_metadata_is_not_exposed_to_llm_schema(registry):
    terminal_schema = registry.list_schemas(["terminal"])[0]["function"]
    assert "display_name" not in terminal_schema
    assert "ui_label_template" not in terminal_schema
    assert registry.ui_meta("terminal")["ui_label_template"] == "运行 {command}"
    assert registry.render_ui_label("terminal", {"command": "echo hi"}) == "运行 echo hi"


def test_ui_label_shrinks_long_absolute_paths(registry):
    # 长绝对路径只留 basename，其余参数原样保留
    label = registry.render_ui_label(
        "terminal",
        {"command": 'python3 /Users/x/Documents/Codes/Crew/crew/skills/mail-assistant/scripts/search_mail.py --sid="abc" --channel=web'},
    )
    assert label == '运行 python3 search_mail.py --sid="abc" --channel=web'
    # file_read 的 path 参数同样收缩
    assert registry.render_ui_label("file_read", {"path": "/Users/x/docs/readme.md"}) == "读取 readme.md"
    # URL、短路径、相对路径不动
    label = registry.render_ui_label(
        "terminal",
        {"command": "curl https://example.com/aaaaaaaaaaaaaaaaaaaaaaaa 2>/dev/null && cat docs/readme.md"},
    )
    assert "https://example.com/aaaaaaaaaaaaaaaaaaaaaaaa" in label
    assert "/dev/null" in label
    assert "docs/readme.md" in label
    # heredoc / 多行命令压成单行
    label = registry.render_ui_label("terminal", {"command": "python3 << 'EOF'\nimport json\nEOF"})
    assert "\n" not in label


async def test_crew_tool_registration_executes():
    r = Registry()
    r.register(
        name="echo_json",
        toolset="demo",
        schema={
            "name": "echo_json",
            "description": "Echo a value",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
        handler=lambda args: tool_result(value=args["value"]),
    )

    result = await r.execute(ToolCall("c1", "echo_json", {"value": "ok"}))
    assert not result.is_error
    assert '"ok"' in result.content


def test_toolset_filter_uses_toolset_metadata(registry):
    schemas = registry.list_schemas(enabled_toolsets=["file"])
    names = {item["function"]["name"] for item in schemas}
    assert {"file_read", "file_write", "file_delete", "glob", "grep", "patch"} <= names

    without_file = registry.list_schemas(disabled_toolsets=["file"])
    names = {item["function"]["name"] for item in without_file}
    assert "terminal" in names
    assert "file_read" not in names


def test_tool_name_filter(registry):
    schemas = registry.list_schemas(enabled_tools=["terminal", "web_search"])
    names = {item["function"]["name"] for item in schemas}
    assert names == {"terminal", "web_search"}

    without_web_search = registry.list_schemas(disabled_tools=["web_search"])
    names = {item["function"]["name"] for item in without_web_search}
    assert "terminal" in names
    assert "web_search" not in names

    all_disabled = registry.list_schemas(disabled_tools=["*"])
    assert all_disabled == []


def test_tool_name_filter_combined_with_toolset(registry):
    schemas = registry.list_schemas(
        enabled_toolsets=["web"], disabled_tools=["web_search"]
    )
    names = {item["function"]["name"] for item in schemas}
    assert "web_search" not in names
    assert "web_extract" in names


def test_builtin_tools_are_crew_function_tools(registry):
    assert isinstance(registry.get("terminal"), FunctionTool)
    assert isinstance(registry.get("file_read"), FunctionTool)
    assert isinstance(registry.get("file_write"), FunctionTool)
    assert isinstance(registry.get("file_delete"), FunctionTool)


async def test_grep_and_patch(registry, tmp_path):
    p = tmp_path / "demo.txt"
    p.write_text("hello old world", encoding="utf-8")

    found = await registry.execute(ToolCall("c1", "grep", {"pattern": "old", "path": str(tmp_path)}))
    assert not found.is_error
    assert "demo.txt" in found.content

    patched = await registry.execute(ToolCall("c2", "patch", {"path": str(p), "old": "old", "new": "new"}))
    assert not patched.is_error
    assert "replacements" in patched.content
    assert p.read_text(encoding="utf-8") == "hello new world"


async def test_file_tools_use_agent_workdir_for_relative_paths(registry, tmp_path):
    (tmp_path / "demo.txt").write_text("hello isolated world", encoding="utf-8")
    token = current_agent_workdir.set(str(tmp_path))
    try:
        found = await registry.execute(ToolCall("c1", "grep", {"pattern": "isolated"}))
        assert not found.is_error
        assert "demo.txt" in found.content

        patched = await registry.execute(ToolCall("c2", "patch", {"path": "demo.txt", "old": "isolated", "new": "task"}))
        assert not patched.is_error
        assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "hello task world"
    finally:
        current_agent_workdir.reset(token)


async def test_memory_tool(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))

    mem = await registry.execute(ToolCall("m1", "memory", {"action": "write", "text": "Crew 喜欢清晰工具"}))
    assert not mem.is_error
    mem_search = await registry.execute(ToolCall("m2", "memory", {"action": "search", "query": "工具"}))
    assert "清晰工具" in mem_search.content
    # 注：会话级 todo 工具改由 register_plan_tools 注册，
    # 其测试见 tests/test_plan_mode.py。


async def test_skills_list_and_view(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()
    skill_dir = tmp_path / ".crew" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n\n# Demo\nUse it.",
        encoding="utf-8",
    )

    listed = await registry.execute(ToolCall("s1", "skills_list", {}))
    assert "Demo skill" in listed.content
    viewed = await registry.execute(ToolCall("s2", "skill_view", {"name": "demo"}))
    assert "Use it." in viewed.content


async def test_skills_audit_tool_reports_missing_metadata(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()
    skill_dir = tmp_path / ".crew" / "skills" / "legacy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: legacy\ndescription: Legacy\n---\n技能正文",
        encoding="utf-8",
    )

    result = await registry.execute(ToolCall("s3", "skills_audit", {"only": "legacy"}))
    assert not result.is_error
    payload = json.loads(result.content)
    codes = {f["code"] for f in payload["skills"][0]["findings"]}
    assert "missing_metadata_zh_name" in codes


async def test_skills_repair_tool_fixes_skill(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    import crew.agent.skills as skills_mod
    skills_mod._cache = {}
    skills_mod._cache_key = ()

    async def fake_generate(skill_md, frontmatter, body):  # noqa: ANN001
        return {
            "zh_name": "修复测试",
            "zh_description": "用于验证自动修复技能。",
            "query_examples": ["帮我修复这个技能"],
        }

    monkeypatch.setattr("crew.agent.skills.generate_skill_metadata_with_model", fake_generate)

    skill_dir = tmp_path / ".crew" / "skills" / "legacy"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: legacy\ndescription: Legacy\n---\n技能正文",
        encoding="utf-8",
    )

    result = await registry.execute(ToolCall("s4", "skills_repair", {"only": "legacy"}))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["ok"] is True
    content = skill_md.read_text(encoding="utf-8")
    assert "zh_name: 修复测试" in content
    assert "技能正文" in content


async def test_skills_repair_authorizes_registered_directory_before_mutation(monkeypatch):
    from crew.tools import skills_tools

    pending = {
        "skills": [{
            "skill_dir": "/registered/skill",
            "findings": [{"code": "missing_metadata_zh_name"}],
        }]
    }
    monkeypatch.setattr(skills_tools, "audit_skills", lambda **_kwargs: pending)
    monkeypatch.setattr("crew.agent.skills._is_metadata_finding", lambda _finding: True)
    authorized = []

    async def authorize(args, **kwargs):
        authorized.append((args, kwargs))

    async def repair(**_kwargs):
        return {"ok": True, "skills": []}

    monkeypatch.setattr(skills_tools, "authorize_file_tool", authorize)
    monkeypatch.setattr(skills_tools, "repair_skills", repair)

    await skills_tools.handle_skills_repair(
        {"only": "demo"},
        workspace_store=object(),
        security_service=object(),
    )

    assert len(authorized) == 1
    assert authorized[0][0] == {"path": "/registered/skill"}
    assert authorized[0][1]["operation"] == "write"
    assert authorized[0][1]["tool_name"] == "skills_repair"


def test_default_registry_does_not_install_legacy_fake_browser(registry):
    # Browser Use is registered by build_app with an account-scoped
    # BrowserManager. The old local-file HTTP parser must never masquerade as a
    # real browser when the bundled runtime is absent.
    assert "browser_navigate" not in registry.names()


async def test_vision_analyze_png(registry, tmp_path):
    png = tmp_path / "tiny.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))
    result = await registry.execute(ToolCall("v1", "vision_analyze", {"path": str(png)}))
    assert not result.is_error
    assert '"width": 1' in result.content


async def test_vision_analyze_uses_file_authorization(tmp_path, monkeypatch):
    from crew.tools import web_tools

    png = tmp_path / "authorized.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))
    seen = {}

    async def authorize(args, **kwargs):
        seen.update({"args": args, **kwargs})
        return png

    monkeypatch.setattr(web_tools, "authorize_file_tool", authorize)
    payload = json.loads(await web_tools.handle_vision_analyze(
        {"path": "/untrusted/model/path.png"},
        workspace_store=object(),
        security_service=object(),
    ))

    assert payload["image"]["path"] == str(png)
    assert seen["operation"] == "read"
    assert seen["tool_name"] == "vision_analyze"


async def test_file_read_pagination(registry, tmp_path):
    p = tmp_path / "lines.txt"
    p.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

    r = await registry.execute(ToolCall("c1", "file_read", {"path": str(p), "offset": 2, "limit": 2}))
    assert not r.is_error
    payload = json.loads(r.content)
    assert payload["offset"] == 2
    assert payload["limit"] == 2
    assert payload["content"] == "line2\nline3\n"
    assert payload["total_lines"] == 5


async def test_file_read_preserves_bom(registry, tmp_path):
    p = tmp_path / "bom.txt"
    original = "﻿hello world\r\nsecond line\r\n"
    p.write_text(original, encoding="utf-8")

    r = await registry.execute(ToolCall("c1", "file_read", {"path": str(p)}))
    assert not r.is_error
    payload = json.loads(r.content)
    assert payload["content"] == "hello world\r\nsecond line\r\n"

    # overwrite should keep BOM and CRLF
    w = await registry.execute(ToolCall("c2", "file_write", {"path": str(p), "content": "hello universe\r\nsecond line\r\n"}))
    assert not w.is_error
    written = p.read_bytes()
    assert written.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in written


async def test_file_write_append(registry, tmp_path):
    p = tmp_path / "append.txt"
    w1 = await registry.execute(ToolCall("c1", "file_write", {"path": str(p), "content": "first"}))
    assert not w1.is_error
    w2 = await registry.execute(ToolCall("c2", "file_write", {"path": str(p), "content": "second", "append": True}))
    assert not w2.is_error
    assert p.read_text(encoding="utf-8") == "firstsecond"


async def test_patch_preserves_line_endings(registry, tmp_path):
    p = tmp_path / "crlf.txt"
    p.write_text("hello old world\r\nnext\r\n", encoding="utf-8")

    patched = await registry.execute(ToolCall("c1", "patch", {"path": str(p), "old": "old", "new": "new"}))
    assert not patched.is_error
    payload = json.loads(patched.content)
    assert payload["success"] is True
    assert payload["replacements"] == 1
    assert b"\r\n" in p.read_bytes()


async def test_grep_glob_filter_and_pagination(registry, tmp_path):
    (tmp_path / "a.py").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.py").write_text("alpha beta", encoding="utf-8")
    (tmp_path / "c.txt").write_text("alpha", encoding="utf-8")

    # glob 过滤：只搜 *.py，a.py 与 b.py 命中，c.txt 被排除
    r = await registry.execute(ToolCall("c1", "grep", {
        "pattern": "alpha", "path": str(tmp_path), "glob": "*.py", "output_mode": "files_with_matches",
    }))
    assert not r.is_error
    payload = json.loads(r.content)
    assert payload["num_files"] == 2
    assert all(f.endswith(".py") for f in payload["files"])

    # 分页：offset=1 head_limit=1 → 跳过第 1 个，返回 1 个
    r2 = await registry.execute(ToolCall("c2", "grep", {
        "pattern": "alpha", "path": str(tmp_path), "glob": "*.py",
        "output_mode": "files_with_matches", "offset": 1, "head_limit": 1,
    }))
    payload2 = json.loads(r2.content)
    assert len(payload2["files"]) == 1
    assert payload2["files"][0].endswith(".py")


async def test_terminal_dangerous_command_blocked(registry):
    r = await registry.execute(ToolCall("c1", "terminal", {"command": "rm -rf /"}))
    assert not r.is_error
    payload = json.loads(r.content)
    assert payload["success"] is False
    assert "BLOCKED" in payload["error"]


async def test_terminal_dangerous_command_with_force(registry, tmp_path):
    token = current_agent_workdir.set(str(tmp_path))
    try:
        r = await registry.execute(ToolCall("c1", "terminal", {"command": "git reset --hard", "force": True}))
        assert not r.is_error
        payload = json.loads(r.content)
        # git reset --hard in an empty dir fails, but should not be blocked
        assert "BLOCKED" not in payload.get("error", "")
    finally:
        current_agent_workdir.reset(token)


async def test_terminal_background(registry, tmp_path):
    token = current_agent_workdir.set(str(tmp_path))
    try:
        r = await registry.execute(ToolCall("c1", "terminal", {"command": "sleep 0.1", "background": True}))
        assert not r.is_error
        payload = json.loads(r.content)
        assert payload["background"] is True
        assert "pid" in payload
    finally:
        current_agent_workdir.reset(token)


async def test_sensitive_write_path_blocked(registry, tmp_path):
    # absolute /etc path should be blocked
    r = await registry.execute(ToolCall("c1", "file_write", {"path": "/etc/crew_test_please_ignore.txt", "content": "x"}))
    assert r.is_error
    assert "Refusing" in r.content

    # patch on sensitive path should also be blocked
    r2 = await registry.execute(ToolCall("c2", "patch", {"path": "/etc/passwd", "old": "x", "new": "y"}))
    assert r2.is_error
    assert "Refusing" in r2.content


async def test_blocked_device_read(registry):
    r = await registry.execute(ToolCall("c1", "file_read", {"path": "/dev/urandom"}))
    assert r.is_error
    assert "禁止读取" in r.content


async def test_ask_followup_question_returns_user_answers():
    import asyncio

    from crew.core.followup import drain_followup_answer_messages, resolve_answer
    from crew.core.runctx import current_push_fn, current_request_id, current_session_id
    from crew.tools.interaction import handle_ask_followup_question

    session_id = "test-followup-session"
    sid_token = current_session_id.set(session_id)
    rid_token = current_request_id.set("req-followup-test")
    pushed: list[tuple[str, dict]] = []

    async def mock_push(sid: str, payload: dict) -> None:
        pushed.append((sid, payload))

    push_token = current_push_fn.set(mock_push)

    async def answer_after_short_delay():
        await asyncio.sleep(0.05)
        assert len(pushed) == 1
        question_id = pushed[0][1]["body"]["question_id"]
        resolve_answer(session_id, question_id, [{"question_id": "q1", "answers": ["选项B"]}])

    try:
        answerer = asyncio.create_task(answer_after_short_delay())
        result = await handle_ask_followup_question({
            "title": "测试",
            "questions": [
                {"id": "q1", "question": "选哪个？", "options": ["选项A", "选项B"], "multiSelect": False},
            ],
        })
        await answerer
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["answers"] == [{"question_id": "q1", "answers": ["选项B"]}]
        assert pushed[0][1]["kind"] == "followup_question"
        assert pushed[0][1]["body"]["questions"][0]["options"] == [
            {"label": "选项A", "value": "选项A"},
            {"label": "选项B", "value": "选项B"},
        ]
        assert pushed[0][1]["body"]["questions"][0]["allowFreeText"] is True
        assert pushed[0][1]["body"]["record_history"] is True
        assert "note" not in pushed[0][1]["body"]
        assert pushed[0][1]["request_id"] == "req-followup-test"
        assert drain_followup_answer_messages(session_id) == ["已选择：选项B"]
        assert drain_followup_answer_messages(session_id) == []
    finally:
        current_session_id.reset(sid_token)
        current_request_id.reset(rid_token)
        current_push_fn.reset(push_token)


def test_followup_question_options_accept_label_value_objects():
    from crew.core.followup import FollowupWaiter, validate_questions

    questions = validate_questions([{
        "id": "q1",
        "question": "请选择继续方式",
        "options": [
            {"label": "简短结论", "value": "short", "description": "只给出最终答案"},
            {"label": "风险分析", "value": "risk", "detail": "列出主要风险点"},
        ],
    }])

    assert questions[0]["options"] == [
        {"label": "简短结论", "value": "short", "description": "只给出最终答案"},
        {"label": "风险分析", "value": "risk", "description": "列出主要风险点"},
    ]

    waiter = FollowupWaiter()
    qid = waiter.create("s-label-value", questions)
    assert waiter.resolve("s-label-value", qid, [{"question_id": "q1", "answers": ["risk"]}]) is True
    assert waiter.drain_answer_messages("s-label-value") == ["已选择：风险分析"]


async def test_followup_status_reuses_the_existing_question_channel():
    from crew.core.followup import send_followup_status_to

    pushed = []

    async def push(session_id, payload):
        pushed.append((session_id, payload))

    assert await send_followup_status_to(
        "session-1",
        "question-1",
        "applied",
        note="协作助手已加入，继续开工。",
        push_fn=push,
    ) is True
    assert pushed[0][0] == "session-1"
    assert pushed[0][1]["kind"] == "followup_question"
    assert pushed[0][1]["body"] == {
        "question_id": "question-1",
        "status": "applied",
        "note": "协作助手已加入，继续开工。",
    }


async def test_ask_followup_question_validation_errors():
    from crew.core.errors import ToolError
    from crew.tools.interaction import handle_ask_followup_question

    with pytest.raises(ToolError, match="questions 必须是非空数组"):
        await handle_ask_followup_question({"questions": []})

    with pytest.raises(ToolError, match=r"questions\[0\] 必须是对象"):
        await handle_ask_followup_question({"questions": ["bad"]})

    with pytest.raises(ToolError, match=r"questions\[0\]\.question 不能为空"):
        await handle_ask_followup_question({"questions": [{"question": "", "options": ["A"]}]})

    with pytest.raises(ToolError, match=r"questions\[0\]\.options 必须是非空数组"):
        await handle_ask_followup_question({"questions": [{"question": "?", "options": []}]})


def test_followup_question_text_input_accepts_empty_options():
    from crew.core.followup import FollowupWaiter, validate_questions

    questions = validate_questions([{
        "id": "q1",
        "question": "请补充合同正文或文件路径",
        "inputMode": "text",
    }])

    assert questions[0]["inputMode"] == "text"
    assert questions[0]["options"] == []

    waiter = FollowupWaiter()
    qid = waiter.create("s-text", questions)
    assert waiter.resolve("s-text", qid, [{"question_id": "q1", "answers": ["合同在 /tmp/a.pdf"]}]) is True
    assert waiter.drain_answer_messages("s-text") == ["已补充：请补充合同正文或文件路径：合同在 /tmp/a.pdf"]


async def test_ask_followup_question_no_push_fn_raises():
    from crew.core.errors import ToolError
    from crew.core.runctx import current_push_fn, current_session_id
    from crew.tools.interaction import handle_ask_followup_question

    sid_token = current_session_id.set("s1")
    push_token = current_push_fn.set(None)
    try:
        with pytest.raises(ToolError, match="无 push"):
            await handle_ask_followup_question({"questions": [{"question": "?", "options": ["A"]}]})
    finally:
        current_session_id.reset(sid_token)
        current_push_fn.reset(push_token)


async def test_ask_followup_question_cancelled_by_user():
    """用户点「取消」：cancel_followup 回灌取消标记，handler 返回 success=False 且不抛异常。"""
    import asyncio

    from crew.core.followup import cancel_followup
    from crew.core.runctx import current_push_fn, current_session_id
    from crew.tools.interaction import handle_ask_followup_question

    session_id = "test-followup-cancel"
    sid_token = current_session_id.set(session_id)
    pushed: list[tuple[str, dict]] = []

    async def mock_push(sid: str, payload: dict) -> None:
        pushed.append((sid, payload))

    push_token = current_push_fn.set(mock_push)

    async def cancel_after_short_delay():
        await asyncio.sleep(0.05)
        question_id = pushed[0][1]["body"]["question_id"]
        assert cancel_followup(session_id, question_id) is True

    try:
        canceller = asyncio.create_task(cancel_after_short_delay())
        result = await handle_ask_followup_question({
            "questions": [{"id": "q1", "question": "选哪个？", "options": ["A", "B"]}],
        })
        await canceller
        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["answers"] == []
        assert "取消" in payload["note"]
    finally:
        current_session_id.reset(sid_token)
        current_push_fn.reset(push_token)


async def test_cancel_followup_returns_false_when_expired():
    """对不存在或已结束的追问取消，应返回 False 而非抛异常。"""
    from crew.core.followup import cancel_followup

    assert cancel_followup("no-such-session", "no-such-question") is False


# ---------------------------------------------------------------------------
# 输出后处理：去 ANSI / 头尾截断 / 阈值可配 / 脱敏
# ---------------------------------------------------------------------------


def test_strip_ansi_removes_escape_sequences():
    from crew.tools.output_filters import strip_ansi

    colored = "\x1b[32mPASS\x1b[0m tests/test_foo.py \x1b[90m[100%]\x1b[0m"
    assert strip_ansi(colored) == "PASS tests/test_foo.py [100%]"
    # 干净文本走快速路径原样返回
    assert strip_ansi("plain text") == "plain text"


def test_truncate_output_keeps_head_and_tail():
    from crew.tools.output_filters import truncate_output

    text = "H" * 1000 + "M" * 1000 + "T" * 1000  # 3000 字符
    out, truncated = truncate_output(text, max_chars=1000)
    assert truncated is True
    # 头 40% = 400 个 H，尾 60% = 600 个 T
    assert out.startswith("H" * 400)
    assert out.endswith("T" * 600)
    assert "省略" in out and "字符" in out
    # 中间的 M 段被挖掉
    assert "M" * 100 not in out


def test_truncate_output_passthrough_when_short():
    from crew.tools.output_filters import truncate_output

    out, truncated = truncate_output("short output", max_chars=1000)
    assert truncated is False
    assert out == "short output"


def test_get_max_output_chars_reads_config(monkeypatch):
    import crew.tools.output_filters as of

    class _Cfg:
        raw_config = {"tools": {"terminal": {"max_output": 12345}}}

    monkeypatch.setattr("crew.state.config.load_config", lambda: _Cfg())
    assert of.get_max_output_chars() == 12345


def test_get_max_output_chars_default_on_error(monkeypatch):
    import crew.tools.output_filters as of

    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("crew.state.config.load_config", _boom)
    assert of.get_max_output_chars() == of.DEFAULT_MAX_OUTPUT_CHARS


def test_redact_sensitive_text_masks_secrets():
    from crew.tools.redact import redact_sensitive_text

    # OpenAI 风格 key
    masked = redact_sensitive_text("export OPENAI_API_KEY=sk-abcdef1234567890ABCDEF")
    assert "sk-abcdef1234567890ABCDEF" not in masked
    # 普通文本原样
    assert redact_sensitive_text("hello world") == "hello world"


async def test_terminal_output_is_cleaned_and_redacted(registry, tmp_path):
    token = current_agent_workdir.set(str(tmp_path))
    try:
        # 输出里同时含 ANSI 颜色码和一个 key
        cmd = "printf '\\033[31msk-abcdef1234567890ABCDEF\\033[0m\\n'"
        r = await registry.execute(ToolCall("c1", "terminal", {"command": cmd}))
        assert not r.is_error
        payload = json.loads(r.content)
        assert "\x1b[" not in payload["output"]          # ANSI 已去除
        assert "sk-abcdef1234567890ABCDEF" not in payload["output"]  # key 已脱敏
    finally:
        current_agent_workdir.reset(token)
