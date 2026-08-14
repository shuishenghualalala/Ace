---
name: blueprint
description: Use when choosing between Daimon Blueprint Automation, Widget, Binding,
  and Canvas tools or routing a request to the correct Blueprint domain skill.
metadata:
  zh_name: "蓝图路由"
  zh_description: "在 Daimon Blueprint 的自动化、Widget、绑定和画布工具之间做选择，或将请求路由到正确的 Blueprint 领域技能。"
  query_examples:
    - "这个需求应该用 Blueprint 的哪个能力？"
    - "帮我路由到对应的蓝图技能"
    - "Blueprint 的自动化、Widget、绑定、画布怎么选"
  skillCategoryName: 通用办公
---
# Blueprint

Blueprint has four assets:

- `Automation`: executes work from manual, one-shot, scheduled, interval, condition, or Widget-event triggers.
- `Widget`: renders one responsive `index.html` and owns display data, input state, and UI events.
- `Binding`: connects an Automation artifact to `Widget.slots.main` and routes `Widget.events.submit` back to Automation input.
- `Canvas`: places Widgets and owns placement layout, z-order, and placement-local view state.

## Rules

- Branch on `DaimonToolResponse.ok` before reading `data`.
- Use returned ids; never invent `automationId`, `widgetId`, `bindingId`, `canvasId`, `mountId`, or `viewId`.
- Resolve named assets with `list`; use `read` for details.
- Use the shortest asset chain that satisfies the request.
- Do not report completion until the matching verification below passes.
- Use platform file APIs and `FileResourceRef` for uploads and generated files.
- Automation limits count only enabled regular tasks. Cron jobs and widget tasks have separate limits.
- Disabled tasks are kept. A downgrade disables older tasks by `createdAt`; running tasks finish.

## Routing

- Executable work, schedules, conditions, runs, cancellation, or delivery: load `automation/SKILL.md`.
- HTML UI, data rendering, input controls, or Widget files: load `widget/SKILL.md`, then read `widget/references/runtime-api.md` before iframe API code.
- Automation-to-Widget data delivery or Widget submit routing: load `binding/SKILL.md`.
- Board creation, Widget placement, movement, resizing, z-order, or view state: load `canvas/SKILL.md`.

## Standard Chains

- Static Widget: `Widget.create` -> `Widget.show` for the generating placeholder -> write `workspaceRoot/index.html` -> `Widget.validate` for live refresh -> optionally `Canvas.placeWidget`.
- Automation: `Automation.create` -> write Python entry when using code execution -> `Automation.run` -> `Automation.readRun`.
- Automation-backed Widget: define matching schemas -> create Automation and Widget -> write and validate `index.html` -> `Binding.create` -> run Automation -> verify delivery and `Widget.latestData.main`.
- Public JSON dashboard: create an `http_json` Automation -> create a schema-compatible Widget -> validate -> bind -> run once -> place on Canvas -> enable interval or schedule refresh after the first successful delivery.
- Conversation preview: call `Widget.show` as soon as creation starts; after placements are ready, call `Canvas.show` to keep the complete dashboard mounted beside the bound conversation.
- File Widget: pick and submit `FileResourceRef` -> Automation reads and registers outputs -> Binding -> Widget/Canvas host preview, download, reveal, and copy actions.

## Verification

- Widget: `index.html` renders on the requested surface.
- Automation: the run reaches terminal `status: "succeeded"`.
- Binding delivery: the run has a succeeded delivery row and `Widget.read` shows the expected `latestData.main` and `lastRun`.
- Canvas placement: `Canvas.read` contains the generated `mountId` with the expected `widgetId`.
- `pending_run` means the Binding is configured; it does not mean fresh data has been delivered.

Ownership: Automation owns execution/results; Widget presentation/interaction; Binding the durable edge; Canvas placement only.

## Boundaries

- Pass files between assets as `FileResourceRef`; keep browser `File`, bytes, whole-file text, large base64, local/Resource Store paths, `sourcePath`, and `file:` URLs out of persisted JSON and notifications.
- Run-local paths may be used only inside the current Automation run. Register output files before delivery.
- Widget iframe code uses `window.DaimonWidget` and `window.DaimonCanvas`; it never calls agent-facing Blueprint tools directly.
- Scheduled work uses `Automation.trigger`; do not use legacy Cron, instant-widget, or retired archive paths.
- Treat behavior absent from the current tool schema or an observed tool result as unavailable.
