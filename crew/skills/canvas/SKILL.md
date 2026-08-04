---
name: canvas
description: Use when creating, reading, updating, or deleting Daimon Blueprint Canvases and when placing, moving, resizing, ordering, removing, or inspecting Widget placements and placement-local view state.
---

# Canvas

Canvas is the Blueprint placement surface. It owns Canvas metadata and Widget placement state:

- `mountId`
- `widgetId`
- layout
- z-order
- placement-local view state

Widget definition, data, input, Binding, and Automation execution remain owned by their respective assets.

When Binding delivery includes output `FileResourceRef` values, the host projects them as placement attachments. Host-owned actions support preview, download, reveal, and copying the safe ref; Widget iframe code must not expose local paths, fabricate download URLs, or call native file actions directly.

## Standard Flow

Use generated ids from tool responses:

```text
Canvas.list or Canvas.create
-> Widget.create or Widget.list/Widget.read
-> write and validate Widget index.html when creating
-> Canvas.placeWidget({ canvasId, widgetId, layout })
-> Canvas.read({ canvasId })
```

For Canvas-only display, place the Widget directly. Use `Widget.show` only when the user also wants conversation or dock display.

## Conversation Preview

After resolving a real Canvas id, emit a preview block when the user should open that Canvas from the conversation:

```daimon-canvas
canvasId: canvas_generated
title: Canvas title
```

Use `canvasId` returned by `Canvas.create`, `Canvas.list`, or `Canvas.read`. The preview opens the existing Canvas in the client.

## Place And Update

Default grid placement:

```json
{
  "action": "placeWidget",
  "canvasId": "canvas_generated",
  "widgetId": "widget_generated",
  "layout": { "mode": "grid", "x": 0, "y": 0, "w": 5, "h": 8 }
}
```

Update layout and placement-local state:

```json
{
  "action": "updatePlacement",
  "canvasId": "canvas_generated",
  "mountId": "mount_generated",
  "layout": { "mode": "grid", "x": 5, "y": 0, "w": 5, "h": 8 },
  "viewState": { "main": { "tab": "overview" } }
}
```

`layout` is exactly `{ mode, x, y, w, h }`, where mode is `"grid"` or `"free"`. Pass `zOrder` as a top-level field. Omit it when normal front-most ordering is sufficient.

## Current Size Model

Grid defaults:

- 12 columns
- column width `80px`
- row height `32px`
- gap `12px`
- horizontal step `92px`
- vertical step `44px`

Free-layout placement design policy:

- minimum `240x160`
- default `420x320`
- maximum `960x1440`

Grid placement spans:

- minimum `2x2`
- default `5x8`
- maximum `12x33`

Grid spans include gaps. Width is:

```text
w * 80 + (w - 1) * 12
```

Useful rendered widths:

- 2 columns: `172px`
- 3 columns: `264px`
- 5 columns: `448px`
- 12 columns: `1092px`

Use `5x8` for a normal Widget unless its content clearly needs another size. The same Widget page must respond continuously while the user resizes the placement.

## Shared And Local State

- `widgetId` identifies shared Widget definition, latest data, status, input state, and active Binding.
- `mountId` identifies one Canvas placement.
- Multiple placements of the same Widget share Widget data and Automation delivery.
- `viewState.main` belongs to one placement and stores presentation state such as a selected tab or local filter.
- `viewState.__daimonCanvasLayouts` is maintained by the renderer for grid/free layout continuity.

Inside a Canvas Widget, read `widget/references/runtime-api.md` before using `window.DaimonCanvas`, then persist placement-local state with `setViewState(...)`.

## Tool Actions

- `list`: resolve Canvas metadata with `limit` and `offset`, then follow `data.page.nextOffset`.
- `create`: create a Canvas with a generated id.
- `read`: read placements and placement-local state.
- `update`: change Canvas title or purpose.
- `placeWidget`: place an existing Widget and return a generated `mountId`.
- `updatePlacement`: update layout, z-order, or view state.
- `removePlacement`: remove one placement.
- `delete`: remove one Canvas and all of its placements.

## Verification

After placement or layout changes, call `Canvas.read` and confirm:

- the expected `mountId` exists
- it references the expected `widgetId`
- layout and z-order match the requested result
- placement-local view state is stored under `viewState.main`

Canvas placement proves layout only. Verify Widget rendering, Binding delivery, and Automation runs through their owning tools.

## Remove And Delete

- `Canvas.removePlacement` removes one placement, its layout, z-order, and placement-local view state.
- `Canvas.delete` removes Canvas metadata and all placements on that Canvas.
- Widgets, Bindings, Automations, run history, and shared Widget data remain available after these Canvas operations.
