---
name: widget
description: Use when creating, showing, updating, validating, deleting, rendering, or troubleshooting Daimon Blueprint Widgets, their responsive index.html UI, data slots, submit events, input state, files, and workspace resources. You MUST read widgetdesign skill before creating any widget.
---

# Widget

Widget owns one responsive `index.html`, display data, input state, events, files, and workspace resources. Every surface renders `data.metadata.roots.workspaceRoot/index.html`.

## Create And Show

Create the Widget definition first:

```json
{
  "action": "create",
  "title": "Status card",
  "description": "Shows the latest pipeline status.",
  "type": "html",
  "creationHints": ["Checking live status", "Preparing the status card"]
}
```

Then:

1. Write a complete HTML document to `data.metadata.roots.workspaceRoot/index.html`.
2. Call `Widget.validate`.
3. For conversation or dock display, call `Widget.show`.
4. For Canvas display, call `Canvas.placeWidget` with the returned `widgetId`.

`creationHints` accepts 1-6 short progress lines. The host displays them while the page is being authored. Static Widgets need no slots, events, Binding, or Automation.

For appearance and responsive layout, load `widgetdesign/SKILL.md` before writing HTML/CSS. It is
the self-contained Widget design authority; do not load another Widget Design Skill for the same
work.

## Inspect And Modify An Existing Widget

1. Resolve the Widget with `Widget.list` or a known `widgetId`; follow `data.page.nextOffset`.
2. Call `Widget.read` and inspect its slots, events, active Binding, status, input state, latest data, and `data.metadata.roots.workspaceRoot`.
3. Read the existing `index.html` before editing it. Preserve working behavior unless the user requested a replacement.
4. If metadata, slots, or events change, call `Widget.update` and inspect `revalidatedBindings`. Do not call update merely to activate `index.html`.
5. Write the updated complete document, then call `Widget.validate` and repair any invalid owning contract before running again.
6. If the Widget has an active Binding, call `Binding.validate` after page or resource changes and inspect its current status before running the Automation.
7. Confirm an existing mounted view refreshed, or call `Widget.show` when conversation/dock display is requested.

If `Widget.show` returns `no_surface_context`, the Widget remains valid; show it later from a conversation/dock context or place it on Canvas. Do not create a replacement Widget only to obtain a display surface.

## Responsive Size Contract

The same `index.html` renders across all surfaces:

- Free-layout Canvas placement policy: minimum `240x160`, default `420x320`, maximum `960x1440`.
- Canvas grid design spans: minimum `2x2`, default `5x8`, maximum `12x33`.
- Conversation width is commonly `420-680px`; inline height is content-driven and capped at `720px`.
- Fullscreen verification size is approximately `1120x820`.

Use width-based media or container queries. Keep `html` and `body` in normal document flow so conversation surfaces can measure content height. Verify at `240x160`, `420x320`, `960x1440`, and `1120x820`.

## Data Slot And Submit Event

Use `slots.main` only for Automation artifact delivery. Use `events.submit` for Widget input sent to the active Automation Binding.

```json
{
  "action": "update",
  "widgetId": "widget_generated",
  "slots": {
    "main": {
      "kind": "json",
      "schema": {
        "type": "object",
        "properties": { "summary": { "type": "string" } },
        "required": ["summary"]
      }
    }
  },
  "events": {
    "submit": {
      "schema": {
        "type": "object",
        "properties": { "query": { "type": "string" } },
        "required": ["query"]
      }
    }
  }
}
```

- Slot and event schemas must describe JSON objects.
- Choose schemas before writing the dynamic UI so sample data and live data share one render path.
- The Automation artifact schema must be compatible with `slots.main.schema`.
- `events.submit.schema` must be compatible with the Automation input contract.

Widget submit input resolves in this order:

```text
event payload -> Widget currentInput -> Automation defaultInput
```

## Common Widget Runtime

Author the shared `index.html` against the runtime surface common to conversation, dock, and Canvas placements. Do not depend on a field or listener that exists on only one host surface.

Before iframe JavaScript uses either global, read `references/runtime-api.md`; it defines all signatures and behavior.

Render current data on load and subscribe to updates:

```js
function render(data) {
  const main = data?.main ?? {};
  document.querySelector("[data-summary]").textContent = main.summary ?? "Waiting for data";
}

window.DaimonWidget.onDataChange(render);
```

`onDataChange` immediately supplies the current snapshot and returns an unsubscribe function.

The common Widget surface includes:

