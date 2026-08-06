---
name: automation
description: Use when creating, reading, updating, deleting, running, cancelling, scheduling, condition-triggering, or inspecting Daimon Blueprint Automations and their run records.
---

# Automation

Automation owns executable work: trigger, input contract, execution, result contract, files, delivery, runs, assets, and workspace.

## Commercial Limits

- Only enabled regular Automations count. Cron jobs and widget tasks use separate limits.
- Disabled tasks are kept and use no slot. Disable one before enabling another when a limit is full.
- On downgrade, the newest tasks by `createdAt` stay enabled. Older tasks are disabled, not deleted.
- A running task finishes after downgrade; disabling blocks future automatic runs.
- If a quota reminder says not to retry, report the limit and ask the user which task to disable.

## Create Contract

`Automation.create` requires sibling top-level fields:

- `title`
- `description`: a concise, non-empty statement of purpose
- `trigger`
- `input`
- `execution`
- `result`

`delivery` is optional. Use it only for local notification delivery; omit it or pass `[]` when no notification is needed.

`input` is only the input contract:

- `{ kind: "none" }`
- `{ kind: "text", defaultInput? }`
- `{ kind: "json", schema, defaultInput? }`

Use `result.kind: "artifact"` for structured output and Widget delivery. The artifact must be a JSON object that matches `result.schema`.

Minimum Python artifact Automation:

```json
{
  "action": "create",
  "title": "Daily metric producer",
  "description": "Produces a daily metric artifact for downstream display.",
  "trigger": { "kind": "manual" },
  "input": {
    "kind": "json",
    "schema": { "type": "object", "properties": {}, "additionalProperties": false },
    "defaultInput": {}
  },
  "execution": {
    "kind": "code",
    "runtime": "python",
    "entryRef": { "kind": "path", "base": "automation", "path": "automation.py" }
  },
  "result": {
    "kind": "artifact",
    "schema": {
      "type": "object",
      "properties": { "summary": { "type": "string" } },
      "required": ["summary"],
      "additionalProperties": true
    }
  },
  "delivery": []
}
```

After create, write the Python program to `data.metadata.codeEntry.path`. The Automation assets root is `data.metadata.roots.assetsRoot`; its run workspace is `data.metadata.roots.workspaceRoot`.

## Update Contract

Every `Automation.update` call must include a top-level, non-empty `description`. Rewrite it to
accurately describe the resulting Automation after the requested changes; never leave stale
purpose or behavior in the description. `description` is a sibling of `trigger`, `input`,
`execution`, `result`, and `delivery`, not a nested field.

## Choose A Trigger

- On demand: `{ "kind": "manual" }`
- Calendar schedule: `{ "kind": "schedule", "cron": "0 7 * * *", "timezone": "Asia/Shanghai" }`
- Fixed interval: `{ "kind": "interval", "every": "15m" }`
- One shot: `{ "kind": "once", "at": "2026-07-15T09:00:00+08:00" }`
- Poll a Python condition: use `kind: "condition"` with `every` and a Python condition entry.

Condition trigger:

```json
{
  "action": "update",
  "automationId": "automation_generated",
  "description": "Runs the configured task when the Python condition returns true.",
  "trigger": {
    "kind": "condition",
    "every": "10m",
    "condition": {
      "kind": "code",
      "runtime": "python",
      "entryRef": {
        "kind": "path",
        "base": "automation",
        "path": "conditions/should_fire.py"
      }
    }
  }
}
```

The condition entry is a Python module under the Automation assets root. It must expose:

```py
def should_fire(ctx):
    return True
```

`ctx` contains `scheduledAt`, `triggeredAt`, `taskDir`, `runDir`, and `input`. `ctx["input"]` includes `automationId`, `scheduledAt`, `observedAt`, and `workspacePath`.

The condition is a predicate, not the business execution. Keep it fast and side-effect-free. It has a 30-second limit and must return a boolean. `False` creates no business run; `True` starts the configured `execution`. A condition error or invalid return is recorded as scheduler skip evidence.

Author automatic Python Automations in this order:

1. Create disabled with a manual trigger when possible.
2. Write and verify the Python execution entry.
3. For a condition trigger, write the Python condition entry under `assetsRoot`.
4. Update the trigger to once, schedule, interval, or condition.
5. Read the Automation and confirm the stored trigger.
6. Enable it.

## Choose The Result Path

### Ace Public HTTP JSON

When the user provides a public HTTP/HTTPS JSON endpoint, prefer Ace's deterministic
`http_json` execution instead of generating Python. It performs the request in the host,
applies outbound-network safety checks, validates the artifact schema, records a durable run,
and delivers through Binding:

```json
{
  "execution": {
    "kind": "http_json",
    "method": "GET",
    "url": "https://example.com/market.json",
    "timeoutSeconds": 15
  },
  "result": {
    "kind": "artifact",
    "schema": {
      "type": "object",
      "properties": { "price": { "type": "number" } },
      "required": ["price"]
    }
  }
}
```

