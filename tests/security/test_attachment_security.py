import os
import stat
from pathlib import Path

import pytest


def test_websocket_attachment_normalization_rejects_paths_outside_owner_uploads(
    tmp_path, monkeypatch
):
    from crew.gateway.context import normalize_agent_attachments

    uploads = tmp_path / "owner" / "uploads"
    uploads.mkdir(parents=True)
    allowed = uploads / "allowed.txt"
    allowed.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr("crew.gateway.context._get_upload_dir", lambda _owner: uploads)

    result = normalize_agent_attachments(
        [
            {"name": "allowed.txt", "path": str(allowed), "type": "file"},
            {"name": "outside.txt", "path": str(outside), "type": "file"},
        ],
        "owner-a",
    )

    assert [Path(item["path"]) for item in result] == [allowed.resolve()]


def test_websocket_attachment_normalization_rejects_leaf_links(
    tmp_path,
    monkeypatch,
):
    from crew.gateway.context import normalize_agent_attachments

    uploads = tmp_path / "owner" / "uploads"
    uploads.mkdir(parents=True)
    target = uploads / "target.txt"
    target.write_text("safe", encoding="utf-8")
    linked = uploads / "linked.txt"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr("crew.gateway.context._get_upload_dir", lambda _owner: uploads)

    result = normalize_agent_attachments(
        [{"name": "linked.txt", "path": str(linked), "type": "file"}],
        "owner-a",
    )

    assert result == []


def test_save_upload_uses_bounded_atomic_private_write(tmp_path, monkeypatch):
    import crew.gateway.context as context

    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(context, "_MAX_UPLOAD_BYTES", 4)

    with pytest.raises(ValueError, match="大小"):
        context.save_upload("large.txt", b"12345", owner_account_id="owner-a")

    original_write_bytes = Path.write_bytes

    def forbidden_path_write(self, data):
        raise AssertionError(f"non-atomic Path.write_bytes used for {self}")

    monkeypatch.setattr(Path, "write_bytes", forbidden_path_write)
    saved = context.save_upload("safe.txt", b"safe", owner_account_id="owner-a")
    monkeypatch.setattr(Path, "write_bytes", original_write_bytes)

    target = Path(saved["path"])
    assert target.read_bytes() == b"safe"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_attachment_normalization_bounds_file_count_and_inline_content(
    tmp_path,
    monkeypatch,
):
    import crew.gateway.context as context

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    first = uploads / "first.txt"
    second = uploads / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    monkeypatch.setattr(context, "_get_upload_dir", lambda _owner: uploads)
    monkeypatch.setattr(context, "_MAX_ATTACHMENTS", 1)
    monkeypatch.setattr(context, "_MAX_INLINE_ATTACHMENT_CHARS", 4)

    normalized = context.normalize_agent_attachments(
        [
            {"name": "inline.txt", "content": "12345"},
            {"name": "first.txt", "path": str(first)},
            {"name": "second.txt", "path": str(second)},
        ],
        "owner-a",
    )

    assert [item["name"] for item in normalized] == ["first.txt"]


def test_save_upload_enforces_owner_disk_budget(tmp_path, monkeypatch):
    import crew.gateway.context as context

    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(context, "_MAX_UPLOAD_BYTES", 4)
    monkeypatch.setattr(context, "_MAX_UPLOAD_STORE_BYTES", 4)

    first = context.save_upload("first.txt", b"123", owner_account_id="owner-a")
    with pytest.raises(ValueError, match="owner 上传存储"):
        context.save_upload("second.txt", b"12", owner_account_id="owner-a")

    upload_dir = Path(first["path"]).parent
    files = [
        item
        for item in upload_dir.iterdir()
        if item.name not in {".quota.lock", ".dedup.json"}
    ]
    assert len(files) == 1


def test_attachment_normalization_enforces_request_total_bytes(
    tmp_path,
    monkeypatch,
):
    import crew.gateway.context as context

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    first = uploads / "first.txt"
    second = uploads / "second.txt"
    first.write_text("123", encoding="utf-8")
    second.write_text("456", encoding="utf-8")
    monkeypatch.setattr(context, "_get_upload_dir", lambda _owner: uploads)
    monkeypatch.setattr(context, "_MAX_REQUEST_ATTACHMENT_BYTES", 5)

    with pytest.raises(ValueError, match="本轮附件总量"):
        context.normalize_agent_attachments(
            [
                {"name": "first.txt", "path": str(first)},
                {"name": "second.txt", "path": str(second)},
            ],
            "owner-a",
        )


def test_ws_attachment_schema_enforces_declared_total_bytes(monkeypatch):
    from crew.gateway.ws import (
        WebSocketProtocolError,
        _validate_attachments,
    )
    import crew.gateway.ws as ws

    monkeypatch.setattr(ws, "WS_MAX_REQUEST_ATTACHMENT_BYTES", 5)
    with pytest.raises(WebSocketProtocolError):
        _validate_attachments([
            {"name": "first.bin", "path": "first", "size": 3},
            {"name": "second.bin", "path": "second", "size": 3},
        ])
