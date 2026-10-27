# Desktop inline runtime v1

Use this for interactive content directly within desktop chat answers. Create a Widget, write
its `workspacePath/index.html`, validate it, then call Widget with
`{"action":"show","widgetId":"<returned id>","display":"inline"}`.

The HTML must be self-contained and at most 128,000 UTF-8 bytes. Use inline CSS/JS and SVG or
Canvas for charts. No CDN, fetch, imports, external images, filesystem or desktop APIs are available.
Only the desktop renderer supports this display mode. Other Widget surfaces retain their existing runtime.

`window.AceInteractive.state` supplies the last saved parameter object before page scripts run.
Initialize defaults with `{...defaults, ...window.AceInteractive.state}`.
`window.AceInteractive.setState(object)` replaces the saved parameter object and never calls the model.
Update it whenever inputs or computed parameters change. JSON state and control values together
must fit within 16,000 UTF-8 bytes. Values are saved per account, session and tool call on this device.
State contains user data; never save credentials. Persistence failures are displayed by the host.

Native input/select/textarea values with a stable unique `id` (preferred) or `name` also restore.
Use unique IDs for radio options; password/file fields are excluded. Use min/max/required and custom
validity for input validation. Use local button handlers for reset, tabs, filtering and sorting.

The host supplies a "用当前参数继续分析" button outside the isolated frame. It validates controls,
then submits a JSON snapshot of `parameters` and `controls` as a user message to the owning active
chat (queued if busy). Do not implement your own model call or submit postMessage; page code cannot
send chat messages automatically. The host button shows completion/failure and blocks duplicate
submissions until parameters change. Composer drafts and attachments are not consumed.

Include titles, units, assumptions, accessible labels and a text explanation alongside the chart.
Keep body in normal flow: the host measures height and caps it at 720px with internal scrolling.
Use responsive layouts for narrow chat windows. History contains the original HTML, so later
Widget edits or deletion do not change an earlier answer. Local input state is not synced to Web.
