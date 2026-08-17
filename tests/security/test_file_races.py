"""Best-effort TOCTOU and hard-link defenses for structured file writes."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import subprocess
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.core.errors import ToolError
from crew.core.runctx import current_owner_account_id, current_task_runtime_id
from crew.tools import builtin, file_tools, file_utils, web_tools
from crew.tools.file_utils import (
    FileConflictError,
    atomic_replace_bytes,
    snapshot_file,
)
from crew.tools.security_guard import AuthorizedFileTarget


def test_atomic_replace_rejects_concurrent_content_change(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    version = snapshot_file(target)
    target.write_text("other writer", encoding="utf-8")

    with pytest.raises(FileConflictError, match="修改或替换"):
        atomic_replace_bytes(target, b"agent write", version)

    assert target.read_text(encoding="utf-8") == "other writer"


def test_atomic_replace_digest_detects_same_size_same_mtime_tampering(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"aaaa")
    version = snapshot_file(target)
    target.write_bytes(b"bbbb")
    os.utime(target, ns=(target.stat().st_atime_ns, version.mtime_ns))

    with pytest.raises(FileConflictError, match="修改或替换"):
        atomic_replace_bytes(target, b"agent write", version)

    assert version.digest == hashlib.sha256(b"aaaa").hexdigest()
    assert target.read_bytes() == b"bbbb"


def test_read_verified_bytes_can_bind_expected_content_digest(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"approved")
    expected = hashlib.sha256(b"approved").hexdigest()

    assert file_utils.read_verified_bytes(
        target,
        expected_digest=expected,
    ) == b"approved"
    with pytest.raises(FileConflictError, match="摘要"):
        file_utils.read_verified_bytes(
            target,
            expected_digest=hashlib.sha256(b"different").hexdigest(),
        )


def test_atomic_replace_rejects_write_over_byte_budget(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    monkeypatch.setattr(file_utils, "_DEFAULT_MAX_WRITE_BYTES", 3)

    with pytest.raises(ValueError, match="写入上限"):
        atomic_replace_bytes(target, b"1234", snapshot_file(target))

    assert not target.exists()


def test_atomic_replace_rejects_insufficient_disk_budget(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    monkeypatch.setattr(file_utils, "_MIN_FREE_SPACE_AFTER_WRITE", 2)
    monkeypatch.setattr(
        file_utils.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10, used=7, free=3),
    )

    with pytest.raises(OSError, match="磁盘"):
        atomic_replace_bytes(target, b"12", snapshot_file(target))

    assert not target.exists()


def test_atomic_replace_rejects_concurrency_budget_exhaustion(tmp_path, monkeypatch):
    class ExhaustedSlots:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

        def release(self):
            raise AssertionError("unacquired slot was released")

    monkeypatch.setattr(file_utils, "_WRITE_SLOTS", ExhaustedSlots())
    target = tmp_path / "target.txt"

    with pytest.raises(FileConflictError, match="并发写入"):
        atomic_replace_bytes(target, b"blocked", snapshot_file(target))

    assert not target.exists()


def test_atomic_replace_write_budget_isolated_by_owner_and_task(tmp_path, monkeypatch):
    budget = file_utils._WriteBudgetRegistry(max_bytes=4)
    monkeypatch.setattr(file_utils, "_WRITE_BUDGET", budget)
    monkeypatch.setattr(file_utils, "_atomic_replace_posix", lambda *_args: None)
    monkeypatch.setattr(file_utils, "_atomic_replace_windows", lambda *_args: None)
    held = budget.reserve(("owner-a", "task-a"), 4)
    assert held is not None
    try:
        owner_token = current_owner_account_id.set("owner-a")
        task_token = current_task_runtime_id.set("task-a")
        try:
            with pytest.raises(FileConflictError, match="聚合|在途"):
                atomic_replace_bytes(tmp_path / "same.txt", b"1", snapshot_file(tmp_path / "same.txt"))
        finally:
            current_task_runtime_id.reset(task_token)
            current_owner_account_id.reset(owner_token)

        owner_token = current_owner_account_id.set("owner-a")
        task_token = current_task_runtime_id.set("task-b")
        try:
            atomic_replace_bytes(tmp_path / "other-task.txt", b"1234", snapshot_file(tmp_path / "other-task.txt"))
        finally:
            current_task_runtime_id.reset(task_token)
            current_owner_account_id.reset(owner_token)

        atomic_replace_bytes(tmp_path / "unbound.txt", b"1234", snapshot_file(tmp_path / "unbound.txt"))
    finally:
        held.release()


def test_atomic_replace_releases_write_budget_after_exception_and_duplicate_release_is_safe(
    tmp_path,
    monkeypatch,
):
    budget = file_utils._WriteBudgetRegistry(max_bytes=4)
    monkeypatch.setattr(file_utils, "_WRITE_BUDGET", budget)
    duplicate = budget.reserve(("owner-duplicate", "task"), 4)
    assert duplicate is not None
    duplicate.release()
    duplicate.release()

    def fail_write(*_args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(file_utils, "_atomic_replace_posix", fail_write)
    monkeypatch.setattr(file_utils, "_atomic_replace_windows", fail_write)
    owner_token = current_owner_account_id.set("owner-a")
    task_token = current_task_runtime_id.set("task-a")
    try:
        with pytest.raises(OSError, match="simulated"):
            atomic_replace_bytes(tmp_path / "target.txt", b"1234", snapshot_file(tmp_path / "target.txt"))
        budget_lease = budget.reserve(("owner-a", "task-a"), 4)
        assert budget_lease is not None
        budget_lease.release()
    finally:
        current_task_runtime_id.reset(task_token)
        current_owner_account_id.reset(owner_token)


def test_atomic_replace_rejects_owner_task_aggregate_write_budget_over_limit(tmp_path, monkeypatch):
    budget = file_utils._WriteBudgetRegistry(max_bytes=3)
    monkeypatch.setattr(file_utils, "_WRITE_BUDGET", budget)
    held = budget.reserve(("owner-a", "task-a"), 3)
    assert held is not None
    owner_token = current_owner_account_id.set("owner-a")
    task_token = current_task_runtime_id.set("task-a")
    try:
        with pytest.raises(FileConflictError, match="聚合|在途"):
            atomic_replace_bytes(tmp_path / "target.txt", b"1", snapshot_file(tmp_path / "target.txt"))
    finally:
        current_task_runtime_id.reset(task_token)
        current_owner_account_id.reset(owner_token)
        held.release()


def test_read_verified_bytes_budget_isolated_by_owner_and_task(tmp_path, monkeypatch):
    budget = file_utils._ReadBudgetRegistry(max_bytes=4)
    monkeypatch.setattr(file_utils, "_READ_BUDGET", budget)
    target = tmp_path / "target.txt"
    target.write_bytes(b"1234")
    held = budget.reserve(("owner-a", "task-a"), 4)
    assert held is not None
    try:
        owner_token = current_owner_account_id.set("owner-a")
        task_token = current_task_runtime_id.set("task-a")
        try:
            with pytest.raises(FileConflictError, match="读取|在途"):
                file_utils.read_verified_bytes(target, max_bytes=1)
        finally:
            current_task_runtime_id.reset(task_token)
            current_owner_account_id.reset(owner_token)

        owner_token = current_owner_account_id.set("owner-a")
        task_token = current_task_runtime_id.set("task-b")
        try:
            assert file_utils.read_verified_bytes(target, max_bytes=4) == b"1234"
        finally:
            current_task_runtime_id.reset(task_token)
            current_owner_account_id.reset(owner_token)
    finally:
        held.release()


def test_read_verified_bytes_budget_releases_after_failure(tmp_path, monkeypatch):
    budget = file_utils._ReadBudgetRegistry(max_bytes=4)
    monkeypatch.setattr(file_utils, "_READ_BUDGET", budget)
    monkeypatch.setattr(
        file_utils,
        "_read_verified_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated read failure")),
    )
    owner_token = current_owner_account_id.set("owner-a")
    task_token = current_task_runtime_id.set("task-a")
    try:
        with pytest.raises(OSError, match="simulated"):
            file_utils.read_verified_bytes(tmp_path / "target.txt", max_bytes=4)
        lease = budget.reserve(("owner-a", "task-a"), 4)
        assert lease is not None
        lease.release()
    finally:
        current_task_runtime_id.reset(task_token)
        current_owner_account_id.reset(owner_token)


def test_snapshot_file_shares_owner_task_read_budget_with_verified_reads(
    tmp_path,
    monkeypatch,
):
    """snapshot_file and read_verified_bytes charge the same cross-entry budget."""
    budget = file_utils._ReadBudgetRegistry(max_bytes=4)
    monkeypatch.setattr(file_utils, "_READ_BUDGET", budget)
    target = tmp_path / "target.txt"
    target.write_bytes(b"1234")
    held = budget.reserve(("owner-a", "task-a"), 4)
    assert held is not None
    owner_token = current_owner_account_id.set("owner-a")
    task_token = current_task_runtime_id.set("task-a")
    try:
        with pytest.raises(FileConflictError, match="读取|在途"):
            file_utils.snapshot_file(target, max_bytes=1)
        with pytest.raises(FileConflictError, match="读取|在途"):
            file_utils.read_verified_bytes(target, max_bytes=1)
    finally:
        current_task_runtime_id.reset(task_token)
        current_owner_account_id.reset(owner_token)
        held.release()

    owner_token = current_owner_account_id.set("owner-a")
    task_token = current_task_runtime_id.set("task-b")
    try:
        assert file_utils.snapshot_file(target, max_bytes=4).data == b"1234"
    finally:
        current_task_runtime_id.reset(task_token)
        current_owner_account_id.reset(owner_token)


def test_snapshot_file_releases_read_budget_after_failure(tmp_path, monkeypatch):
    budget = file_utils._ReadBudgetRegistry(max_bytes=4)
    monkeypatch.setattr(file_utils, "_READ_BUDGET", budget)
    monkeypatch.setattr(
        file_utils,
        "_read_verified_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated read failure")),
    )
    owner_token = current_owner_account_id.set("owner-a")
    task_token = current_task_runtime_id.set("task-a")
    try:
        with pytest.raises(OSError, match="simulated"):
            file_utils.snapshot_file(tmp_path / "target.txt", max_bytes=4)
        lease = budget.reserve(("owner-a", "task-a"), 4)
        assert lease is not None
        lease.release()
    finally:
        current_task_runtime_id.reset(task_token)
        current_owner_account_id.reset(owner_token)


def test_atomic_replace_securely_creates_missing_parent_chain(tmp_path):
    target = tmp_path / "private" / "nested" / "target.txt"
    version = snapshot_file(target)

    atomic_replace_bytes(target, b"created", version)

    assert target.read_bytes() == b"created"
    if os.name != "nt":
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_missing_file_authorization_identity_cannot_move_to_another_path(tmp_path):
    authorized = tmp_path / "authorized.txt"
    other = tmp_path / "other.txt"
    identity = file_utils.capture_file_identity(authorized)

    with pytest.raises(FileConflictError, match="授权|身份"):
        snapshot_file(other, expected_identity=identity)


def test_missing_file_identity_rejects_parent_directory_replacement(tmp_path):
    parent = tmp_path / "authorized"
    replacement = tmp_path / "replacement"
    parent.mkdir()
    replacement.mkdir()
    target = parent / "new.txt"
    identity = file_utils.capture_file_identity(target)

    parent.rename(tmp_path / "authorized-original")
    (tmp_path / "authorized").mkdir()

    with pytest.raises(FileConflictError, match="父目录|身份|授权"):
        snapshot_file(target, expected_identity=identity)


@pytest.mark.skipif(os.name == "nt", reason="Unicode normalization identity is host-specific")
def test_missing_file_identity_does_not_alias_distinct_posix_unicode_names(tmp_path):
    composed = tmp_path / unicodedata.normalize("NFC", "café.txt")
    decomposed = tmp_path / unicodedata.normalize("NFD", "café.txt")
    if composed == decomposed:
        pytest.skip("filesystem normalizes Unicode names")
    identity = file_utils.capture_file_identity(composed)

    with pytest.raises(FileConflictError, match="路径|身份"):
        snapshot_file(decomposed, expected_identity=identity)


def test_atomic_replace_rejects_target_replaced_by_symlink(tmp_path):
    target = tmp_path / "target.txt"
    other = tmp_path / "other.txt"
    target.write_text("before", encoding="utf-8")
    other.write_text("secret", encoding="utf-8")
    version = snapshot_file(target)
    target.unlink()
    try:
        target.symlink_to(other)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(FileConflictError, match="修改或替换"):
        atomic_replace_bytes(target, b"agent write", version)

    assert other.read_text(encoding="utf-8") == "secret"


def test_atomic_replace_does_not_create_directories_through_linked_parent(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    linked = workspace / "linked"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("junction creation unavailable")
    else:
        linked.symlink_to(outside, target_is_directory=True)

    target = linked / "created" / "target.txt"
    expected = file_utils.FileVersion(
        path=Path(os.path.abspath(target)),
        exists=False,
    )
    try:
        with pytest.raises((FileConflictError, OSError)):
            atomic_replace_bytes(target, b"blocked", expected)
    finally:
        if os.name == "nt" and linked.exists():
            os.rmdir(linked)

    assert not (outside / "created").exists()


def test_snapshot_rejects_symlink_swapped_in_after_authorization(tmp_path):
    authorized = tmp_path / "authorized.txt"
    outside = tmp_path / "outside.txt"
    authorized.write_text("authorized", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")
    authorized.unlink()
    try:
        authorized.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(FileConflictError, match="符号链接|身份"):
        snapshot_file(authorized)

    assert outside.read_text(encoding="utf-8") == "secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write", "patch", "vision"])
async def test_file_operation_rejects_leaf_swap_after_authorization(
    tmp_path,
    monkeypatch,
    operation,
):
    target = tmp_path / "authorized.txt"
    original = tmp_path / "authorized-original.txt"
    outside = tmp_path / "outside.txt"
    target.write_text("authorized", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")

    async def authorize(*_args, **_kwargs):
        authorization = AuthorizedFileTarget(
            path=target,
            identity=file_utils.capture_file_identity(target),
        )
        if operation in {"patch", "vision"}:
            swap_after_component_check(target)
        return authorization

    swapped = False

    def swap_after_component_check(_path):
        nonlocal swapped
        target.replace(original)
        target.write_text("attacker replacement", encoding="utf-8")
        swapped = True

    monkeypatch.setattr(builtin, "authorize_file_tool", authorize)
    monkeypatch.setattr(file_tools, "authorize_file_tool", authorize)
    monkeypatch.setattr(web_tools, "authorize_file_tool", authorize)
    monkeypatch.setattr(builtin, "_assert_no_symlink_component", swap_after_component_check)

    if operation == "read":
        call = builtin.handle_file_read(
            {"path": str(target)},
            workspace_store=object(),
            security_service=object(),
        )
    elif operation == "write":
        call = builtin.handle_file_write(
            {"path": str(target), "content": "agent write"},
            workspace_store=object(),
            security_service=object(),
        )
    elif operation == "patch":
        call = file_tools.handle_patch(
            {"path": str(target), "old": "authorized", "new": "agent patch"},
            workspace_store=object(),
            security_service=object(),
        )
    else:
        call = web_tools.handle_vision_analyze(
            {"path": str(target)},
            workspace_store=object(),
            security_service=object(),
        )
    with pytest.raises(ToolError, match="身份|修改或替换|写入失败|读取失败"):
        await call

    assert swapped
    assert original.read_text(encoding="utf-8") == "authorized"
    assert target.read_text(encoding="utf-8") == "attacker replacement"
    assert outside.read_text(encoding="utf-8") == "secret"


@pytest.mark.asyncio
async def test_file_tool_io_failures_do_not_echo_host_exception(tmp_path, monkeypatch):
    target = tmp_path / "authorized.txt"
    target.write_text("authorized", encoding="utf-8")
    identity = file_utils.capture_file_identity(target)

    async def authorize(*_args, **_kwargs):
        return AuthorizedFileTarget(path=target, identity=identity)

    monkeypatch.setattr(builtin, "authorize_file_tool", authorize)
    monkeypatch.setattr(builtin, "_assert_no_symlink_component", lambda _path: None)
    secret = r"C:\private\key.pem ACCESS_TOKEN=must-not-leak"

    monkeypatch.setattr(
        builtin,
        "read_verified_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)),
    )
    with pytest.raises(ToolError) as read_error:
        await builtin.handle_file_read(
            {"path": str(target)},
            workspace_store=object(),
            security_service=object(),
        )
    assert str(read_error.value) == "读取失败"

    monkeypatch.setattr(
        builtin,
        "atomic_replace_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    with pytest.raises(ToolError) as write_error:
        await builtin.handle_file_write(
            {"path": str(target), "content": "agent write"},
            workspace_store=object(),
            security_service=object(),
        )
    assert str(write_error.value) == "写入失败"


@pytest.mark.asyncio
async def test_patch_identity_failure_does_not_echo_host_exception(tmp_path, monkeypatch):
    target = tmp_path / "authorized.txt"
    target.write_text("authorized", encoding="utf-8")
    identity = file_utils.capture_file_identity(target)

    async def authorize(*_args, **_kwargs):
        return AuthorizedFileTarget(path=target, identity=identity)

    monkeypatch.setattr(file_tools, "authorize_file_tool", authorize)
    secret = r"C:\private\key.pem ACCESS_TOKEN=must-not-leak"
    monkeypatch.setattr(
        file_tools,
        "snapshot_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)),
    )

    with pytest.raises(ToolError) as patch_error:
        await file_tools.handle_patch(
            {"path": str(target), "old": "authorized", "new": "patched"},
            workspace_store=object(),
            security_service=object(),
        )
    assert str(patch_error.value) == "补丁目标身份校验失败"


def test_verified_read_enforces_byte_limit_on_the_checked_handle(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"1234")

    with pytest.raises(ValueError, match="读取上限"):
        file_utils.read_verified_bytes(target, max_bytes=3)

    assert file_utils.read_verified_bytes(target, max_bytes=4) == b"1234"


def test_verified_read_rejects_oversized_stat_before_open(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"1234")
    before = target.stat()
    opened = False

    def forbidden_open(_flags):
        nonlocal opened
        opened = True
        raise AssertionError("oversized file was opened before the size check")

    with pytest.raises(ValueError, match="读取上限"):
        file_utils._read_verified_open(before, forbidden_open, max_bytes=3)

    assert not opened


@pytest.mark.skipif(os.name != "nt", reason="case-alias identity is Windows-specific")
def test_verified_read_rejects_case_alias_for_bound_identity(tmp_path):
    target = tmp_path / "CaseBound.txt"
    target.write_bytes(b"bound")
    identity = file_utils.capture_file_identity(target)
    alias = target.with_name("casebound.txt")
    assert str(alias) != str(target)

    with pytest.raises(FileConflictError, match="路径|身份"):
        file_utils.read_verified_bytes(alias, expected_identity=identity)


def test_snapshot_file_has_default_read_budget(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"1234")
    monkeypatch.setattr(file_utils, "_DEFAULT_MAX_FILE_BYTES", 3)

    with pytest.raises(ValueError, match="读取上限"):
        snapshot_file(target)


@pytest.mark.skipif(os.name == "nt", reason="POSIX special-file modes")
@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_verified_open_rejects_special_file_before_open(tmp_path, kind):
    target = tmp_path / kind
    if kind == "fifo":
        os.mkfifo(target)

        def cleanup():
            return None
    else:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(target))
        cleanup = listener.close

    opened = False

    def forbidden_open(_flags):
        nonlocal opened
        opened = True
        raise AssertionError("special file was opened before its type was rejected")

    try:
        with pytest.raises(FileConflictError, match="普通文件"):
            file_utils._open_verified(target.lstat(), forbidden_open)
    finally:
        cleanup()

    assert not opened


def test_verified_open_rejects_windows_reparse_attribute_before_open():
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    before = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_file_attributes=reparse_flag,
    )
    opened = False

    def forbidden_open(_flags):
        nonlocal opened
        opened = True
        raise AssertionError("reparse leaf was opened")

    with pytest.raises(FileConflictError, match="reparse"):
        file_utils._open_verified(before, forbidden_open)

    assert not opened


def test_verified_open_rejects_windows_compressed_attribute_before_open():
    compressed_flag = getattr(stat, "FILE_ATTRIBUTE_COMPRESSED", 0x800)
    before = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_size=1,
        st_file_attributes=compressed_flag,
    )
    opened = False

    def forbidden_open(_flags):
        nonlocal opened
        opened = True
        raise AssertionError("compressed leaf was opened")

    with pytest.raises(FileConflictError, match="稀疏|压缩"):
        file_utils._open_verified(before, forbidden_open)

    assert not opened


@pytest.mark.skipif(os.name == "nt", reason="st_blocks sparse detection is POSIX")
def test_verified_read_rejects_sparse_regular_file(tmp_path):
    target = tmp_path / "sparse.bin"
    with target.open("wb") as handle:
        handle.seek(8 * 1024 * 1024)
        handle.write(b"x")
    info = target.stat()
    if not hasattr(info, "st_blocks") or info.st_blocks * 512 >= info.st_size:
        pytest.skip("filesystem did not create a sparse file")

    with pytest.raises(FileConflictError, match="稀疏"):
        file_utils.read_verified_bytes(target, max_bytes=16 * 1024 * 1024)


def test_structured_write_rejects_existing_hard_link(tmp_path):
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_text("shared", encoding="utf-8")
    try:
        os.link(target, alias)
    except OSError:
        pytest.skip("hard links unavailable")

    with pytest.raises(FileConflictError, match="硬链接"):
        snapshot_file(target)


def test_atomic_replace_writes_new_file_in_same_directory(tmp_path):
    target = tmp_path / "target.txt"
    version = snapshot_file(target)
    atomic_replace_bytes(target, b"created", version)
    assert target.read_bytes() == b"created"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")
def test_atomic_replace_keeps_temporary_private_until_publish(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o644)
    version = snapshot_file(target)
    real_replace = os.replace
    temporary_mode = None

    def inspect_then_replace(source, destination, *args, **kwargs):
        nonlocal temporary_mode
        parent_fd = kwargs.get("src_dir_fd")
        if parent_fd is None:
            temporary_mode = stat.S_IMODE(os.stat(source).st_mode)
        else:
            temporary_mode = stat.S_IMODE(
                os.stat(source, dir_fd=parent_fd, follow_symlinks=False).st_mode
            )
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(file_utils.os, "replace", inspect_then_replace)
    atomic_replace_bytes(target, b"after", version)

    assert temporary_mode == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_atomic_replace_cannot_be_redirected_by_parent_directory_swap(tmp_path, monkeypatch):
    parent = tmp_path / "authorized"
    moved = tmp_path / "authorized-original"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    target = parent / "target.txt"
    outside_target = outside / "target.txt"
    target.write_text("before", encoding="utf-8")
    outside_target.write_text("secret", encoding="utf-8")
    version = snapshot_file(target)
    real_replace = os.replace
    swapped = False

    def swap_parent_then_replace(source, destination, *args, **kwargs):
        nonlocal swapped
        try:
            parent.rename(moved)
            try:
                parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            except OSError:
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(parent), str(outside)],
                    capture_output=True,
                    check=False,
                )
                swapped = created.returncode == 0
                if not swapped:
                    moved.rename(parent)
            if swapped:
                os.link(moved / Path(source).name, outside / Path(source).name)
        except OSError:
            # Windows secure implementation pins each parent without FILE_SHARE_DELETE.
            pass
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(file_utils.os, "replace", swap_parent_then_replace)
    try:
        atomic_replace_bytes(target, b"agent write", version)
    finally:
        if swapped and parent.exists():
            os.rmdir(parent) if os.name == "nt" else parent.unlink()

    assert outside_target.read_text(encoding="utf-8") == "secret"
    actual_target = moved / "target.txt" if swapped else target
    assert actual_target.read_bytes() == b"agent write"
