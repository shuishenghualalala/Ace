from crew.security.actions import normalize_file_action
from crew.tools.security_guard import _file_change_preview


def test_file_write_approval_binds_and_previews_exact_proposed_content(tmp_path):
    first_digest, preview = _file_change_preview(
        {"content": "first line\nsecond line", "append": False},
        "write",
    )
    second_digest, _ = _file_change_preview(
        {"content": "changed", "append": False},
        "write",
    )

    first = normalize_file_action(
        tmp_path / "outside.txt",
        "write",
        content_digest=first_digest,
    )
    second = normalize_file_action(
        tmp_path / "outside.txt",
        "write",
        content_digest=second_digest,
    )

    assert first.digest != second.digest
    assert "待写入内容" in preview
    assert "second line" in preview


def test_patch_preview_is_computed_from_proposed_replacement_only():
    digest, preview = _file_change_preview(
        {"old": "unsafe = True\n", "new": "unsafe = False\n", "count": 1},
        "patch",
    )

    assert len(digest) == 64
    assert "-unsafe = True" in preview
    assert "+unsafe = False" in preview