- `window.DaimonWidget.widgetId`
- `title`
- `data`
- `status`
- `inputState.currentInput`
- `theme`
- `tokens`
- `getToken`
- `onDataChange`
- `onStatusChange`
- `onThemeChange`
- `files`

Use `onStatusChange` for running and generic failure UI, and `onThemeChange` when JavaScript-rendered content needs theme updates. In callbacks, read the current property instead of depending on a host-specific callback payload:

```js
window.DaimonWidget.onStatusChange(() => {
  renderStatus(window.DaimonWidget.status);
});
```

Save draft input and emit a declared event:

```js
window.DaimonWidget.saveInput({ query });
window.DaimonWidget.emit("submit", { query });
```

Interactive Widgets with a declared event can use `saveInput` and `emit`. `saveInput` does not by itself commit `currentInput` or start a run. Always include the intended submit payload in `emit`, unless an already-successful submit established the `currentInput` you intentionally want to reuse.

The host validates the event payload, resolves the active Binding, validates Automation input, runs the Automation, and delivers fresh data back to `data.main`.

The shared iframe contract exposes only `inputState.currentInput`. Inspect full draft, submitted-input, validation-error, run, and error evidence outside the iframe with `Widget.read` and `Automation.readRun`. Common subscription APIs return an unsubscribe function; release listeners when the page no longer needs them.

Widget iframe code uses the host runtime APIs. Agent-facing `Automation`, `Binding`, `Widget`, and `Canvas` tools remain outside the iframe.

## Files

Use `window.DaimonWidget.files` for Widget file workflows. Before any file call, read
`references/runtime-api.md` for arguments, returns, cancellation, errors, and limits:

```js
const [inputFile] = await window.DaimonWidget.files.pick({
  accept: ".csv,text/csv",
  multiple: false
});
if (!inputFile) return; // pick resolves to [] when the user cancels

window.DaimonWidget.saveInput({ inputFile });
window.DaimonWidget.emit("submit", { inputFile });
```

The file API supports `pick`, `readText`, `readBytes`, `write`, `writeText`, `url`, and `download`. `window.DaimonCanvas.files` is a compatibility alias; check `window.DaimonCanvas.capabilities.files` when supporting an older runtime.

Pass only `FileResourceRef` values to Automation. Keep large file contents out of Widget input. Never submit browser `File`, `FileReader` contents, raw bytes, whole-file text, large base64, local paths, `file:` URLs, or Resource Store paths. Read output refs from `window.DaimonWidget.data.main.files`; use the runtime file API rather than fabricated download URLs.

## Canvas Placement State

`window.DaimonCanvas.viewState` and `setViewState` are Canvas-only. The Widget and its latest data are shared by every mounted view, while Canvas placement state is local to one `mountId`.

Inside a Canvas placement, use:

```js
const state = window.DaimonCanvas.viewState;
window.DaimonCanvas.setViewState({ ...state, selectedTab: "details" });
```

Use placement-local view state for tabs, filters, and presentation choices. Keep business data in Widget data delivered through Binding.

## Validation

Before reporting a Widget ready:

1. `Widget.validate` returns `ok: true` with `validation.status: "valid"`.
2. `index.html` is a complete document and renders at the requested size.
3. Static Widgets render on the requested conversation, dock, or Canvas surface.
4. Dynamic Widgets render schema-valid sample data before the first run.
5. Automation-backed Widgets have succeeded run and Binding delivery evidence before live data is reported ready.
6. After page or resource changes, an active Binding has been explicitly revalidated before the next run.

Failed, timed-out, or cancelled runs update status and error evidence without replacing the previous successful `latestData.main`.

## Runtime States

- `idle`/`running`: ready or active.
- `needs_input`: no payload, current input, or default satisfied the contract.
- `error`: show generic failure UI in the iframe; inspect `Widget.read`, the run, and Binding validation for details.
- `degraded`: keep usable data visible and inspect warning evidence.
- `cancelled`: keep the last successful data and allow explicit retry.

For interactive Widgets, render status without replacing the entire document. Disable duplicate submit actions while `running`, show field guidance for `needs_input`, and keep prior successful content visible for `error`, `degraded`, or `cancelled`.

## Update And Delete

- `Widget.update` changes title, description, slots, or events and revalidates related Bindings. Inspect `revalidatedBindings`.
- Workspace file changes refresh mounted views through resource revisions.
- `Widget.delete` removes the Widget, its Canvas placements, and its Binding edges.
- Use `cascadeAutomationIds` only for explicitly selected Automations dedicated to this Widget. Automations with other bindings, active runs, or external delivery remain separate.
- Inspect the delete response for removed placements, bindings, and Automations.
