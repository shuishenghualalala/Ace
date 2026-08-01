"""Plan 模式提示词。

提示词覆盖进入、退出、完整提醒、精简提醒、重入提醒和退出提醒，避免每轮循环
重复完整探索指令导致模型死循环。当前版本采用「只读规划 + 计划文件 + 审批闸门」
机制，面向多步骤、高影响、存在不确定性的通用任务规划。计划文件路径由
``{plan_file}`` 占位，运行时填入当前 agent 工作目录下的相对路径
``plans/plan_<timestamp>.md``，避免把本机绝对路径暴露给模型。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# enter_plan_mode 工具描述
# --------------------------------------------------------------------------- #
ENTER_PLAN_MODE_PROMPT = """ The model must NOT proactively enter plan mode. Plan mode is only entered when the user explicitly selects Plan in the UI, sends the WebSocket `plan_enter` action / first-message `plan_active` flag, or explicitly says they want to use plan mode via the CLI `/plan` command.

## When to Use This Tool

Do not call this tool in normal model turns. If the user did not explicitly activate Plan through UI/CLI/gateway controls, or if the user's message is a greeting, chitchat, or a simple question that does not require implementation planning, answer or act within the currently available tool permissions. Do not call this tool.

## What Happens in Plan Mode

If plan mode is explicitly activated, you will:
1. Gather the necessary context using only read-only tools (file_read, glob, grep).
2. Clarify goals, constraints, risks, dependencies, and success criteria with ask_followup_question when needed.
3. Design a concrete execution approach.
4. Write your plan to the plan file using file_write (incrementally, after each discovery — do not wait until the end).
5. Present your plan to the user for approval by calling exit_plan_mode.
6. After approval, exit plan mode and proceed with full tool permissions.

## Important Notes

- This tool transitions the conversation into a READ-ONLY planning phase — you cannot make changes (except writing the plan file) until the user approves your plan.
- If the user did not explicitly activate Plan, or if the task is trivial, do not call this tool yourself.
- If the user is already in plan mode but asks a simple question or greets you, answer normally instead of re-entering plan mode.
"""


# --------------------------------------------------------------------------- #
# exit_plan_mode 工具描述
# --------------------------------------------------------------------------- #
EXIT_PLAN_MODE_PROMPT = """Use this tool when you are in plan mode and have finished writing your plan to the plan file and are ready for user approval.

## How This Tool Works
- You should have already written your plan to the plan file specified in the plan mode system message
- This tool does NOT take the plan content as a parameter - it will read the plan from the file you wrote
- This tool simply signals that you're done planning and ready for the user to review and approve
- The user will see the contents of your plan file when they review it
- If the plan file is empty, the user will be shown an "empty plan" notice and will NOT be asked to approve — so make sure you have written your plan to the file first before calling this tool

## When to Use This Tool
IMPORTANT: Only use this tool when the task requires planning the execution steps of a non-trivial task that may change persistent state or create user-visible deliverables, and you have already written that plan to the plan file. This is a general-purpose multi-agent platform: the task may involve code, documents, spreadsheets, slides, data, configuration, messages, workflows, external tools, or other systems. For greetings, chitchat, simple Q&A, pure read-only research, exploration, summarization, or analysis where the reply itself completes the task, do NOT use this tool — just reply in plain text.

## Before Using This Tool
Ensure your plan is complete and unambiguous:
- If you have unresolved questions about requirements or approach, ask the user directly in your response first
- Once your plan is finalized, use THIS tool to request approval

**Important:** Do NOT ask "Is this plan okay?" or "Should I proceed?" in text - that's exactly what THIS tool does. exit_plan_mode inherently requests user approval of your plan.
"""


# --------------------------------------------------------------------------- #
# Plan 工作流指引（经 <system-reminder> 注入；使用 full/sparse/reentry
# 切换策略，避免每轮 loop 重复完整 explore 指令导致死循环）
# --------------------------------------------------------------------------- #
PLAN_WORKFLOW_INSTRUCTIONS = """Plan mode is active. The user indicated that they do not want you to execute yet — you MUST NOT make any edits (with the exception of the plan file mentioned below), run any non-readonly tools (including changing configs or making commits), or otherwise make any changes to the system. This supersedes any other instructions you have received.

## Core Principle (READ THIS FIRST)
The ONLY correct way to present a plan for user review is a two-step sequence:
1. Write the complete plan to {plan_file} using file_write.
2. Call exit_plan_mode to submit it for approval.

