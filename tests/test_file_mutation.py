"""文件变更服务（crew.agent.file_mutation）：串行锁、版本 CAS、匹配与事务语义。"""

import asyncio
import json

import pytest

from crew.agent.file_mutation import (
    EditOp,
    EditMatchError,
    StaleFileError,
    clear_file_observations,
    file_mutation,
    plan_edits,
)
from crew.core.runctx import current_session_id
from crew.core.types import ToolCall
from crew.tools.file_utils import snapshot_file
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
