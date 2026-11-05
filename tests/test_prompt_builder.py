"""Prompt 文件读取缓存与线程化测试。"""

import os
import threading
from pathlib import Path

import pytest

from crew.agent import prompt_builder
from crew.agent.prompt_builder import _clear_file_cache, _load_profile, build_prompt_parts


@pytest.fixture(autouse=True)
def _clean_cache():
    _clear_file_cache()
    yield
    _clear_file_cache()


def _patch_counting_read_text(monkeypatch) -> tuple[dict[str, int], list[int]]:
    """统计 Path.read_text 真实读盘次数，并记录读盘发生的线程。"""
    real = Path.read_text
    counter = {"n": 0}
    idents: list[int] = []

    def counting(self, *args, **kwargs):
        counter["n"] += 1
        idents.append(threading.get_ident())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return counter, idents


def test_profile_read_is_cached_by_mtime(tmp_path, monkeypatch):
    """mtime 未变不重复读盘；mtime 变化后重新读盘。"""
    counter, _ = _patch_counting_read_text(monkeypatch)
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


def test_crew_md_read_is_cached_by_mtime(tmp_path, monkeypatch):
    """CREW.md 发现路径同样命中 mtime 缓存。"""
    counter, _ = _patch_counting_read_text(monkeypatch)
    (tmp_path / "CREW.md").write_text("项目规则", encoding="utf-8")

    first = prompt_builder.build_context_files_prompt(cwd=str(tmp_path))
    second = prompt_builder.build_context_files_prompt(cwd=str(tmp_path))
    assert first == second == "## CREW.md\n\n项目规则"
    assert counter["n"] == 1


async def test_build_prompt_parts_reads_off_main_thread(tmp_path, monkeypatch):
    """build_prompt_parts 的读盘在工作线程执行，不在调用方（事件循环）线程。"""
    counter, idents = _patch_counting_read_text(monkeypatch)
    main_ident = threading.get_ident()
    profile = tmp_path / "profile.md"
    profile.write_text("画像", encoding="utf-8")

    parts = await build_prompt_parts(profile_path=str(profile), cwd=None)
    assert "画像" in parts["system_static"]
    assert counter["n"] >= 1
    assert len(idents) == counter["n"]
    assert all(t != main_ident for t in idents)
