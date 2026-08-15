"""Portable contract tests for the Win32 recording append boundary."""

from __future__ import annotations

import errno
import ntpath
import os
from dataclasses import dataclass, replace
from typing import Any

import pytest

from crew.browser.win32_secure_recording import (
    CtypesWin32RecordingAPI,
    Win32FileIdentity,
    _canonical_windows_path,
    secure_append_recording_line,
    secure_ensure_recording_marker,
)


OWNER = r"C:\CrewData\owners\account-a"
DIRECTORY = OWNER + r"\recordings\0123456789abcdef\deadbeef"
TRACE = DIRECTORY + r"\trace.jsonl"
MARKER = DIRECTORY + r"\INCOMPLETE"


@dataclass
class _Object:
    identity: Win32FileIdentity
    data: bytearray
    private: bool = True
    final_path: str = ""


@dataclass(frozen=True)
class _Handle:
    object_id: int
    kind: str


class FakeWin32RecordingAPI:
    """In-memory handle facade; no host-OS filesystem semantics leak in."""

    def __init__(self) -> None:
        self.objects: dict[int, _Object] = {}
        self.paths: dict[str, int] = {}
        self.next_object_id = 1
        self.closed: list[_Handle] = []
        self.write_sizes: list[int] = []
        self.partial_writes: list[int] = []
        self.flushed = False
        self.reparse_path: str | None = None
        self.escape_path: str | None = None
        self.unsafe_path: str | None = None
        self.on_open_audit: Any = None
        self.on_write: Any = None
        self.on_flush: Any = None

    @staticmethod
    def _key(path: str) -> str:
        return ntpath.normcase(ntpath.normpath(path))

    def _new(
        self,
        path: str,
        *,
        directory: bool,
        data: bytes = b"",
    ) -> int:
        object_id = self.next_object_id
        self.next_object_id += 1
        key = self._key(path)
        self.objects[object_id] = _Object(
            Win32FileIdentity(
                volume_serial=7,
                file_index=object_id,
                size=len(data),
                link_count=1,
                is_directory=directory,
                is_reparse_point=self.reparse_path == key,
                is_disk_file=True,
                is_local_fixed_disk=True,
            ),
            bytearray(data),
            private=self.unsafe_path != key,
            final_path=self.escape_path if self.escape_path == key else key,
        )
        self.paths[key] = object_id
        return object_id

    def create_private_directory(self, path: str) -> None:
        key = self._key(path)
        if key not in self.paths:
            self._new(key, directory=True)

    def open_directory(self, path: str) -> _Handle:
        return _Handle(self.paths[self._key(path)], "directory")

    def open_append_file(self, path: str) -> tuple[_Handle, bool]:
        key = self._key(path)
        created = key not in self.paths
        if created:
            self._new(key, directory=False)
        return _Handle(self.paths[key], "append"), created

    def open_file_audit(self, path: str) -> _Handle:
        handle = _Handle(self.paths[self._key(path)], "audit")
        if self.on_open_audit is not None:
            self.on_open_audit(self)
        return handle

    def final_path(self, handle: _Handle) -> str:
        return self.objects[handle.object_id].final_path

    def identity(self, handle: _Handle) -> Win32FileIdentity:
        return self.objects[handle.object_id].identity

    def security_is_current_user_only(self, handle: _Handle) -> bool:
        return self.objects[handle.object_id].private

    def write(self, handle: _Handle, payload: memoryview) -> int:
        if self.on_write is not None:
            callback, self.on_write = self.on_write, None
            callback(self)
        requested = len(payload)
        count = min(requested, self.partial_writes.pop(0)) if self.partial_writes else requested
        obj = self.objects[handle.object_id]
        obj.data.extend(bytes(payload[:count]))
        obj.identity = replace(obj.identity, size=len(obj.data))
        self.write_sizes.append(count)
        return count

    def flush(self, handle: _Handle) -> None:
        self.flushed = True
        if self.on_flush is not None:
            callback, self.on_flush = self.on_flush, None
            callback(self)

    def close(self, handle: _Handle) -> None:
        self.closed.append(handle)

    def trace_data(self) -> bytes:
        object_id = self.paths[self._key(TRACE)]
        return bytes(self.objects[object_id].data)

    def seed_trace(self, data: bytes) -> None:
        self._new(TRACE, directory=False, data=data)


