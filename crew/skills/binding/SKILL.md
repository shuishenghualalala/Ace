---
name: binding
description: Use when creating, reading, validating, repairing, deleting, or troubleshooting Daimon Blueprint automation_widget Bindings between Automation artifact results and Widget data slots and submit events.
---

# Binding

Binding is the durable connection between an Automation and a Widget:

```text
Automation artifact -> Widget.slots.main
Widget.events.submit -> Automation input
```

`Binding.create` validates and attaches the connection. It does not run the Automation or display the Widget.

## Create

Before creating:

1. Automation uses `result.kind: "artifact"` with an object schema.
2. Widget declares `slots.main.kind: "json"` with an object schema compatible with the Automation artifact.
3. If Widget declares `events.submit`, its schema is compatible with the Automation input contract.
4. Widget `index.html` exists and validates.

```json
{
  "action": "create",
  "kind": "automation_widget",
  "automationId": "automation_generated",
  "widgetId": "widget_generated"
}
```

A Widget has one active main Binding at `Widget.bindings.main`. Creating another Binding for the same Widget makes the new Binding active. The previous Binding becomes `invalid` with a `superseded` validation issue, and its deliveries cannot overwrite `Widget.latestData.main`.

## Status

- `pending_run`: schemas and render checks pass, but no current artifact has been delivered through this Binding.
- `valid`: contracts pass and current artifact/render evidence is valid.
- `invalid`: one or more validation issues require repair.

Binding artifact validation uses `Automation.latestArtifactRunId`.

## Automatic Revalidation

`Automation.update` and `Widget.update` revalidate related Bindings. Inspect `revalidatedBindings` in the update response before running or enabling automatic work.

Writing `index.html` or another Widget workspace resource does not call `Widget.update`, so it does not automatically revalidate the active Binding. After page or resource changes, call `Widget.validate`, then call `Binding.validate` for `Widget.bindings.main` before running the Automation.

Common changes that require attention:

- Automation input schema changes affect `events.submit` compatibility.
- Automation result schema changes affect `slots.main` compatibility.
- Widget slot or event schema changes affect both directions.
- Widget page changes can affect render validation.

Repair the asset that owns the invalid contract, then call `Binding.validate`.

## Delivery Readiness

Before reporting an Automation-backed Widget ready:

1. `Binding.read` or `Binding.validate` returns `ok: true`.
2. The Binding is `valid`, or a `pending_run` Binding receives a fresh run.
3. `Automation.readRun` shows terminal `status: "succeeded"`.
4. The run contains a `deliveryResults` row for this `bindingId` with `status: "succeeded"`, the expected `widgetId`, and `slot: "main"`.
5. `Widget.read` shows the expected `latestData.main`, a non-error status, and matching `lastRun`.

Execution success and delivery success are separate. If delivery fails or is skipped, inspect the Binding issue, repair the owning Automation or Widget contract, validate again, run again, and re-check Widget state.

## Ownership

- Automation owns input, execution, result schema, runs, files, and delivery.
- Widget owns `index.html`, `slots.main`, `events.submit`, input state, status, and latest data.
- Binding owns the durable edge and validation result.
- Canvas owns placement and receives only `widgetId`.

Show or place the connected Widget by `widgetId`. Keep `bindingId` for Binding read, validate, delivery verification, and delete operations.

## Repair Issues

- `result_not_bindable`: use an Automation artifact result.
- `schema_mismatch`: align Automation result schema with Widget main slot schema.
- `event_input_mismatch`: align Widget submit schema with Automation input.
- `render_smoke_failed`: repair `index.html` or its resources, call `Widget.validate`, then explicitly call `Binding.validate` for the active Binding.
- `render_smoke_timeout`: warning-only; retry validation and inspect the page, but do not treat this warning alone as an invalid Binding.
- `superseded`: read `Widget.bindings.main` and use the active Binding.
- `binding_not_active`: delivery used a stale edge; read `Widget.bindings.main` and use the active Binding.
- `binding_not_deliverable`: inspect Binding status and validation issues, repair the owning Automation or Widget contract, validate, and run again.
- `not_found`: recreate or select existing endpoints.
- `validation_failed`: inspect issue paths and details returned by Binding validation.

## List And Delete

- `Binding.list` returns paged results. Use `automationId` or `widgetId` filters, then follow `data.page.nextOffset` when needed.
- `Binding.delete` removes the durable edge and detaches it from the Automation and Widget.
- Deleting a Binding does not delete either endpoint or any Canvas placement.
- After delete, a Widget that depended on the Binding retains its definition and last stored data but no longer receives Automation delivery or routes submit through that edge.
