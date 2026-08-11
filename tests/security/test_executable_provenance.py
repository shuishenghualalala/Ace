from pathlib import Path

from crew.security.actions import normalize_exec_action


def test_classifier_evidence_does_not_expand_shell_action_authority(tmp_path: Path) -> None:
    plain = normalize_exec_action(
        ["bash", "-lc", "tool status"],
        tmp_path,
        raw_command="tool status",
    )
    classified = normalize_exec_action(
        ["bash", "-lc", "tool status"],
        tmp_path,
        raw_command="tool status",
        shell_kind="bash",
        parsed_commands=(("tool", "status"),),
        canonical_digest="a" * 64,
    )

    assert classified.digest == plain.digest