def test_win32_append_uses_validated_handles_and_flushes() -> None:
    api = FakeWin32RecordingAPI()
    payload = b'{"action":"click"}\n'

    secure_append_recording_line(OWNER, DIRECTORY, payload, 1024, api=api)

    assert api.trace_data() == payload
    assert api.write_sizes == [len(payload)]
    assert api.flushed is True
    assert {handle.kind for handle in api.closed} == {
        "directory",
        "append",
        "audit",
    }
    assert len(api.closed) == 6
    assert len(set(api.closed)) == len(api.closed)


def test_win32_incomplete_marker_uses_same_private_handle_boundary() -> None:
    api = FakeWin32RecordingAPI()

    secure_ensure_recording_marker(OWNER, DIRECTORY, api=api)
    secure_ensure_recording_marker(OWNER, DIRECTORY, api=api)

    marker = api.objects[api.paths[api._key(MARKER)]]
    assert bytes(marker.data) == b"recording-incomplete\n"
    assert api.flushed is True


def test_ctypes_facade_requests_only_append_authority_for_writer() -> None:
    """Lock the CreateFileW access/share/disposition contract on every OS."""
    api = object.__new__(CtypesWin32RecordingAPI)
    calls: list[tuple[Any, ...]] = []

    def capture(*args, **kwargs):
        calls.append((*args, kwargs))
        return 99, True

    api._create_file = capture  # type: ignore[method-assign]
    assert api.open_append_file(TRACE) == (99, True)
    assert api.open_directory(DIRECTORY) == 99
    assert api.open_file_audit(TRACE) == 99

    assert calls == [
        # writer: FILE_APPEND_DATA, FILE_SHARE_READ, OPEN_ALWAYS,
        # FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT.
        (TRACE, 0x00000004, 0x00000001, 4, 0x00200080, {"private_on_create": True}),
        # directory validator: no FILE_SHARE_DELETE.
        (DIRECTORY, 0x00020080, 0x00000003, 3, 0x02200000, {}),
        # file security validator: read-control only, no data write.
        (TRACE, 0x00020080, 0x00000003, 3, 0x00200000, {}),
    ]


def test_win32_final_path_prefix_unc_and_case_normalization() -> None:
    assert _canonical_windows_path(r"\\?\C:\CrewData\OWNER\trace.jsonl") == (
        _canonical_windows_path(r"c:\crewdata\owner\TRACE.JSONL")
    )
    assert _canonical_windows_path(
        r"\\?\UNC\server\share\owner\trace.jsonl"
    ) == _canonical_windows_path(r"\\SERVER\SHARE\OWNER\TRACE.JSONL")


def test_win32_8dot3_alias_is_rejected_fail_closed() -> None:
    """Never resolve an 8.3 alias with a fresh raceable pathname lookup."""
    api = FakeWin32RecordingAPI()
    short_owner = r"C:\CREWDA~1\owners\account-a"
    short_directory = short_owner + r"\recordings\0123456789abcdef\deadbeef"

    original_new = api._new

    def long_name_new(path: str, *, directory: bool, data: bytes = b"") -> int:
        object_id = original_new(path, directory=directory, data=data)
        api.objects[object_id].final_path = api.objects[object_id].final_path.replace(
            r"crewda~1", "crewdata"
        )
        return object_id

    api._new = long_name_new  # type: ignore[method-assign]
    with pytest.raises(OSError, match="稳定目录"):
        secure_append_recording_line(
            short_owner,
            short_directory,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )


