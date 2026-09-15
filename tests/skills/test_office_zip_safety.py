"""xlsx office 工具链 ZIP/XML 安全语义测试（H10 漂移修复）。

覆盖：
- xlsx helpers/zip_utils.safe_extract_all：路径穿越、绝对路径拒绝；正常解包
- xlsx RedliningValidator：恶意 original docx（zip slip）被拒绝且不落地；
  含 ENTITY 的 document.xml 被 defusedxml 阻断，校验返回 False 不抛异常；
  正常无修订文档通过
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / "crew" / "skills"
XLSX_OFFICE_DIR = SKILLS_DIR / "xlsx" / "scripts" / "office"

MINIMAL_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>hello</w:t></w:r></w:p></w:body>
</w:document>
"""

XXE_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE document [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body>
</w:document>
"""


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def zip_utils_module():
    return _load_module(
        "crew_skill_xlsx_zip_utils_h10", XLSX_OFFICE_DIR / "helpers" / "zip_utils.py"
    )


@pytest.fixture
def redlining_module(monkeypatch):
    monkeypatch.syspath_prepend(str(XLSX_OFFICE_DIR))
    return _load_module(
        "crew_skill_xlsx_redlining_h10",
        XLSX_OFFICE_DIR / "validators" / "redlining.py",
    )


def _make_docx(path: Path, document_xml: str, extra: dict[str, bytes] | None = None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)


# ── safe_extract_all ───────────────────────────────────────────────────────


def test_safe_extract_all_rejects_path_traversal(zip_utils_module, tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "pwned")
        zf.writestr("word/document.xml", "ok")

    out = tmp_path / "out"
    out.mkdir()
    with zipfile.ZipFile(evil) as zf, pytest.raises(ValueError):
        zip_utils_module.safe_extract_all(zf, out)

    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_all_rejects_absolute_path(zip_utils_module, tmp_path):
    evil = tmp_path / "evil-abs.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("/tmp/escape-abs.txt", "pwned")

    out = tmp_path / "out"
    out.mkdir()
    with zipfile.ZipFile(evil) as zf, pytest.raises(ValueError):
        zip_utils_module.safe_extract_all(zf, out)


def test_safe_extract_all_extracts_normal_archive(zip_utils_module, tmp_path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("word/document.xml", "<doc/>")
        zf.writestr("[Content_Types].xml", "<types/>")

    out = tmp_path / "out"
    out.mkdir()
    with zipfile.ZipFile(good) as zf:
        zip_utils_module.safe_extract_all(zf, out)

    assert (out / "word" / "document.xml").read_text() == "<doc/>"
    assert (out / "[Content_Types].xml").exists()


# ── RedliningValidator 安全语义 ──────────────────────────────────────────────


def _write_unpacked(tmp_path: Path, document_xml: str) -> Path:
    unpacked = tmp_path / "unpacked"
    (unpacked / "word").mkdir(parents=True)
    (unpacked / "word" / "document.xml").write_text(document_xml, encoding="utf-8")
    return unpacked


def test_redlining_rejects_zip_slip_original(redlining_module, tmp_path, capsys):
    unpacked = _write_unpacked(tmp_path, MINIMAL_DOCUMENT_XML)
    original = tmp_path / "original.docx"
    # 修改稿带有本作者的修订，会触发 original docx 解包对比路径
    tracked = MINIMAL_DOCUMENT_XML.replace(
        "</w:body>",
        '<w:p><w:ins w:author="Claude" w:id="1"><w:r><w:t>x</w:t></w:r></w:ins></w:p></w:body>',
    )
    (unpacked / "word" / "document.xml").write_text(tracked, encoding="utf-8")
    _make_docx(original, MINIMAL_DOCUMENT_XML, {"../escape.txt": "pwned"})

    validator = redlining_module.RedliningValidator(unpacked, original)
    assert validator.validate() is False
    assert not (tmp_path / "escape.txt").exists()
    assert "Error unpacking original docx" in capsys.readouterr().out


def test_redlining_blocks_xxe_document(redlining_module, tmp_path, capsys):
    unpacked = _write_unpacked(tmp_path, XXE_DOCUMENT_XML)
    original = tmp_path / "original.docx"
    _make_docx(original, MINIMAL_DOCUMENT_XML)

    validator = redlining_module.RedliningValidator(unpacked, original)
    assert validator.validate() is False
    assert "Error parsing XML files" in capsys.readouterr().out


def test_redlining_passes_clean_document(redlining_module, tmp_path):
    unpacked = _write_unpacked(tmp_path, MINIMAL_DOCUMENT_XML)
    original = tmp_path / "original.docx"
    _make_docx(original, MINIMAL_DOCUMENT_XML)

    validator = redlining_module.RedliningValidator(unpacked, original, verbose=True)
    assert validator.validate() is True
