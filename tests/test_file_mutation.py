"""文件变更服务（crew.agent.file_mutation）：串行锁、版本 CAS、匹配与事务语义。"""

import asyncio
import json
import os
import stat
import sys
import threading

import pytest

from crew.agent.file_mutation import (
    EditOp,
    EditMatchError,
    StaleFileError,
    clear_file_observations,
    file_mutation,
    plan_edits,
)
from crew.core.runctx import current_owner_account_id, current_session_id
from crew.core.types import ToolCall
from crew.tools.file_utils import snapshot_file
from crew.tools import file_utils
from crew.tools.registry import Registry, register_builtin_tools


@pytest.fixture
def registry():
    r = Registry()
    register_builtin_tools(r)
    return r


@pytest.fixture(autouse=True)
def _clean_observations():
    clear_file_observations()
    yield
    clear_file_observations()


# ---------------------------------------------------------------------------
# 匹配计划（纯函数）
# ---------------------------------------------------------------------------


def test_plan_edits_exact_unique():
    plan = plan_edits("alpha\nbeta\ngamma\n", [EditOp(old="beta", new="BETA")])
    assert plan.after == "alpha\nBETA\ngamma\n"
    assert plan.matches[0].mode == "exact"
    assert plan.matches[0].score == 1.0
    assert plan.matches[0].line == 2


def test_plan_edits_crlf_domain_normalizes():
    plan = plan_edits("a\r\nb\r\n", [EditOp(old="a\nb", new="x")])
    assert plan.after == "x\n"
    assert plan.matches[0].mode == "exact"


def test_plan_edits_not_found_raises():
    with pytest.raises(EditMatchError, match="未找到"):
        plan_edits("hello\n", [EditOp(old="missing", new="x")])


def test_plan_edits_ambiguous_reports_line_numbers():
    with pytest.raises(EditMatchError, match=r"行号: 1, 3"):
        plan_edits("dup\nmid\ndup\n", [EditOp(old="dup", new="x")])


def test_plan_edits_replace_all_with_count_zero():
    plan = plan_edits("one two one\n", [EditOp(old="one", new="1", count=0)])
    assert plan.after == "1 two 1\n"
    assert len(plan.matches) == 2


def test_plan_edits_overlap_rejected():
    with pytest.raises(EditMatchError, match="重叠"):
        plan_edits("abcdef\n", [EditOp(old="abc", new="x"), EditOp(old="cde", new="y")])


# ---------------------------------------------------------------------------
# C1 模糊匹配阶梯
# ---------------------------------------------------------------------------


def test_plan_edits_fuzzy_smart_quotes_and_dash():
    original = 'print(“hello”)\nvalue = 10−5\n'
    plan = plan_edits(original, [EditOp(old='print("hello")', new="pass")])
    assert plan.matches[0].mode == "fuzzy"
    assert 0 < plan.matches[0].score <= 1.0
    assert plan.after == "pass\nvalue = 10−5\n"
    # 未触碰行保持原文逐字节（Unicode 减号未被改写）
    assert "10−5" in plan.after


def test_plan_edits_fuzzy_trailing_whitespace_and_crlf_preserved():
    original = "def f():   \r\n    return 1\r\nkeep‘this\n"
    plan = plan_edits(original, [EditOp(old="def f():\n    return 1", new="def g():\n    return 2")])
    assert plan.matches[0].mode == "fuzzy"
    # 匹配行按模糊域重写，未触碰行逐字节保留（智能引号不被改写）
    assert plan.after == "def g():\n    return 2\nkeep‘this\n"


async def test_edit_fuzzy_writes_back_with_original_endings(tmp_path):
    target = tmp_path / "demo.txt"
    target.write_bytes(b"def f():   \r\n    return 1\r\nkeep\xe2\x80\x98this\xe2\x80\x99\n")

    outcome = await file_mutation.edit_text(
        target, [EditOp(old="def f():\n    return 1", new="def g():\n    return 2")]
    )

    data = target.read_bytes()
    assert outcome.plan.matches[0].mode == "fuzzy"
    # 未触碰行内容逐字节保留；行尾按采样到的 dominant ending 统一还原（既有语义）
    assert data == "def g():\r\n    return 2\r\nkeep‘this’\r\n".encode("utf-8")