@pytest.mark.parametrize("suffix", ["recordings", "deadbeef"])
def test_win32_append_rejects_directory_junction_or_symlink(suffix: str) -> None:
    api = FakeWin32RecordingAPI()
    api.reparse_path = (
        ntpath.normcase(ntpath.join(OWNER, "recordings"))
        if suffix == "recordings"
        else ntpath.normcase(DIRECTORY)
    )

    with pytest.raises(OSError, match="稳定目录"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert ntpath.normcase(TRACE) not in api.paths


def test_win32_append_rejects_trace_reparse_point() -> None:
    api = FakeWin32RecordingAPI()
    api.reparse_path = ntpath.normcase(TRACE)

    with pytest.raises(OSError, match="稳定普通文件"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert api.trace_data() == b""
    assert len(set(api.closed)) == len(api.closed)


def test_win32_append_rejects_parent_replacement_before_write() -> None:
    api = FakeWin32RecordingAPI()

    def replace_parent(current: FakeWin32RecordingAPI) -> None:
        parent_id = current.paths[current._key(DIRECTORY)]
        current.objects[parent_id].final_path = r"C:\attacker\deadbeef"

    api.on_open_audit = replace_parent

    with pytest.raises(OSError, match="稳定目录"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert api.trace_data() == b""


def test_win32_append_pins_and_revalidates_owner_root() -> None:
    api = FakeWin32RecordingAPI()

    def replace_owner(current: FakeWin32RecordingAPI) -> None:
        owner_id = current.paths[current._key(OWNER)]
        current.objects[owner_id].final_path = r"C:\attacker\account-a"

    api.on_open_audit = replace_owner

    with pytest.raises(OSError, match="owner 录制根"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert api.trace_data() == b""
    assert len(set(api.closed)) == len(api.closed)


def test_win32_append_rejects_owner_root_reparse_point() -> None:
    api = FakeWin32RecordingAPI()
    api.create_private_directory(OWNER)
    owner = api.objects[api.paths[api._key(OWNER)]]
    owner.identity = replace(owner.identity, is_reparse_point=True)

    with pytest.raises(OSError, match="owner 录制根"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert ntpath.normcase(TRACE) not in api.paths


def test_win32_append_rejects_non_local_filesystem() -> None:
    api = FakeWin32RecordingAPI()
    api.create_private_directory(OWNER)
    owner = api.objects[api.paths[api._key(OWNER)]]
    owner.identity = replace(owner.identity, is_local_fixed_disk=False)

    with pytest.raises(OSError, match="本地固定磁盘"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert ntpath.normcase(TRACE) not in api.paths


def test_win32_append_rejects_final_trace_path_escape_before_write() -> None:
    api = FakeWin32RecordingAPI()
    api.escape_path = ntpath.normcase(TRACE)

    # The fake resolves this configured object outside the private root.
    original_new = api._new

    def escaping_new(path: str, *, directory: bool, data: bytes = b"") -> int:
        object_id = original_new(path, directory=directory, data=data)
        if api._key(path) == api._key(TRACE):
            api.objects[object_id].final_path = r"C:\outside\trace.jsonl"
        return object_id

    api._new = escaping_new  # type: ignore[method-assign]

    with pytest.raises(OSError, match="稳定普通文件"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert api.trace_data() == b""


def test_win32_append_rejects_unsafe_owner_or_dacl() -> None:
    api = FakeWin32RecordingAPI()
    api.unsafe_path = ntpath.normcase(DIRECTORY)

    with pytest.raises(OSError, match="当前用户私有"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert ntpath.normcase(TRACE) not in api.paths


def test_win32_append_rejects_unsafe_trace_owner_or_dacl() -> None:
    api = FakeWin32RecordingAPI()
    api.unsafe_path = ntpath.normcase(TRACE)

    with pytest.raises(OSError, match="当前用户私有"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert api.trace_data() == b""


def test_win32_append_rejects_trace_with_multiple_hard_links() -> None:
    api = FakeWin32RecordingAPI()
    api.seed_trace(b"sentinel\n")
    trace_object = api.objects[api.paths[api._key(TRACE)]]
    trace_object.identity = replace(trace_object.identity, link_count=2)

    with pytest.raises(OSError, match="稳定普通文件"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-land"}\n',
            1024,
            api=api,
        )

    assert api.trace_data() == b"sentinel\n"


def test_win32_append_checks_size_before_writing() -> None:
    api = FakeWin32RecordingAPI()
    api.seed_trace(b"x" * 1000)

    with pytest.raises(OSError, match="超过大小上限"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"too":"large"}\n',
            1008,
            api=api,
        )

    assert api.trace_data() == b"x" * 1000
    assert api.write_sizes == []
    assert api.flushed is False


def test_win32_append_retries_partial_write_and_checks_final_size() -> None:
    api = FakeWin32RecordingAPI()
    payload = b'{"partial":"write"}\n'
    api.partial_writes = [3, 2]

    secure_append_recording_line(OWNER, DIRECTORY, payload, 1024, api=api)

    assert api.trace_data() == payload
    assert api.write_sizes == [3, 2, len(payload) - 5]
    assert api.flushed is True


def test_win32_append_checks_size_again_after_flush() -> None:
    api = FakeWin32RecordingAPI()
    payload = b'{"bounded":true}\n'

    def force_oversize(current: FakeWin32RecordingAPI) -> None:
        trace_id = current.paths[current._key(TRACE)]
        trace = current.objects[trace_id]
        trace.data.extend(b"x" * 2048)
        trace.identity = replace(trace.identity, size=len(trace.data))

    api.on_flush = force_oversize
    with pytest.raises(OSError, match="写后大小"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            payload,
            1024,
            api=api,
        )
    assert api.flushed is True


def test_win32_append_rejects_noncanonical_target_name() -> None:
    api = FakeWin32RecordingAPI()
    with pytest.raises(OSError, match="结构无效|规范形式|相对路径"):
        secure_append_recording_line(
            OWNER,
            DIRECTORY + r"\..\cafebabe",
            b"{}\n",
            1024,
            api=api,
        )
    assert api.paths == {}


def test_win32_append_rejects_zero_progress_write() -> None:
    api = FakeWin32RecordingAPI()
    api.partial_writes = [0]
    with pytest.raises(OSError) as exc_info:
        secure_append_recording_line(
            OWNER,
            DIRECTORY,
            b'{"must":"not-complete"}\n',
            1024,
            api=api,
        )
    assert exc_info.value.errno == errno.EIO
    assert api.trace_data() == b""


@pytest.mark.skipif(os.name != "nt", reason="requires the real Win32 kernel APIs")
def test_win32_real_kernel_append_smoke(tmp_path) -> None:
    """Windows-runner smoke test; adversarial ACL/junction cases stay mocked.

    Creating real junctions and foreign-user ACLs is privilege/environment
    dependent.  The portable facade tests above cover those branches
    deterministically; this test proves ctypes signatures, OPEN_ALWAYS append
    semantics, security descriptor creation/audit, final paths and flushing
    against a real local Windows filesystem.
    """
    owner_home = tmp_path / "owner-a"
    owner_home.mkdir()
    directory = owner_home / "recordings" / "0123456789abcdef" / "deadbeef"
    first = b'{"step":1}\n'
    second = b'{"step":2}\n'

    secure_append_recording_line(owner_home, directory, first, 1024)
    secure_append_recording_line(owner_home, directory, second, 1024)

    assert (directory / "trace.jsonl").read_bytes() == first + second
    # Instantiate explicitly as part of the smoke test so token/SID and every
    # ctypes prototype are exercised even if the dispatch changes later.
    assert isinstance(CtypesWin32RecordingAPI(), CtypesWin32RecordingAPI)
