"""skills 独立脚本质检（H10）测试。

覆盖：
- xlsx recalc.py：subprocess 超时、非零退出码、stderr 尾部截断、成功路径
- md-to-pdf md2pdf.py：命令探测超时、转换失败 stderr 回传、超时分类
- pdf pdf.py：顶层未捕获异常 → 结构化 JSON 错误 + 非零退出

全部 mock / 本地 fixture，不真跑 LibreOffice / pandoc / 网络。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / "crew" / "skills"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def recalc_module(monkeypatch):
    scripts_dir = SKILLS_DIR / "xlsx" / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    return _load_module("crew_skill_recalc_h10", scripts_dir / "recalc.py")


@pytest.fixture
def md2pdf_module():
    return _load_module(
        "crew_skill_md2pdf_h10", SKILLS_DIR / "md-to-pdf" / "scripts" / "md2pdf.py"
    )


@pytest.fixture
def pdf_tool_module():
    scripts_dir = SKILLS_DIR / "pdf" / "scripts"
    return _load_module("crew_skill_pdf_h10", scripts_dir / "pdf.py")


@pytest.fixture
def xlsx_file(tmp_path: Path) -> Path:
    from openpyxl import Workbook

    path = tmp_path / "book.xlsx"
    wb = Workbook()
    wb.active["A1"] = "=1+1"
    wb.save(path)
    wb.close()
    return path


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["soffice"], returncode=returncode, stdout=stdout, stderr=stderr)


# ── xlsx recalc.py ─────────────────────────────────────────────────────────


def test_recalc_missing_file_returns_error(recalc_module):
    result = recalc_module.recalc("/definitely/missing/file.xlsx")
    assert "error" in result


def test_recalc_soffice_timeout(recalc_module, xlsx_file, monkeypatch):
    monkeypatch.setattr(recalc_module, "setup_libreoffice_macro", lambda: True)

    def fake_run(*args, **kwargs):
        assert kwargs.get("timeout") == 30
        raise subprocess.TimeoutExpired(cmd=["soffice"], timeout=30)

    monkeypatch.setattr(recalc_module.subprocess, "run", fake_run)

    result = recalc_module.recalc(str(xlsx_file), timeout=30)
    assert "超时" in result["error"]


def test_recalc_soffice_nonzero_exit_truncates_stderr(recalc_module, xlsx_file, monkeypatch):
    monkeypatch.setattr(recalc_module, "setup_libreoffice_macro", lambda: True)
    long_stderr = "x" * 5000 + "fatal at end"
    monkeypatch.setattr(
        recalc_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed(returncode=1, stderr=long_stderr),
    )

    result = recalc_module.recalc(str(xlsx_file), timeout=30)

    assert "fatal at end" in result["error"]
    assert len(result["error"]) < len(long_stderr)
    assert "省略" in result["error"]


def test_recalc_soffice_missing_binary(recalc_module, xlsx_file, monkeypatch):
    monkeypatch.setattr(recalc_module, "setup_libreoffice_macro", lambda: True)

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("soffice")

    monkeypatch.setattr(recalc_module.subprocess, "run", fake_run)

    result = recalc_module.recalc(str(xlsx_file), timeout=30)
    assert "无法启动 LibreOffice" in result["error"]


def test_recalc_success_counts_formulas(recalc_module, xlsx_file, monkeypatch):
    monkeypatch.setattr(recalc_module, "setup_libreoffice_macro", lambda: True)
    monkeypatch.setattr(
        recalc_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed(returncode=0),
    )

    result = recalc_module.recalc(str(xlsx_file), timeout=30)

    assert result["status"] == "success"
    assert result["total_formulas"] == 1
    assert result["total_errors"] == 0


# ── md-to-pdf md2pdf.py ────────────────────────────────────────────────────


def test_md2pdf_check_command_missing(md2pdf_module):
    assert md2pdf_module.check_command("definitely-not-a-command-h10") is False


def test_md2pdf_pandoc_failure_returns_stderr_tail(md2pdf_module, tmp_path, monkeypatch):
    monkeypatch.setattr(md2pdf_module, "check_command", lambda cmd: True)
    long_stderr = "y" * 5000 + "boom at end"

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=long_stderr)

    monkeypatch.setattr(md2pdf_module.subprocess, "run", fake_run)

    ok, message = md2pdf_module.method_pandoc_xelatex(tmp_path / "a.md", tmp_path / "a.pdf")

    assert ok is False
    assert "boom at end" in message
    assert len(message) < len(long_stderr)
    assert "省略" in message


def test_md2pdf_pandoc_timeout_classified(md2pdf_module, tmp_path, monkeypatch):
    monkeypatch.setattr(md2pdf_module, "check_command", lambda cmd: True)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    monkeypatch.setattr(md2pdf_module.subprocess, "run", fake_run)

    ok, message = md2pdf_module.method_pandoc_xelatex(tmp_path / "a.md", tmp_path / "a.pdf")

    assert ok is False
    assert "超时" in message


def test_md2pdf_convert_missing_file_exits_nonzero(md2pdf_module, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        md2pdf_module.convert_md_to_pdf(tmp_path / "missing.md", tmp_path / "out.pdf")

    assert exc_info.value.code == 1
    assert "not found" in capsys.readouterr().out.lower()


def test_md2pdf_tail_truncates(md2pdf_module):
    text = "head" + "z" * 5000
    tailed = md2pdf_module._tail(text)
    assert tailed.startswith("…")
    assert len(tailed) < len(text)


# ── pdf pdf.py ─────────────────────────────────────────────────────────────


def test_pdf_main_guard_catches_unexpected_error(pdf_tool_module, monkeypatch, capsys):
    fake_cmd_form = types.ModuleType("cmd_form")

    def form_info(pdf):
        raise RuntimeError("kaboom")

    fake_cmd_form.form_info = form_info
    monkeypatch.setitem(sys.modules, "cmd_form", fake_cmd_form)
    monkeypatch.setattr(sys, "argv", ["pdf.py", "form", "info", "any.pdf"])

    with pytest.raises(SystemExit) as exc_info:
        pdf_tool_module.main()

    assert exc_info.value.code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["status"] == "error"
    assert err["error"] == "UnexpectedError"
    assert "kaboom" in err["message"]


def test_pdf_missing_file_structured_error(pdf_tool_module, monkeypatch, capsys):
    fake_cmd_form = types.ModuleType("cmd_form")

    def fake_error(error, message, hint=None, code=1):
        print(
            json.dumps({"status": "error", "error": error, "message": message}),
            file=sys.stderr,
        )
        raise SystemExit(code)

    fake_cmd_form.form_info = lambda pdf: pdf_tool_module.Output.error(
        "FileNotFound", f"File not found: {pdf}", code=2
    )
    monkeypatch.setitem(sys.modules, "cmd_form", fake_cmd_form)
    monkeypatch.setattr(pdf_tool_module.Output, "error", fake_error)
    monkeypatch.setattr(sys, "argv", ["pdf.py", "form", "info", "missing.pdf"])

    with pytest.raises(SystemExit) as exc_info:
        pdf_tool_module.main()

    assert exc_info.value.code == 2
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "FileNotFound"
    assert "missing.pdf" in err["message"]