The first Ace release supports `GET` and `POST`, public endpoints, JSON responses up to 5 MiB,
and manual, interval, schedule, or once triggers. Do not place Authorization, Cookie, API keys,
or other secrets in `execution.headers`; authenticated endpoints require a future secret-reference
contract. Widget iframe code must never fetch the endpoint directly.

### Background Artifact

Use a background Agent for open-ended model work such as research, summarization, classification, extraction, or semantic analysis that must produce structured data for a Widget:

```json
{
  "action": "update",
  "automationId": "automation_generated",
  "description": "Uses a background agent to analyze submitted material and produce a structured summary.",
  "execution": {
    "kind": "agent",
    "mode": "background",
    "prompt": "Analyze the submitted material and produce the requested structured result."
  },
  "result": {
    "kind": "artifact",
    "schema": {
      "type": "object",
      "properties": { "summary": { "type": "string" } },
      "required": ["summary"],
      "additionalProperties": true
    }
  }
}
```

Omit `allowedTools`: background Automations inherit the full Daimon tool set. `AutomationResources` and `AutomationOutput` are injected by the runtime. Call `AutomationOutput({ artifact, files? })` exactly once, after the final artifact is ready. Use `mode: "background"` only with `result.kind: "artifact"`.

### Local Conversation

Use a local conversation when the Automation should perform open-ended work in a local workspace and leave a durable conversation. The default recipe includes a notification, but notification is optional:

```json
{
  "action": "update",
  "automationId": "automation_generated",
  "description": "Reviews the selected local project and stores the result as a durable conversation.",
  "execution": {
    "kind": "agent",
    "mode": "local_conversation",
    "workspace": { "kind": "current" },
    "prompt": "Review the workspace and summarize the current project state."
  },
  "result": { "kind": "conversation" },
  "delivery": [
    {
      "kind": "notification",
      "title": "Workspace review finished",
      "desktop": true,
      "actionContext": { "action": "open_run" }
    }
  ]
}
```

For `local_conversation`, always provide a project workspace. `workspace.kind: "current"` is
resolved when the Automation is created and persisted as the current project's canonical absolute
path. Use `{ "kind": "path", "path": "/absolute/project/path" }` to bind another existing project.
Automation-owned directories remain asset/run storage, not the local conversation's project. The
completed run stores `conversationKey`; the notification is a separate delivery result.

To turn notification off, omit `delivery` or pass `delivery: []`. This does not change local-conversation execution or its stored result.

Use `mode: "local_conversation"` only with `result.kind: "conversation"`. Omit `allowedTools` to inherit the full Daimon tool set. Use `timeoutMs` when the default agent limit is not appropriate.

### Widget Delivery

Use Python code for deterministic transforms and background Agent execution for open-ended model work; both use `result.kind: "artifact"`. Create compatible `Widget.slots.main`, then connect it with `Binding.create`. Widget delivery belongs to the Binding, not `delivery`; notification remains an independent optional side effect.

### Notification

Use `delivery: [{ "kind": "notification", ... }]` as an optional side effect for either a local-conversation Automation or a Widget-bound artifact Automation. Notification delivery is attempted for ordinary terminal completion such as success, failure, and timeout. An explicit `Automation.cancel` records a durable `cancelled` run without notification delivery; do not wait for a notification row after cancellation. Omit static fields to use the Automation title, run-derived severity, and default `open_run` action.

## Python Runtime

Python receives one JSON request on stdin:

```ts
type AutomationCodeRequest = {
  automationId: string;
  runId: string;
  input: null | string | Record<string, unknown>;
  resources: {
    contextFile: string;
    files: Array<{ file: FileResourceRef; localPath: string; sizeBytes: number }>;
  };
};
```

Runtime environment variables:

- `DAIMON_BLUEPRINT_AUTOMATION_ID`
- `DAIMON_BLUEPRINT_AUTOMATION_RUN_ID`
- `DAIMON_BLUEPRINT_AUTOMATION_RUN_DIRECTORY`
- `DAIMON_BLUEPRINT_AUTOMATION_WORKSPACE_PATH`
- `DAIMON_BLUEPRINT_AUTOMATION_OUTPUT_FILE`
- `DAIMON_BLUEPRINT_AUTOMATION_RESOURCES_CONTEXT_FILE`

Read stdin once and emit one wrapper JSON object:

```py
import json
import sys

request = json.load(sys.stdin)
print(json.dumps({"artifact": {"summary": "ready"}}))
```

The object inside `artifact` must match `result.schema`. Do not print raw artifact fields as the output contract.

For large or file-backed output, write the same wrapper JSON to `DAIMON_BLUEPRINT_AUTOMATION_OUTPUT_FILE`.

## Artifact And File Output

Python execution returns wrapper JSON through stdout or the output file:

```jsonc
{ "artifact": { "summary": "ready" } }
```

Generated files belong under `DAIMON_BLUEPRINT_AUTOMATION_RUN_DIRECTORY`. Return file descriptors alongside the artifact:

