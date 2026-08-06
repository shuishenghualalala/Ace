"""Pack a directory into a DOCX, PPTX, or XLSX file.

Validates with auto-repair, condenses XML formatting, and creates the Office file.

Usage:
    python pack.py <input_directory> <output_file> [--original <file>] [--validate true|false]

Examples:
    python pack.py unpacked/ output.docx --original input.docx
    python pack.py unpacked/ output.pptx --validate false
"""

import argparse
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

import defusedxml.minidom

from validators import DOCXSchemaValidator, PPTXSchemaValidator, RedliningValidator

# File extensions that have no business being inside an Office Open XML package.
BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".sh", ".vbs", ".js", ".wsf",
    ".jar", ".class", ".py", ".pyc", ".rb", ".pl", ".ps1", ".app",
    ".msi", ".dmg", ".pkg", ".deb", ".rpm",
}


def _is_safe_to_pack(file_path: Path, base_dir: Path) -> tuple[bool, str | None]:
    """Validate that a file is safe to include in the output package.

    Returns (is_safe, reason_or_none).
    """
    name = file_path.name
    if any(part == ".." for part in file_path.relative_to(base_dir).parts):
        return False, f"Path contains parent reference: {name}"

    # Reject hidden/system files at the root level of the package.
    try:
        rel = file_path.resolve().relative_to(base_dir.resolve())
    except (ValueError, OSError):
        return False, f"File escapes input directory: {name}"

    if rel.parts and rel.parts[0].startswith("."):
        return False, f"Hidden file/package component: {name}"

    # Reject symlinks that point outside the input directory.
    if file_path.is_symlink():
        real_path = file_path.resolve()
        try:
            real_path.relative_to(base_dir.resolve())
        except (ValueError, OSError):
            return False, f"Symlink escapes input directory: {name}"

    if file_path.suffix.lower() in BLOCKED_EXTENSIONS:
        return False, f"Blocked file extension: {name}"

    # Reject Unix special files (devices, fifos, sockets).
    mode = file_path.lstat().st_mode
    if not stat.S_ISREG(mode):
        return False, f"Not a regular file: {name}"

    return True, None


def pack(
    input_directory: str,
    output_file: str,
    original_file: str | None = None,
    validate: bool = True,
    infer_author_func=None,
) -> tuple[None, str]:
    input_dir = Path(input_directory)
    output_path = Path(output_file)
    suffix = output_path.suffix.lower()

    if not input_dir.is_dir():
        return None, f"Error: {input_dir} is not a directory"

    if suffix not in {".docx", ".pptx", ".xlsx"}:
        return None, f"Error: {output_file} must be a .docx, .pptx, or .xlsx file"

    if validate and original_file:
        original_path = Path(original_file)
        if original_path.exists():
            success, output = _run_validation(
                input_dir, original_path, suffix, infer_author_func
            )
            if output:
                print(output)
            if not success:
                return None, f"Error: Validation failed for {input_dir}"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_content_dir = Path(temp_dir) / "content"
        shutil.copytree(input_dir, temp_content_dir)

        for pattern in ["*.xml", "*.rels"]:
            for xml_file in temp_content_dir.rglob(pattern):
                _condense_xml(xml_file)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in temp_content_dir.rglob("*"):
                if not f.is_file():
                    continue
                safe, reason = _is_safe_to_pack(f, temp_content_dir)
                if not safe:
                    return None, f"Error: {reason}"
                zf.write(f, f.relative_to(temp_content_dir))

    return None, f"Successfully packed {input_dir} to {output_file}"


def _run_validation(
    unpacked_dir: Path,
    original_file: Path,
    suffix: str,
    infer_author_func=None,
) -> tuple[bool, str | None]:
    output_lines = []
    validators = []

    if suffix == ".docx":
        author = "Claude"
        if infer_author_func:
            try:
                author = infer_author_func(unpacked_dir, original_file)
            except ValueError as e:
                print(f"Warning: {e} Using default author 'Claude'.", file=sys.stderr)

        validators = [
            DOCXSchemaValidator(unpacked_dir, original_file),
            RedliningValidator(unpacked_dir, original_file, author=author),
        ]
    elif suffix == ".pptx":
        validators = [PPTXSchemaValidator(unpacked_dir, original_file)]

    if not validators:
        return True, None

    total_repairs = sum(v.repair() for v in validators)
    if total_repairs:
        output_lines.append(f"Auto-repaired {total_repairs} issue(s)")

    success = all(v.validate() for v in validators)

    if success:
        output_lines.append("All validations PASSED!")

    return success, "\n".join(output_lines) if output_lines else None


def _condense_xml(xml_file: Path) -> None:
    try:
        with open(xml_file, encoding="utf-8") as f:
            dom = defusedxml.minidom.parse(f, forbid_dtd=True)

        for element in dom.getElementsByTagName("*"):
            if element.tagName.endswith(":t"):
                continue

            for child in list(element.childNodes):
                if (
                    child.nodeType == child.TEXT_NODE
                    and child.nodeValue
                    and child.nodeValue.strip() == ""
                ) or child.nodeType == child.COMMENT_NODE:
                    element.removeChild(child)

        xml_file.write_bytes(dom.toxml(encoding="UTF-8"))
    except Exception as e:
        print(f"ERROR: Failed to parse {xml_file.name}: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pack a directory into a DOCX, PPTX, or XLSX file"
    )
    parser.add_argument("input_directory", help="Unpacked Office document directory")
    parser.add_argument("output_file", help="Output Office file (.docx/.pptx/.xlsx)")
    parser.add_argument(
        "--original",
        help="Original file for validation comparison",
    )
    parser.add_argument(
        "--validate",
        type=lambda x: x.lower() == "true",
        default=True,
        metavar="true|false",
        help="Run validation with auto-repair (default: true)",
    )
    args = parser.parse_args()

    _, message = pack(
        args.input_directory,
        args.output_file,
        original_file=args.original,
        validate=args.validate,
    )
    print(message)

    if "Error" in message:
        sys.exit(1)
