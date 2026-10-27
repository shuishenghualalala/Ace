# Native desktop visuals v2

Call `Widget` with `{"action":"render","spec":{...}}`. It displays native charts
and forms directly before your final explanation. No HTML, JavaScript, files,
create/validate/show calls, browser preview, Canvas, Binding or Automation.
Do not use the HTML widgetdesign workflow for this mode.

## Contract

`spec` accepts only:
- `title`: short title, once only; `description`: optional assumptions/units (max 1000 chars).
- `parameters`: optional list, max 8. Each: `id` (ASCII letter followed by letters/digits/underscore, max 40), `label`, `type`, `value`, optional `unit`.
  - `type: "range" | "number"`: numeric value; required finite `min`, `max`, `step` (>0).
  - `type: "text"`: string value, max 1000 chars.
  - `type: "select"`: string value and `options` list of strings (1–20), value must be an option.
- `chart`: optional `{type:"line"|"bar", x:number[], xLabel:string, yLabel:string, series:[], showTotals?:boolean}`. x contains 1–120 strictly increasing numbers. Max 4 series.
  - Each series: unique `id`, `label`, and either `values:number[]` (one per x) OR `expression` (evaluated for each x).
  - Optional `cumulative:true` accumulates the point values; optional `initial` expression is added once before the first point. Without cumulative, initial is not allowed.
  - `showTotals:true` displays the last point of each series as compact values, only when useful.
- `action`: optional `{label, instruction}`. On explicit user click, the host sends instruction plus a readable snapshot of ALL labelled parameter values to this chat. Requires parameters. Do not add it to a static chart.

Provide a chart, or parameters plus an action. Payload <=64KB. Numeric values and
computed results must stay finite within ±1e12. Unknown fields are rejected.

Expressions are numeric literals or one of these JSON objects:
`{"param":"numeric_parameter_id"}`, `{"x":true}`, `{"op":"add|sub|mul|div|pow|min|max","args":[expression,expression]}`.
Exactly two arguments; maximum depth 12. No strings containing formulas, scripts,
property access or functions. Text/select values are passed to the follow-up but
cannot be used as numeric operands. Changing controls recomputes locally, never
calls the model. Division by zero/overflow shows an error instead of invalid data.

Example static comparison:
```json
{"action":"render","spec":{"title":"季度销量","chart":{"type":"bar","x":[1,2,3,4],"xLabel":"季度","yLabel":"件","series":[{"id":"a","label":"方案 A","values":[20,30,25,45]},{"id":"b","label":"方案 B","values":[15,28,32,40]}]}}}
```

Example local cumulative calculation (general arithmetic, not a fixed business template):
```json
{"action":"render","spec":{"title":"12 个月现金流","description":"收入固定；累计现金流包含一次性投入。","parameters":[{"id":"income","label":"月收入","type":"number","value":8000,"min":0,"max":100000,"step":100,"unit":"元"},{"id":"cost","label":"月成本","type":"range","value":6000,"min":0,"max":20000,"step":100,"unit":"元"},{"id":"initial","label":"初始投入","type":"number","value":3000,"min":0,"max":100000,"step":100,"unit":"元"}],"chart":{"type":"line","x":[1,2,3,4,5,6,7,8,9,10,11,12],"xLabel":"月","yLabel":"累计现金流（元）","showTotals":true,"series":[{"id":"base","label":"基准","expression":{"op":"sub","args":[{"param":"income"},{"param":"cost"}]},"cumulative":true,"initial":{"op":"sub","args":[0,{"param":"initial"}]}}]},"action":{"label":"用这些参数继续分析","instruction":"请根据当前参数分析现金流与回本时间。"}}}
```

For growth, use income × factor × pow(1 + growth, x - 1), minus cost;
express it using the operators above. Use only supplied or explicitly disclosed
assumptions. If generating multiple scenarios, keep the same x and units.

The host supplies theme, chart axes, legend toggles, pointer/keyboard inspection,
validation and per-device/per-account/per-session/per-tool-call state. Values
remain intact when the answer streams and when the session reopens. Each render
call is a new immutable answer block: call once per intended result, not once per
series or to repeatedly preview. For requested revisions, render a new version
in the new turn and briefly say what changed; old answers remain snapshots.

After render, provide a concise interpretation. Do not repeat the chart title or
all its values in another table. Do not describe implementation files or testing
steps to the user. Unsupported calculations should be explained in ordinary text
rather than silently creating an HTML project.