```jsonc
{
  "artifact": { "summary": "Report ready" },
  "files": [
    {
      "sourcePath": "/absolute/run-directory/report.pdf",
      "name": "report.pdf",
      "mimeType": "application/pdf"
    }
  ]
}
```

Each generated file descriptor supports only:

```ts
{ sourcePath: string; name?: string; mimeType?: string; maxBytes?: number }
```

- `sourcePath` must resolve inside the current run directory.
- `maxBytes` is a read limit, not file-size metadata.
- Do not add `fileId`, `sizeBytes`, bytes, base64, or arbitrary metadata to a generated descriptor.
- Automation registers generated descriptors as `automation-output` `FileResourceRef` values in `run.files`.
- Artifact JSON and `run.files` are stored separately. Artifact fields are not mutated to insert file ids.

## File Input

Automation input can contain `FileResourceRef` values selected by a Widget. Treat them as references.

Background Agents use the injected `AutomationResources` tool:

- `listInputFiles`, `readText`, and `readBytes` inspect submitted refs.
- `localPath` materializes an input into the current run workspace only.
- `registerOutputFile` registers a run-local result and returns a `FileResourceRef`.
- Pass registered refs in the single final `AutomationOutput({ artifact, files })` call.

Python reads materialized resources through `DAIMON_BLUEPRINT_AUTOMATION_RESOURCES_CONTEXT_FILE`:

```py
import json
import os

with open(os.environ["DAIMON_BLUEPRINT_AUTOMATION_RESOURCES_CONTEXT_FILE"], encoding="utf-8") as file:
    resources = json.load(file)

input_file = request["input"]["inputFile"]
local_path = next(
    entry["localPath"]
    for entry in resources.get("files", [])
    if (entry.get("file") or {}).get("fileId") == input_file["fileId"]
)
```

Match `entry.file.fileId`; the local materialized path is runtime-only. Persist and deliver `FileResourceRef`, never browser `File`, local paths, `sourcePath`, raw bytes, whole-file text, or large base64 inside artifact JSON or notifications. Python outputs must stay under the run directory; background Agent outputs must be registered with `AutomationResources.registerOutputFile`.

## Run And Verify

Run with stored default input:

```json
{ "action": "run", "automationId": "automation_generated" }
```

Run with one-off input:

```json
{
  "action": "run",
  "automationId": "automation_generated",
  "runInput": { "topic": "AI infrastructure", "limit": 5 }
}
```

Verification:

- A run succeeds only when `Automation.run` or `Automation.readRun` reaches terminal `status: "succeeded"`.
- `failed`, `timeout`, `cancelled`, and `skipped` are terminal non-success states.
- Check `deliveryResults` separately; execution can succeed while delivery fails.
- For artifacts, use `latestArtifactRunId` or the current succeeded artifact run.
- Use `readRunArtifact` to inspect the complete artifact and `readRunLogs` for failure evidence.
- For files, confirm expected `run.files` entries.
- For Widget delivery, require a succeeded delivery row and verify `Widget.latestData.main`, status, and `lastRun`.
- For local conversations, require `resultKind: "conversation"` and a real `conversationKey`.
- For notifications, require a `deliveryResults` row with `kind: "notification"` and `status: "succeeded"`.
- For an explicit cancellation, require terminal `status: "cancelled"`; no notification delivery row is expected.
- For schedule, interval, or condition triggers, confirm a real triggered run before reporting the automatic path as working.

If a run is still `running`, poll `readRun`; do not start another run. If `Automation.run` returns `ok: false` with `error.details.run`, inspect that durable run before retrying.

If validation fails, repair the reported field or owning contract; do not retry unchanged input.

## Read Run Content

`Automation.list` and `Automation.listRuns` return paged results. Use `limit`, `offset`, and `data.page.nextOffset`; when a result is summarized or truncated, narrow the query or use a specific content-read action.

`Automation.readRun` returns metadata and summaries. Read full content with:

- `readRunInput`
- `readRunLogs`
- `readRunArtifact`
- `readRunTranscript`

Use `byteOffset` and `maxBytes` for chunked reads.

## Update, Disable, Cancel, And Delete

- Prefer disabling an enabled automatic Automation before changing its trigger, execution entry, input contract, or result schema. The tool enforces this only while a run is active; disabling first prevents scheduler races during multi-step edits.
- After `Automation.update`, inspect `revalidatedBindings`; repair invalid bindings before enabling or running.
- A running Automation does not re-enter. A concurrent trigger is skipped or returns `already_running`.
- Running update/delete returns `run_in_progress`. Cancel the active run and wait for a terminal state first.
- Cancellation does not deliver artifacts, Widget data, external results, or notifications, and does not overwrite the last successful Widget data.
- Before delete, decide whether bound Widgets should remain. `cascadeWidgetIds` deletes only explicitly selected eligible Widgets and their placements; otherwise Automation.delete removes the Automation and its Binding edges.
- Inspect the delete result for removed bindings, Widgets, and placements.