You MUST NOT describe the plan in chat text and then ask the user whether to proceed. Do NOT write "Here's my plan:" / "我的计划是" followed by bullet points or numbered steps in your response. Do NOT ask "Shall I proceed?" / "可以开始吗?" / "这个计划可以吗?" in text — exit_plan_mode IS the approval request; calling it is how you ask for approval. The user reviews the plan FILE, not your chat message. If you catch yourself typing plan content (headings, steps, numbered lists, "Plan:", "计划:", "目标", "步骤", "Verification") into the chat, STOP immediately and redirect that content to {plan_file} via file_write instead. Your chat text in plan mode should at most be a brief one-line note like "Plan written to file, submitting for approval" — nothing more.

NOTE: The `todo` tool only updates your  task checklist (metadata) and does NOT modify files, configs, or any system state. It is allowed, but it is NOT the approval artifact. In plan mode, the formal user-reviewable plan MUST be written to the plan file; do not use todo as a substitute for the plan file.

## Plan File Info
You should create your plan at {plan_file} using the file_write tool. NOTE that this is the only file you are allowed to edit — other than this you are only allowed to take READ-ONLY actions.

Do NOT read or list {plan_file}, its parent directory, or any generic plans/plan.md path just to check whether a plan already exists. Treat {plan_file} as the designated output path for this planning turn; file_write will create or overwrite it as needed. Only read an existing plan file when you receive PLAN_REENTRY_REMINDER.

## Workflow

Follow these steps in order. Do not loop forever on step 1.

1. **Understand** — Quickly explore the codebase with file_read, glob, and grep. Read at most 3-5 key files to form an initial understanding.
2. **Design** — Converge on the best approach; consider trade-offs but aim for a single recommendation.
3. **Review** — Re-read key files to verify understanding, if needed.
4. **Write Plan** — Use the file_write tool to write the complete plan to {plan_file}. Update it incrementally as you learn, but never skip this step. Do NOT output the plan content in chat text; the user-reviewable plan must live in the plan file.
5. **Exit** — Call exit_plan_mode for user approval when the plan is ready.

## First Turn

Start by quickly scanning 3-5 key sources of context to form an initial understanding of the task scope. Then IMMEDIATELY write a skeleton plan (headers and rough notes) to {plan_file} using file_write. On planning tasks, your first substantial plan content must be a file_write call to {plan_file}, not assistant text. Do NOT inspect {plan_file}, its parent directory, or any generic plans/plan.md path before writing. Do NOT read more than 5 files before writing the skeleton. Do NOT explore exhaustively before writing anything, and do NOT ask the user to approve a plan you have not yet written to the file.

## When NOT to Plan (Chitchat Exemption)

Plan mode restricts you to read-only actions, but **not every user message requires a plan**. If the user's message is a greeting, chitchat, a simple question, an explanation request, or pure read-only research that does NOT involve planning execution steps for a persistent or user-visible change, reply normally in plain text. Do NOT write to the plan file, do NOT call `exit_plan_mode`, and do NOT force the conversation toward planning.

If the user's request requires any future execution, deliverable creation, file change, document generation, message sending, workflow operation, external tool operation, or other persistent/user-visible output, it is NOT pure read-only research. In that case, you MUST write the plan to {plan_file} with file_write and then call exit_plan_mode. Do NOT present the plan in chat text.

Only enter the understand → write-plan → `exit_plan_mode` flow when the user's message actually describes a task that needs an implementation plan (multi-step, ambiguous, persistent changes, etc.). For a one-line greeting or simple Q&A, a direct text reply is the correct end of turn.

## Ending Your Turn

If the message DOES require planning, your turn should end in one of two ways:
1. Call `ask_followup_question` tool — to clarify a requirement or choose between approaches.
2. Call `exit_plan_mode` tool — to request approval of the plan you have written to the plan file.

Never present the plan directly in assistant text. The user-reviewable plan must exist only in {plan_file}. If you are about to write headings like "Plan", "计划", "目标", "步骤", "Verification", "验证", or numbered execution steps in chat, STOP and write that content to {plan_file} using file_write instead.

Do NOT ask about plan approval via text or via `ask_followup_question` — `exit_plan_mode` IS the approval request. Do NOT reference "the plan" in your `ask_followup_question` questions (e.g., "Does the plan look good?"), because the user cannot see the plan file in the UI until you call `exit_plan_mode`.

## Plan File Structure
- Begin with a **Context** section: explain why this change is being made — the problem or need it addresses, what prompted it, and the intended outcome.
- Include only your recommended approach, not all alternatives.
- Ensure that the plan file is concise enough to scan quickly, but detailed enough to execute effectively.
- Include the concrete artifacts, systems, documents, files, records, messages, or workflows that will be changed or produced.
- Reference existing resources, functions, templates, APIs, tools, people, or processes you found that should be reused, with paths or identifiers when available.
- Include a verification section describing how to check the result end-to-end. This may be automated tests, rendered document review, API checks, UI checks, data validation, message previews, or other task-appropriate verification.

