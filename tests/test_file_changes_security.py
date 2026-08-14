from pathlib import Path

from crew.agent.file_changes import (
    FILE_CHANGE_MAX_BYTES,
    FileState,
    TurnFileChangeTracker,
    external_tool_write_paths,
    file_change_from_states,
    metadata_change,
    read_file_state,
)


def test_external_tool_paths_stay_inside_tracker_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("before", encoding="utf-8")

    assert external_tool_write_paths(
        "file_write", {"path": str(outside)}, cwd=root
    ) == []
    assert external_tool_write_paths(
        "file_write", {"path": str(Path("..") / outside.name)}, cwd=root
    ) == []

    tracker = TurnFileChangeTracker(root)
    tracker.capture_tool_start("file_write", {"path": str(outside)})
    outside.write_text("after", encoding="utf-8")
    tracker.capture_tool_end("file_write", {"path": str(outside)})

    assert tracker.finalize() == []


def test_oversized_files_are_metadata_only_not_text_diffs(tmp_path):
    target = tmp_path / "large.txt"
    target.write_bytes(b"x" * (FILE_CHANGE_MAX_BYTES + 1))

    state = read_file_state(target)
    assert state == FileState(True, binary=True)

    change = file_change_from_states(target, FileState(False), state)
    assert change["binary"] is True
    assert change["diff"] == []

    metadata = metadata_change(str(target), "added")
    assert metadata["binary"] is True
    assert metadata["added"] == 0
    assert metadata["diff"] == []


def test_oversized_states_do_not_reach_text_diff(tmp_path):
    target = tmp_path / "large.txt"
    large_text = "x" * (FILE_CHANGE_MAX_BYTES + 1)

    change = file_change_from_states(
        target,
        FileState(True, text=large_text),
        FileState(True, text="small"),
    )

    assert change["binary"] is True
    assert change["added"] == 0
    assert change["removed"] == 0
    assert change["diff"] == []


def test_sensitive_files_are_not_exposed_as_text_diffs(tmp_path):
    target = tmp_path / ".env"
    target.write_text("API_KEY=secret-value\n", encoding="utf-8")

    state = read_file_state(target)
    change = file_change_from_states(target, FileState(False), state)

    assert state.binary is True
    assert change["binary"] is True
    assert change["diff"] == []
