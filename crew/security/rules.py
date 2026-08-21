"""Token-aware in-memory rule matching shared by approvals and persistence."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from crew.security.actions import ActionKind, NormalizedAction, normalize_exec_action
from crew.security.models import (
    EMPTY_ADDITIONAL_PERMISSIONS,
    AdditionalPermissionProfile,
    SandboxPermissions,
)


class RuleScope(StrEnum):
    ALWAYS = "always"
    SESSION = "session"
    ONCE = "once"


class RuleDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class ActionRule:
    """An exact transient grant or an explicit persistent argv prefix."""

    scope: RuleScope
    decision: RuleDecision
    kind: ActionKind
    exact_digest: str = ""
    argv_prefix: tuple[str, ...] = ()
    cwd: str = ""
    rule_id: str = field(default_factory=lambda: uuid4().hex)
    # Redacted, user-facing provenance. These fields never participate in matching.
    action_summary: str = ""
    action_detail: str = ""
    additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS
    allow_prefix_authority: bool = False
    tool_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scope, RuleScope):
            raise ValueError("rule scope 无效")
        if not isinstance(self.decision, RuleDecision):
            raise ValueError("rule decision 无效")
        if not isinstance(self.kind, ActionKind):
            raise ValueError("rule kind 无效")
        if (
            not isinstance(self.rule_id, str)
            or not self.rule_id
            or len(self.rule_id) > 128
            or "\x00" in self.rule_id
        ):
            raise ValueError("rule_id 无效")
        if not isinstance(self.exact_digest, str):
            raise ValueError("exact_digest 必须是字符串")
        if not isinstance(self.argv_prefix, tuple) or not all(
            isinstance(token, str)
            and token
            and "\x00" not in token
            and len(token) <= 4096
            for token in self.argv_prefix
        ):
            raise ValueError("argv_prefix 必须是非空字符串 token 元组")
        if len(self.argv_prefix) > 256:
            raise ValueError("argv_prefix token 过多")
        has_exact = bool(self.exact_digest)
        has_prefix = bool(self.argv_prefix)
        if has_exact == has_prefix:
            raise ValueError("规则必须且只能包含 exact_digest 或 argv_prefix")
        if has_exact:
            if len(self.exact_digest) != 64 or any(
                char not in "0123456789abcdef" for char in self.exact_digest
            ):
                raise ValueError("exact_digest 必须是 SHA-256 hex")
            if self.cwd:
                raise ValueError("exact 规则不得携带 cwd")
        else:
            if self.kind is not ActionKind.EXEC:
                raise ValueError("argv_prefix 只适用于 exec 规则")
            if (
                not isinstance(self.cwd, str)
                or not self.cwd
                or "\x00" in self.cwd
                or not Path(self.cwd).is_absolute()
                or str(Path(self.cwd).expanduser().resolve(strict=False)) != self.cwd
            ):
                raise ValueError("argv_prefix cwd 必须是规范化绝对路径")
        if not isinstance(self.action_summary, str) or len(self.action_summary) > 500:
            raise ValueError("action_summary 必须是不超过 500 字符的字符串")
        if not isinstance(self.action_detail, str) or len(self.action_detail) > 4000:
            raise ValueError("action_detail 必须是不超过 4000 字符的字符串")

    @classmethod
    def exact(
        cls,
        action: NormalizedAction,
        *,
        scope: RuleScope,
        decision: RuleDecision = RuleDecision.ALLOW,
        additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
        tool_name: str = "",
    ) -> ActionRule:
        """Create an exact digest rule; safe for shell exec because raw+final argv are bound."""
        return cls(
            scope=scope,
            decision=decision,
            kind=action.kind,
            exact_digest=action.digest,
            additional_permissions=additional_permissions,
            tool_name=str(tool_name).strip(),
        )

    @classmethod
    def exec_prefix(
        cls,
        argv_prefix: Sequence[str],
        *,
        cwd: str | Path,
        decision: RuleDecision = RuleDecision.ALLOW,
        additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
        allow_authority: bool = False,
        tool_name: str = "",
    ) -> ActionRule:
        action = normalize_exec_action(argv_prefix, cwd)
        return cls(
            scope=RuleScope.ALWAYS,
            decision=decision,
            kind=ActionKind.EXEC,
            argv_prefix=action.argv,
            cwd=action.cwd,
            additional_permissions=additional_permissions,
            allow_prefix_authority=allow_authority,
            tool_name=str(tool_name).strip(),
        )

    def matches(self, action: NormalizedAction) -> bool:
        if action.kind is not self.kind:
            return False
        if self.exact_digest:
            return action.digest == self.exact_digest
        # Only host-validated, newly persisted allow prefixes carry authority.
        # Legacy prefixes remain inert while deny prefixes stay active.
        if self.decision is not RuleDecision.DENY:
            if not self.allow_prefix_authority or self.tool_name != "terminal":
                return False
            if (
                self.additional_permissions.sandbox_permissions
                is not SandboxPermissions.REQUIRE_ESCALATED
            ):
                return False
        candidates = action.parsed_commands or (action.argv,)
        return action.cwd == self.cwd and any(
            len(candidate) >= len(self.argv_prefix)
            and tuple(candidate[: len(self.argv_prefix)]) == self.argv_prefix
            for candidate in candidates
        )

    @property
    def specificity(self) -> int:
        return 10_000 if self.exact_digest else len(self.argv_prefix)


def choose_rule(rules: Iterable[ActionRule], action: NormalizedAction) -> ActionRule | None:
    """Choose the most specific matching rule, with deny winning at equal specificity.

    Spec §5.3 优先级：更具体 > 更宽泛；同 specificity 时 deny 优先于 allow；scope 仅作末位
    tiebreaker。此前把 scope_rank 放在 deny 之前，会使一条 ONCE ALLOW 压倒 ALWAYS DENY
    （P3 exec deny 规则一旦落地即变漏洞）。这里把 specificity 提到首位、deny 紧随其后。
    """
    matches = [rule for rule in rules if rule.matches(action)]
    if not matches:
        return None
    scope_rank = {RuleScope.ALWAYS: 1, RuleScope.SESSION: 2, RuleScope.ONCE: 3}
    return max(
        matches,
        key=lambda rule: (
            rule.specificity,
            1 if rule.decision is RuleDecision.DENY else 0,
            scope_rank[rule.scope],
        ),
    )
