"""AUTO_REVIEW fails closed until executable identity is bound end to end."""

from __future__ import annotations

from crew.security.models import ConversationPermissionMode
from crew.security.runtime_client import ShellClassification, ShellVerdict
from crew.tools.builtin import _classification_auto_allows


def test_auto_review_rejects_workspace_same_named_executable() -> None:
    fake = ShellClassification(
        shell_kind="bash",
        raw_command="./echo hi",
        parsed_commands=(("./echo", "hi"),),
        canonical_digest="d" * 64,
        verdict=ShellVerdict.ALLOW_READ_ONLY,
        reason="basename says echo",
    )
    assert _classification_auto_allows(ConversationPermissionMode.AUTO_REVIEW, fake) is False
