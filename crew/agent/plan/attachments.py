"""Hidden Plan-mode attachment messages.

This mirrors Crew's plan_mode attachment behavior while keeping Crew's provider
message shape unchanged: attachments are hidden user system-reminders with
metadata used for history scanning and throttling.
"""

from __future__ import annotations

from typing import Literal

from crew.core.types import Message

from .manager import PlanModeManager, plan_display_path, read_plan
from .prompts import (
    PLAN_EXIT_REMINDER,
    PLAN_REENTRY_REMINDER,
    PLAN_WORKFLOW_INSTRUCTIONS,
    SPARSE_PLAN_WORKFLOW_INSTRUCTIONS,
)

PLAN_MODE_TURNS_BETWEEN_ATTACHMENTS = 5
PLAN_MODE_FULL_REMINDER_EVERY_N_ATTACHMENTS = 5

PlanAttachmentType = Literal["plan_mode", "plan_mode_reentry", "plan_mode_exit"]


def create_plan_attachment_message(
    attachment_type: PlanAttachmentType,
    content: str,
    *,
    data: dict | None = None,
) -> Message:
    """Create a hidden attachment message that persists in canonical history."""
    msg = Message.system_reminder(content)
    msg.attachment_type = attachment_type
    msg.attachment_data = data or {}
    return msg


def is_plan_attachment(message: Message, *types: str) -> bool:
    if not message.is_meta:
        return False
    if not message.attachment_type:
        return False
    return message.attachment_type in types if types else message.attachment_type.startswith("plan_")


def _is_real_user_turn(message: Message) -> bool:
    return message.role == "user" and not message.is_meta and not message.tool_call_id


def count_user_turns_since_last_plan_attachment(messages: list[Message]) -> tuple[int, bool]:
    """Count real user turns since the latest plan_mode/reentry attachment."""
    turns = 0
    found = False
    for message in reversed(messages):
        if _is_real_user_turn(message):
            turns += 1
            continue
        if is_plan_attachment(message, "plan_mode", "plan_mode_reentry"):
            found = True
            break
    return turns, found


def count_plan_attachments_since_last_exit(messages: list[Message]) -> int:
    """Count plan_mode attachments since the most recent plan_mode_exit."""
    count = 0
    for message in reversed(messages):
        if is_plan_attachment(message, "plan_mode_exit"):
            break
        if is_plan_attachment(message, "plan_mode"):
            count += 1
    return count


def get_plan_mode_attachment_messages(
    messages: list[Message],
    session_id: str,
    manager: PlanModeManager,
    *,
    owner_account_id: str | None = None,
) -> list[Message]:
    """Return Crew-style hidden plan attachments for this user turn."""
    attachments: list[Message] = []
    plan_file = plan_display_path(session_id, owner_account_id=owner_account_id)

    if not manager.is_active(session_id, owner_account_id=owner_account_id):
        if manager.take_plan_exit_attachment(session_id, owner_account_id=owner_account_id):
            plan_exists = bool(read_plan(session_id, owner_account_id=owner_account_id))
            content = PLAN_EXIT_REMINDER
            if plan_exists:
                content += f"\n\nThe plan file is located at {plan_file} if you need to reference it."
            attachments.append(
                create_plan_attachment_message(
                    "plan_mode_exit",
                    content,
                    data={"plan_file": plan_file, "planExists": plan_exists},
                )
            )
        return attachments

    turns, found = count_user_turns_since_last_plan_attachment(messages)
    if found and turns < PLAN_MODE_TURNS_BETWEEN_ATTACHMENTS:
        return []

    plan_exists = bool(read_plan(session_id, owner_account_id=owner_account_id))
    if manager.take_plan_reentry_attachment(session_id, owner_account_id=owner_account_id) and plan_exists:
        attachments.append(
            create_plan_attachment_message(
                "plan_mode_reentry",
                PLAN_REENTRY_REMINDER.format(plan_file=plan_file),
                data={"plan_file": plan_file, "planExists": True},
            )
        )

    attachment_count = count_plan_attachments_since_last_exit(messages) + 1
    reminder_type = (
        "full"
        if attachment_count % PLAN_MODE_FULL_REMINDER_EVERY_N_ATTACHMENTS == 1
        else "sparse"
    )
    template = (
        PLAN_WORKFLOW_INSTRUCTIONS
        if reminder_type == "full"
        else SPARSE_PLAN_WORKFLOW_INSTRUCTIONS
    )
    attachments.append(
        create_plan_attachment_message(
            "plan_mode",
            template.format(plan_file=plan_file),
            data={
                "reminderType": reminder_type,
                "plan_file": plan_file,
                "planExists": plan_exists,
            },
        )
    )
    return attachments