## When to Converge
Your plan is ready when it covers: what to change or produce, which artifacts/systems are involved, what existing resources should be reused, and how to verify the result. Call `exit_plan_mode` when the plan is ready for approval. If the plan file is empty, the user will be shown an "empty plan" notice and will NOT be asked to approve — so make sure you have written your plan to the file first.

## Do NOT Execute in Plan Mode
Plan mode is read-only. After you finish writing the plan to {plan_file}, call `exit_plan_mode` to submit it for user approval. You must NOT start executing the plan or make any changes until the user approves and plan mode exits.
"""


# --------------------------------------------------------------------------- #
# Sparse reminder：在两次 full reminder 之间注入，避免每轮 loop 重复完整 explore
# 指令。
# --------------------------------------------------------------------------- #
# SPARSE_PLAN_WORKFLOW_INSTRUCTIONS = """Plan mode still active (see full instructions earlier in this conversation). Read-only except the current plan file {plan_file}. Use file_write to create or update the plan file. If you need user input to finish the plan, call ask_followup_question. When the plan is ready, call exit_plan_mode for approval.

# Do NOT continue exploring — finish writing the plan now. Never ask about plan approval via text or ask_followup_question."""


SPARSE_PLAN_WORKFLOW_INSTRUCTIONS = """Plan mode still active. You are in the READ-ONLY planning phase. Do NOT execute the task. Do NOT create deliverables. Do NOT edit files except the current plan file {plan_file}. Do NOT run mutating commands. Do NOT present the plan in chat text. The plan must be written to {plan_file} with file_write. When ready, call exit_plan_mode. If you need clarification, call ask_followup_question. Your next action should be file_write, ask_followup_question, or exit_plan_mode.
"""



# --------------------------------------------------------------------------- #
# Reentry reminder：恢复已有计划文件时使用，提醒模型先读旧计划再决定更新/替换。
# Reentry reminder：恢复已有计划文件时使用。
# --------------------------------------------------------------------------- #
PLAN_REENTRY_REMINDER = """Plan mode is active. A plan file from a previous planning session already exists at {plan_file}.

Before proceeding:
1. Read the existing plan file to understand what was previously planned.
2. Evaluate the user's current request against that plan.
3. If different task: replace the old plan with a fresh one. If same task: update the existing plan.
4. Use file_write to modify the plan file.
5. Use ask_followup_question to clarify missing requirements or user preferences that affect the plan.

Your turn must end with either ask_followup_question tool (to clarify requirements) or exit_plan_mode tool (to request plan approval). Do NOT continue exploring without first reading the existing plan file."""


# --------------------------------------------------------------------------- #
# Exit reminder：退出 plan 模式后的一次性提醒，告知模型约束已解除。
# Exit reminder：退出 plan 模式后使用。
# --------------------------------------------------------------------------- #
PLAN_EXIT_REMINDER = """Plan mode is no longer active. The read-only and plan-file-only restrictions no longer apply. Continue with the approved plan using the normal tool and permission rules."""


# --------------------------------------------------------------------------- #
# 审批通过后的执行提示（用户在 CLI 批准计划后，下一轮注入一次）
# --------------------------------------------------------------------------- #
# 注入正文上限：避免长计划撑爆 context；超限时截断并指向 plan 文件。
PLAN_APPROVED_CONTENT_MAX_CHARS = 12_000

PLAN_APPROVED_REMINDER = """The user has approved your plan. Plan mode has exited, and you now have full tool permissions.

The approved plan file is at {plan_file}. The authoritative plan content (including any user edits made in the plan board before approval) is:

----- BEGIN APPROVED PLAN -----
{plan_content}
----- END APPROVED PLAN -----

You MUST execute according to this approved plan content. Do NOT fall back to any earlier draft that may appear in chat history. If the chat shows a different plan, ignore it and follow the block above.
Please proceed and use the todo tool to track progress (check off each step promptly for complex tasks)."""


def format_approved_plan_content(plan_content: str, *, max_chars: int = PLAN_APPROVED_CONTENT_MAX_CHARS) -> str:
    """截断过长的批准计划正文，保留文件路径作为权威来源提示。"""
    text = plan_content if isinstance(plan_content, str) else str(plan_content or "")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return (
        text[:max_chars]
        + f"\n\n...[truncated {omitted} chars; read the full plan from the plan file path above]..."
    )


# --------------------------------------------------------------------------- #
# Todo 内部提醒（只注入给模型，不作为用户可见消息渲染）
# --------------------------------------------------------------------------- #
TODO_REMINDER = """# todo_reminder
This session is suitable for tracking progress with the todo tool. Before continuing, call todo to break the task into checkable steps; if a list already exists, update pending / in_progress / completed status. Keep only one in_progress at a time, and mark completed promptly when a step is done.
This is an internal reminder; do not repeat it to the user."""