def test_plan_edits_fuzzy_ambiguous_reports_lines():
    original = "‘a’\nmiddle\n‘a’\n"
    with pytest.raises(EditMatchError, match=r"行号: 1, 3"):
        plan_edits(original, [EditOp(old="'a'", new="x")])


def test_plan_edits_exact_wins_over_fuzzy():
    # 文件中同时存在 ASCII 原文与智能引号变体：精确命中优先，不变道模糊。
    original = "say 'hi'\nsay ‘hi’\n"
    plan = plan_edits(original, [EditOp(old="say 'hi'", new="x")])
    assert plan.matches[0].mode == "exact"
    assert plan.after == "x\nsay ‘hi’\n"


@pytest.mark.parametrize(("original", "old", "expected"), [
    ("oﬃce=Ａ  ", "ffi", "oXce=Ａ  "),
    ("cafe\u0301 Ａ", "é", "cafX Ａ"),
    ("coﬀee ‘ready’", "'ready'", "coﬀee X"),
    ("Ａ  \nＢ  ", "\nB", "Ａ  X  "),
    ("‘x’  \nＡ", "'x'\n", "XＡ"),
])
def test_fuzzy_match_replaces_only_its_original_span(original, old, expected):
    plan = plan_edits(original, [EditOp(old, "X")])
    match, = plan.matches
    assert match.mode == "fuzzy" and plan.after == expected
    assert original[:match.start] + "X" + original[match.start + match.length:] == expected


@pytest.mark.parametrize(("original", "edits", "code"), [
    ("ﬃ", [EditOp("fi", "x")], "patch_no_match"),
    ("cafe\u0301", [EditOp("café", "x"), EditOp("e", "y")], "patch_overlap"),
    ("‘x’ “x”", [EditOp("x", "y"), EditOp('"x"', "z")], "patch_ambiguous"),
])
def test_invalid_fuzzy_or_overlapping_batch_is_rejected(original, edits, code):
    with pytest.raises(EditMatchError) as error:
        plan_edits(original, edits)
    assert error.value.code == code


async def test_registry_fuzzy_batch_preserves_sibling_values_and_cas(registry, tmp_path):
    target = tmp_path / "目录.json"
    original = '{"label":"“draft”","sku":"Ａ０１","tier":"①","other":"draft"}  \r\n'
    target.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
    path = {"path": str(target)}
    session = current_session_id.set("fuzzy-batch")
    try:
        read = await registry.execute(ToolCall("read", "file_read", path))
        version = json.loads(read.content)["file_version"]
        # The exact 'other' edit remains exact even when the same batch needs
        # normalization to locate a different field.
        edits = {"edits": [
            {"old": '"draft"', "new": '"ready"'},
            {"old": 'label":""draft""', "new": 'label":"final"'},
        ]}
        patch = await registry.execute(ToolCall("patch", "patch", {**path, **edits, "expected_version": version}))
        assert not patch.is_error, patch.content
        assert {match["mode"] for match in json.loads(patch.content)["matches"]} == {"exact", "fuzzy"}
        expected = '{"label":"final","sku":"Ａ０１","tier":"①","other":"ready"}  \r\n'
        written = b"\xef\xbb\xbf" + expected.encode("utf-8")
        assert target.read_bytes() == written
        stale = await registry.execute(ToolCall("stale", "patch", {**path, **edits, "expected_version": version}))
        assert stale.is_error and stale.code == "stale_file"
        assert target.read_bytes() == written
    finally:
        current_session_id.reset(session)


def test_plan_edits_not_found_mentions_fuzzy_attempt():
    with pytest.raises(EditMatchError, match="模糊"):
        plan_edits("completely different\n", [EditOp(old="not there", new="x")])


# ---------------------------------------------------------------------------
# 服务：串行锁 / CAS / 事务
# ---------------------------------------------------------------------------


