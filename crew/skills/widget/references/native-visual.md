# Structured desktop replies v3

Use `Widget({"action":"render","spec":{...}})` for charts, comparison cards,
parameter exploration, forms and data tables inside an answer. Read this contract
before rendering. Do not create HTML/JS, files, Widget assets, browser previews,
Canvas, Binding or Automation for these ordinary reply interactions.

## Envelope and composition

`spec` requires `version:3`, `id` (stable ID for this result), `revision` (integer,
start at 1), `title`, `children` (component array). Optional `description`,
`parameters`, `datasets`. Only documented fields are accepted. Max 128 KB,
120 components, 6 nested layout levels, 16 parameters, 8 datasets. IDs start with
an ASCII letter and contain letters/digits/underscore, <=40 characters; no
constructor/prototype/__proto__. Component IDs must be unique in the reply.

Every component requires `id` and `type`:
- `row`, `column`, `card`: `children`, optional `title`. Rows wrap responsively.
- `text`: `text` (plain text, <=4000 chars).
- `badge`: `text`, optional `tone:neutral|success|warning|danger`.
- `image`: `src` (absolute local PNG/JPEG/WebP/GIF path, or raster data URL), `alt`.
  Use existing images only. External URLs and SVG are not accepted.
- `metric`: `label`, `expression`, optional `unit`.
- `field`: `parameter` (parameter ID). Each parameter must have exactly one field.
- `chart`: `dataset`, `chartType`, `x` (column ID), `series:[{column,label?}]`,
  optional `title`, `xLabel`, `yLabel`. Types: line, bar, area, stackedBar,
  stackedArea, scatter, pie, donut. Max 8 series; pie/donut exactly one numeric
  series, nonnegative values with positive total. Scatter requires numeric x.
  Number/string/date x columns produce numeric/category/time axes respectively.
- `table`: `dataset`, optional `title`, `pageSize` (5–50, default 10).
  The host provides local sorting, filtering and paging automatically.
- `button`: `label`, `action` (see below). No arbitrary URLs or tool execution.

## Parameters and actions

Each parameter: `id`, `label`, `type`, `value`; optional `required`, `unit`.
- range/number: require finite min < max, step > 0; value must align to step from min.
- text/textarea: string <=1000 chars; empty string allowed until required submission.
- date: YYYY-MM-DD or empty string.
- checkbox: boolean (required means it must be checked before submitting).
- select/radio: string value; `options` is 1–20 distinct strings.
- multiselect: string-array value; `options` is 1–20 distinct strings.

Actions:
- `{"type":"set","values":{"parameterId":value}}`: immediately updates selected
  parameters, fields, metrics, charts and tables locally. Use for choosing a plan.
- `{"type":"reset"}`: restores all parameter defaults.
- `{"type":"submit","instruction":"Explain the chosen plan"}`: explicit user
  click sends instruction + labelled parameter values into the same conversation.
  The host validates required inputs, prevents duplicate submits, shows progress,
  supports retry and records successful submission. No model call for local actions.

## Datasets and bounded calculations

Dataset: `id`, `columns`, `rows` (1–500 rows). Column: `id`, `label`,
`type:number|string|date`; optional `expression`, `cumulative`, `initial`.
Rows contain values for ONLY the columns without an expression, in column order.
Computed columns are numeric. Charts and tables referencing the same dataset
always use the same computed snapshot.

Expressions: numeric constant, `{"param":"numeric_parameter_id"}`,
`{"column":"raw_numeric_column_id"}`, or
`{"op":"add|sub|mul|div|pow|min|max","args":[expr,expr]}`.
Exactly two operands, max depth 12. No executable strings, scripts, functions or
references to other computed columns. A metric can reference parameters only.
`cumulative:true` sums a column over row order, with optional `initial` expression
(default 0). Numeric inputs/results are finite within ±1e12. Calculation errors
show guidance; invalid values are never plotted or submitted.

## Complete example: choose a plan, calculate locally, submit

```json
{"action":"render","spec":{"version":3,"id":"revenue","revision":1,"title":"收入方案","description":"示例假设：固定月成本 6000 元。","parameters":[{"id":"income","label":"月收入","type":"range","value":8000,"min":0,"max":20000,"step":100,"unit":"元"}],"datasets":[{"id":"cash","columns":[{"id":"month","label":"月份","type":"number"},{"id":"net","label":"累计结余","type":"number","expression":{"op":"sub","args":[{"param":"income"},6000]},"cumulative":true,"initial":-3000}],"rows":[[1],[2],[3],[4],[5],[6]]}],"children":[{"id":"plans","type":"row","children":[{"id":"basic","type":"card","title":"基准","children":[{"id":"basicText","type":"text","text":"每月收入 8000 元"},{"id":"basicPick","type":"button","label":"选择基准","action":{"type":"set","values":{"income":8000}}}]},{"id":"growth","type":"card","title":"增长","children":[{"id":"growthText","type":"text","text":"每月收入 12000 元"},{"id":"growthPick","type":"button","label":"选择增长","action":{"type":"set","values":{"income":12000}}}]}]},{"id":"incomeField","type":"field","parameter":"income"},{"id":"plot","type":"chart","dataset":"cash","chartType":"area","x":"month","yLabel":"元","series":[{"column":"net"}]},{"id":"details","type":"table","dataset":"cash","pageSize":5},{"id":"analyze","type":"button","label":"按当前参数分析","action":{"type":"submit","instruction":"分析当前方案的回本时间与风险。"}}]}}
```

Static categorical example: dataset columns `[{id:"name",label:"方案",type:"string"},
{id:"sales",label:"销量",type:"number"}]`, rows `[["甲",20],["乙",35],["丙",45]]`;
chart uses `x:"name",series:[{column:"sales"}]` and `chartType:"donut"` or `"bar"`.
No parameters/fields/buttons are needed for a static chart.

## Revisions and response quality

Use a new id for an independent result. To revise the same result, reuse its id
and increment revision, providing the complete spec. Within a turn the host
updates that component; previous turns remain read-only snapshots with their
revision. State is isolated by account/session/component/revision. Restarting
restores valid inputs, table views and successful submit state on this device.

Call render once per intended result, not once per series or for previews. Do not
render a gallery unless asked; choose a few useful components. After rendering,
briefly interpret the result rather than repeat its title and every value.
No implementation/testing details in the user-facing answer. Do not silently
switch unsupported requests into HTML projects.
