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