async def test_concurrent_edits_same_file_serialize(tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("aaa bbb\n", encoding="utf-8")

    results = await asyncio.gather(
        file_mutation.edit_text(target, [EditOp(old="aaa", new="AAA")]),
        file_mutation.edit_text(target, [EditOp(old="bbb", new="BBB")]),
    )
    assert all(r.plan.matches for r in results)
    assert target.read_text(encoding="utf-8") == "AAA BBB\n"


async def test_concurrent_write_and_edit_serialize(tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("seed\n", encoding="utf-8")

    write_task = asyncio.create_task(file_mutation.write_text(target, "aaa\n"))
    edit_task = asyncio.create_task(file_mutation.edit_text(target, [EditOp(old="seed", new="bbb")]))
    results = await asyncio.gather(write_task, edit_task, return_exceptions=True)
    content = target.read_text(encoding="utf-8")
    # 串行语义只有两种合法终态：编辑先跑（随后被覆盖成 aaa），或写入先跑（编辑找不到 seed 报错）。
    edit_error = next((r for r in results if isinstance(r, Exception)), None)
    assert content == "aaa\n"
    if edit_error is not None:
        assert isinstance(edit_error, EditMatchError)


async def test_stale_observed_version_rejected(tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("v1\n", encoding="utf-8")
    observed = snapshot_file(target)

    target.write_text("v2\n", encoding="utf-8")

    with pytest.raises(StaleFileError, match="重新读取"):
        await file_mutation.edit_text(
            target,
            [EditOp(old="v2", new="v3")],
            expected=(observed.device, observed.inode, observed.size, observed.mtime_ns),
        )
    assert target.read_text(encoding="utf-8") == "v2\n"


async def test_edit_preserves_bom_and_crlf(tmp_path):
    target = tmp_path / "demo.txt"
    raw = b"\xef\xbb\xbfa\r\nb\r\n"
    target.write_bytes(raw)

    outcome = await file_mutation.edit_text(target, [EditOp(old="a\nb", new="x\ny")])

    data = target.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert data == b"\xef\xbb\xbfx\r\ny\r\n"
    assert outcome.had_bom and outcome.line_ending == "\r\n"


# ---------------------------------------------------------------------------
# 工具层回归：registry 走通服务
# ---------------------------------------------------------------------------


async def test_patch_reports_match_details(registry, tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("hello old world\n", encoding="utf-8")

    result = await registry.execute(
        ToolCall("c1", "patch", {"path": str(target), "old": "old", "new": "new"})
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["replacements"] == 1
    assert payload["matches"][0]["mode"] == "exact"
    assert payload["matches"][0]["score"] == 1.0
    assert payload["matches"][0]["line"] == 1
    assert target.read_text(encoding="utf-8") == "hello new world\n"


async def test_patch_ambiguous_error_via_registry(registry, tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("dup\nmid\ndup\n", encoding="utf-8")

    result = await registry.execute(
        ToolCall("c1", "patch", {"path": str(target), "old": "dup", "new": "x"})
    )
    assert result.is_error
    assert "行号: 1, 3" in result.content
    assert target.read_text(encoding="utf-8") == "dup\nmid\ndup\n"


async def test_file_write_uses_service_and_guard_disabled_by_default(registry, tmp_path):
    target = tmp_path / "demo.txt"
    token = current_session_id.set("sess-unobserved")
    try:
        result = await registry.execute(
            ToolCall("c1", "file_write", {"path": str(target), "content": "hi"})
        )
        assert not result.is_error
        assert target.read_text(encoding="utf-8") == "hi"
    finally:
        current_session_id.reset(token)


# ---------------------------------------------------------------------------
# C2 多编辑 edits[]（事务化）
# ---------------------------------------------------------------------------


async def test_patch_edits_applies_multiple_in_order(registry, tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    before = target.read_bytes()

    result = await registry.execute(
        ToolCall(
            "c1",
            "patch",
            {
                "path": str(target),
                "edits": [
                    {"old": "gamma", "new": "GAMMA"},
                    {"old": "alpha", "new": "ALPHA"},
                ],
            },
        )
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\n"
    # matches 按文中位置排序上报
    assert [m["edit_index"] for m in payload["matches"]] == [1, 0]
    assert before != target.read_bytes()


async def test_patch_edits_partial_failure_leaves_file_byte_identical(registry, tmp_path):
    target = tmp_path / "demo.txt"
    original = "alpha\nbeta\ngamma\n"
    target.write_text(original, encoding="utf-8")
    before = target.read_bytes()

    result = await registry.execute(
        ToolCall(
            "c1",
            "patch",
            {
                "path": str(target),
                "edits": [
                    {"old": "alpha", "new": "ALPHA"},
                    {"old": "missing", "new": "X"},
                ],
            },
        )
    )
    assert result.is_error
    assert "edits[1]" in result.content
    assert target.read_bytes() == before


async def test_patch_edits_overlap_rejected(registry, tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("abcdef\n", encoding="utf-8")
    before = target.read_bytes()

    result = await registry.execute(
        ToolCall(
            "c1",
            "patch",
            {
                "path": str(target),
                "edits": [
                    {"old": "abc", "new": "x"},
                    {"old": "cde", "new": "y"},
                ],
            },
        )
    )
    assert result.is_error
    assert "重叠" in result.content
    assert target.read_bytes() == before


async def test_patch_edits_duplicate_old_rejected(registry, tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("one two\n", encoding="utf-8")
    before = target.read_bytes()

    result = await registry.execute(
        ToolCall(
            "c1",
            "patch",
            {
                "path": str(target),
                "edits": [
                    {"old": "one", "new": "1"},
                    {"old": "one", "new": "2"},
                ],
            },
        )
    )
    assert result.is_error
    assert target.read_bytes() == before


async def test_patch_edits_count_not_allowed(registry, tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("a b a\n", encoding="utf-8")

    result = await registry.execute(
        ToolCall(
            "c1",
            "patch",
            {"path": str(target), "edits": [{"old": "a", "new": "x", "count": 0}]},
        )
    )
    assert result.is_error
    assert "count" in result.content


# ---------------------------------------------------------------------------
# C3 参数宽进严出
# ---------------------------------------------------------------------------


def test_coerce_tool_arguments_unwraps_stringified_json():
    from crew.core.types import coerce_tool_arguments

    inner = {"path": "/tmp/a.txt", "old": "x", "new": "y"}
    assert coerce_tool_arguments(__import__("json").dumps(inner)) == inner


def test_coerce_tool_arguments_passthrough_and_failure():
    import json

    from crew.core.types import coerce_tool_arguments

    assert coerce_tool_arguments("plain text") == "plain text"
    assert coerce_tool_arguments(42) == 42
    # 二次解析失败：原样保留，交给下游 schema 严校验拒绝
    broken = '{"path": "/tmp/a.txt", '
    assert coerce_tool_arguments(broken) == broken
    # 字符串化的数组也展开（由 schema 层判定是否为合法对象）
    assert coerce_tool_arguments(json.dumps([1, 2])) == [1, 2]


def test_openai_parse_tool_arguments_unwraps_stringified_json():
    import json

    from crew.providers.openai_provider import _parse_tool_arguments

    inner = {"path": "/tmp/a.txt", "old": "x", "new": "y"}
    parsed = _parse_tool_arguments(json.dumps(json.dumps(inner)), "patch")
    assert parsed == inner


async def test_registry_schema_rejects_non_dict_args_without_crashing(registry, tmp_path):
    """严出：结构非法的参数转成 error toolResult 回模型自纠，不崩循环。"""
    target = tmp_path / "demo.txt"
    target.write_text("hi\n", encoding="utf-8")

    result = await registry.execute(ToolCall("c1", "patch", ["not", "a", "dict"]))
    assert result.is_error
    assert "工具参数必须是对象" in result.content

    result2 = await registry.execute(ToolCall("c2", "patch", {"path": str(target), "old": "hi"}))
    assert result2.is_error
    assert "new" in result2.content


# ---------------------------------------------------------------------------
# C6 read-before-edit 观察策略
# ---------------------------------------------------------------------------


class _PolicyConfig:
    def __init__(self, enabled: bool):
        self.raw_config = {"tools": {"file": {"require_read_before_edit": enabled}}}


@pytest.fixture
def policy_on(monkeypatch):
    monkeypatch.setattr(
        "crew.state.config.load_config", lambda: _PolicyConfig(True)
    )


async def test_patch_without_read_rejected_with_remedy(registry, tmp_path, policy_on):
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")
    token = current_session_id.set("sess-c6")
    try:
        result = await registry.execute(
            ToolCall("c1", "patch", {"path": str(target), "old": "hello", "new": "hi"})
        )
        assert result.is_error
        assert "has not been read" in result.content
        assert "file_read" in result.content  # 补救动作
        assert target.read_text(encoding="utf-8") == "hello\n"
    finally:
        current_session_id.reset(token)


async def test_patch_after_read_allowed(registry, tmp_path, policy_on):
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")
    token = current_session_id.set("sess-c6")
    try:
        read = await registry.execute(ToolCall("c0", "file_read", {"path": str(target)}))
        assert not read.is_error

        result = await registry.execute(
            ToolCall("c1", "patch", {"path": str(target), "old": "hello", "new": "hi"})
        )
        assert not result.is_error
        assert target.read_text(encoding="utf-8") == "hi\n"
    finally:
        current_session_id.reset(token)


async def test_patch_after_external_change_is_stale(registry, tmp_path, policy_on):
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")
    token = current_session_id.set("sess-c6")
    try:
        read = await registry.execute(ToolCall("c0", "file_read", {"path": str(target)}))
        assert not read.is_error

        target.write_text("hello world\n", encoding="utf-8")  # 会话外修改

        result = await registry.execute(
            ToolCall("c1", "patch", {"path": str(target), "old": "hello world", "new": "hi"})
        )
        assert result.is_error
        assert "重新读取" in result.content
        assert target.read_text(encoding="utf-8") == "hello world\n"
    finally:
        current_session_id.reset(token)


async def test_file_write_new_file_allowed_without_read(registry, tmp_path, policy_on):
    token = current_session_id.set("sess-c6")
    try:
        target = tmp_path / "new.txt"
        result = await registry.execute(
            ToolCall("c1", "file_write", {"path": str(target), "content": "created"})
        )
        assert not result.is_error
        assert target.read_text(encoding="utf-8") == "created"
    finally:
        current_session_id.reset(token)


async def test_file_write_existing_without_read_rejected(registry, tmp_path, policy_on):
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")
    token = current_session_id.set("sess-c6")
    try:
        result = await registry.execute(
            ToolCall("c1", "file_write", {"path": str(target), "content": "overwrite"})
        )
        assert result.is_error
        assert "has not been read" in result.content
        assert target.read_text(encoding="utf-8") == "hello\n"
    finally:
        current_session_id.reset(token)


async def test_observation_isolated_per_session(registry, tmp_path, policy_on):
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")
    token_a = current_session_id.set("sess-a")
    try:
        read = await registry.execute(ToolCall("c0", "file_read", {"path": str(target)}))
        assert not read.is_error
    finally:
        current_session_id.reset(token_a)

    token_b = current_session_id.set("sess-b")
    try:
        result = await registry.execute(
            ToolCall("c1", "patch", {"path": str(target), "old": "hello", "new": "hi"})
        )
        assert result.is_error
        assert "has not been read" in result.content
    finally:
        current_session_id.reset(token_b)


async def test_same_size_and_mtime_external_edit_is_rejected_with_legacy_policy_off(registry, tmp_path, monkeypatch):
    monkeypatch.setattr("crew.state.config.load_config", lambda: _PolicyConfig(False))
    target = tmp_path / "same-metadata.txt"
    target.write_bytes(b"old\nkeep\n")
    session = current_session_id.set("edit-metadata")
    try:
        read = await registry.execute(ToolCall("read", "file_read", {"path": str(target)}))
        assert json.loads(read.content)["file_version"]
        previous = target.stat()
        target.write_bytes(b"old\npeer\n")
        os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert target.stat().st_mtime_ns == previous.st_mtime_ns
        patch = await registry.execute(ToolCall("patch", "patch", {"path": str(target), "old": "old", "new": "new"}))
        assert patch.is_error and patch.code == "stale_file"
        assert json.loads(patch.content)["next_action"] == "file_read"
        assert target.read_bytes() == b"old\npeer\n"
    finally:
        current_session_id.reset(session)


async def test_file_version_is_scoped_to_account_and_commit_results_match_reads(registry, tmp_path):
    target = tmp_path / "中文-🌙.txt"
    path = {"path": str(target)}
    session = current_session_id.set("shared-name")
    owner = current_owner_account_id.set("A:owner-a")
    try:
        created = await registry.execute(ToolCall("create", "file_write", {**path, "content": "before\n"}))
        assert not created.is_error, created.content
        updated = await registry.execute(ToolCall("update-created", "file_write", {
            **path, "content": "before\nknown\n", "expected_version": json.loads(created.content)["file_version"],
        }))
        assert not updated.is_error, updated.content
        read = await registry.execute(ToolCall("read", "file_read", path))
        assert not read.is_error, read.content
        observed = json.loads(read.content)["file_version"]
        assert observed == json.loads(updated.content)["file_version"]

        different_owner = current_owner_account_id.set("A:owner-b")
        try:
            denied = await registry.execute(ToolCall("deny", "patch", {
                **path, "old": "before", "new": "wrong", "expected_version": observed,
            }))
            assert denied.is_error and denied.code == "file_not_read"
        finally:
            current_owner_account_id.reset(different_owner)
        patched = await registry.execute(ToolCall("patch", "patch", {
            **path, "old": "before", "new": "after", "expected_version": observed,
        }))
        assert not patched.is_error, patched.content
        read_again = await registry.execute(ToolCall("read-again", "file_read", path))
        assert json.loads(patched.content)["file_version"] == json.loads(read_again.content)["file_version"] != observed
        assert target.read_bytes() == b"after\nknown\n"
    finally:
        current_owner_account_id.reset(owner)
        current_session_id.reset(session)


async def test_complete_coverage_can_be_assembled_from_untruncated_read_pages(registry, tmp_path):
    target = tmp_path / "paged.txt"
    target.write_bytes(b"first\nsecond\nthird\n")
    path = {"path": str(target)}
    session = current_session_id.set("read-pages")
    try:
        tail = await registry.execute(ToolCall("tail", "file_read", {**path, "offset": -1}))
        tail_data = json.loads(tail.content)
        assert not tail_data["read_complete"] and tail_data["truncated"]
        version = {"expected_version": tail_data["file_version"]}
        wrong_range = await registry.execute(ToolCall("outside", "patch", {
            **path, **version, "old": "first", "new": "wrong",
        }))
        assert wrong_range.is_error and wrong_range.code == "incomplete_read"
        denied = await registry.execute(ToolCall("denied", "file_write", {**path, **version, "content": "wrong"}))
        assert denied.is_error and denied.code == "incomplete_read"
        head = await registry.execute(ToolCall("head", "file_read", {**path, "offset": 1, "limit": 2}))
        head_data = json.loads(head.content)
        assert not head_data["read_complete"] and head_data["next_offset"] == 3
        assert head_data["file_version"] == version["expected_version"]
        result = await registry.execute(ToolCall("overwrite", "file_write", {**path, **version, "content": "new\n"}))
        assert not result.is_error, result.content
        assert target.read_bytes() == b"new\n"
    finally:
        current_session_id.reset(session)


async def test_read_payload_keeps_version_after_budgeting_json_escaped_lines(registry, tmp_path):
    target = tmp_path / "escaped.txt"
    target.write_text(('"\\' * 100 + "\n") * 420, encoding="utf-8")
    session = current_session_id.set("escaped-content")
    try:
        result = await registry.execute(ToolCall("read", "file_read", {"path": str(target)}))
        data = json.loads(result.content)
        assert not data["read_complete"] and data["file_version"]
        assert data["next_offset"] == data["shown_lines"] + 1
        assert data["total_lines"] == 420 and data["shown_lines"] > 100
    finally:
        current_session_id.reset(session)


async def test_new_file_never_replaces_a_late_external_creation(tmp_path, monkeypatch):
    target = tmp_path / "late.txt"
    compare = file_utils._file_version_matches
    checked = 0

    def insert_after_final_check(expected, descriptor=None):
        nonlocal checked
        matched = compare(expected, descriptor)
        checked += 1
        if checked == 2:
            target.write_bytes(b"external")
        return matched

    monkeypatch.setattr(file_utils, "_file_version_matches", insert_after_final_check)
    with pytest.raises(StaleFileError):
        await file_mutation.write_text(target, "agent", guard=True)
    assert checked == 2 and target.read_bytes() == b"external"
    assert [file.name for file in tmp_path.iterdir()] == [target.name]


async def test_cancelled_mutations_keep_the_path_queue_until_the_worker_exits(tmp_path, monkeypatch):
    target = tmp_path / "cancel.txt"
    worker_started, release = threading.Event(), threading.Event()
    executed = []
    write = file_mutation._write_text_sync

    def blocked_write(path, content, *args):
        executed.append(content)
        if content == "first":
            worker_started.set()
            assert release.wait(5)
        return write(path, content, *args)

    monkeypatch.setattr(file_mutation, "_write_text_sync", blocked_write)
    first = asyncio.create_task(file_mutation.write_text(target, "first"))
    try:
        assert await asyncio.to_thread(worker_started.wait, 5)
        second = asyncio.create_task(file_mutation.write_text(target, "cancelled"))
        await asyncio.sleep(0)
        second.cancel()
        first.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task
        third = asyncio.create_task(file_mutation.write_text(target, "third"))
        await asyncio.sleep(0)
        assert executed == ["first"] and not third.done()
    finally:
        release.set()
    await third
    assert executed == ["first", "third"] and target.read_bytes() == b"third"


async def test_readonly_file_returns_structured_error_and_stays_readable(registry, tmp_path):
    directory = tmp_path / "observed"
    directory.mkdir()
    target = directory / "readonly.txt"
    target.write_bytes(b"old\n")
    target.chmod(stat.S_IREAD)
    try:
        result = await registry.execute(ToolCall("patch", "patch", {"path": str(target), "old": "old", "new": "new"}))
        assert result.is_error and result.code == "file_not_writable"
        read = await registry.execute(ToolCall("reread", "file_read", {"path": str(target)}))
        assert "old" in json.loads(read.content)["content"]
        assert list(directory.iterdir()) == [target]
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize(("change", "code"), [
    ({"old": "absent", "new": "replace"}, "patch_no_match"),
    ({"old": "abc", "new": "replace"}, "patch_ambiguous"),
    ({"edits": [{"old": "abcdef", "new": "one"}, {"old": "def", "new": "two"}]}, "patch_overlap"),
])
async def test_patch_reports_structured_matching_failures_without_writing(registry, tmp_path, change, code):
    target = tmp_path / "matching.txt"
    target.write_bytes(b"abcdef\nabc\n")
    result = await registry.execute(ToolCall("patch", "patch", {"path": str(target), **change}))
    payload = json.loads(result.content)
    assert result.is_error and result.code == payload["code"] == code
    assert payload["next_action"] == "file_read"
    if code == "patch_ambiguous":
        assert payload["details"]["lines"] == [1, 2]
    assert target.read_bytes() == b"abcdef\nabc\n"


async def test_transcoded_display_does_not_allow_utf8_edit_of_utf16_source(registry, tmp_path):
    target = tmp_path / "wide.txt"
    original = b"\xff\xfe" + "first\r\n".encode("utf-16-le")
    target.write_bytes(original)
    path = {"path": str(target)}
    session = current_session_id.set("read-wide")
    try:
        read = await registry.execute(ToolCall("read", "file_read", path))
        payload = json.loads(read.content)
        assert payload["read_complete"] and not payload["editable"]
        patch = await registry.execute(ToolCall("patch", "patch", {
            **path, "old": "first", "new": "changed", "expected_version": payload["file_version"],
        }))
        assert patch.is_error and patch.code == "file_encoding_not_editable"
        assert target.read_bytes() == original
    finally:
        current_session_id.reset(session)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing flags")
async def test_windows_file_open_without_delete_sharing_preserves_file_and_reports_busy(registry, tmp_path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    target = tmp_path / "busy.txt"
    target.write_bytes(b"old\r\n")
    handle = create(str(target), 0x80000000, 0x0001 | 0x0002, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        call = {"path": str(target), "old": "old", "new": "new"}
        error = await registry.execute(ToolCall("busy", "patch", call))
        assert error.is_error and error.code == "file_not_writable"
        assert target.read_bytes() == b"old\r\n" and list(tmp_path.iterdir()) == [target]
    finally:
        kernel.CloseHandle(handle)
    retry = await registry.execute(ToolCall("retry", "patch", call))
    assert not retry.is_error and target.read_bytes() == b"new\r\n"
