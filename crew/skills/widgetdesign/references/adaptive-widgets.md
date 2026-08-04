# Adaptive Widget Behavior

Use this file when a widget can be resized, placed on a canvas, opened as a full result, or reused
across compact and expanded surfaces. The model is close to Apple-style widgets: smaller surfaces
show the essence; larger surfaces reveal structure and controls.

<!-- preview-rule: adaptive.tiers | section: Adaptive behavior | specimen: adaptive-tiers | coverage: covered | priority: core -->
## Semantic disclosure tiers

Design three tiers before writing markup.

| Tier | Goal | Content rule |
|---|---|---|
| Compact | answer at a glance | one main value/relationship, one short label, no secondary controls |
| Regular | usable structure | primary visual plus 2-4 supporting facts, direct labels, essential controls |
| Expanded | inspection | full structure, secondary details, caveats, comparison rows, or audit notes |

The tier is not just responsive wrapping. It changes information depth. Small does not mean
cramped; it means edited. Semantic disclosure tiers and physical width bands are distinct.

Do not solve size pressure by making everything smaller. Font size may step down within the product
scale, but semantic adaptation comes first: summarize, abbreviate, reorder, collapse repeated rows
into a count, turn prose into marks, or move evidence to expanded/fullscreen.

Choose the tier from **width, height, and content density together**. The mapping from width bands to
Compact / Regular / Expanded disclosure is explicitly non-one-to-one: a wide-width but short or
dense placement may use Compact or Regular, while a sparse standard-width placement may support
Expanded. Downgrade disclosure, reduce repeated examples, or move inspection detail to fullscreen.
Do not use hidden overflow, a scroll pane, an accordion, or clipped rows as the adaptation strategy.

Keep one dominant contract across tiers. Compact, regular, and expanded versions should look like
the same widget at different disclosure depths, not three different component designs. Expanded may
add rows, labels, controls, caveats, or inspection evidence, but it should not switch from an answer
card into a dashboard, from a matrix into option cards, or from a timeline into a step-card collage.

Texture follows the same contract. Compact keeps at most one high-value texture or motif carrier;
regular may show one local texture system; expanded may extend that same system with coordinates,
evidence density, or a resolve trail. Remove background fields, material grain, secondary ticks,
and long ASCII before reducing useful content.

<!-- preview-rule: adaptive.disclosure | section: Adaptive behavior | specimen: adaptive-tiers | coverage: covered | priority: core -->
## Disclosure order

Reveal content in this order:

1. Primary answer, selected option, current state, or main relationship.
2. Visual structure that proves the answer: bar, axis, row, node, matrix, route, or control output.
3. Short supporting facts and direct labels.
4. Controls, if interaction is essential.
5. Expanded-only evidence, caveats, secondary rows, or audit notes.

Do not hide the core visual in compact mode. Hide secondary explanation first.

<!-- preview-rule: adaptive.density-budget | section: Adaptive behavior | specimen: adaptive-tiers | coverage: covered | priority: core -->
## Density budget

Treat every host-sized widget as having a visible budget. Count before rendering.

| Item | Compact budget | Regular budget | Expanded budget |
|---|---:|---:|---:|
| Primary values / answers | 1 | 1-2 | 1-3 |
| Rows, nodes, or steps | 1-3 | 3-5 | 6-10 |
| Controls | 0-1 | 1-3 | 1-5 |
| Repeated examples | 1-2 | 2-4 | 4-8 |
| Support facts | 0-1 | 2-4 | 4-8 |
| Chart direct labels | 1-2 | 2-4 | 4-8, only with lanes |

These are not quotas. They are clipping guards. If the content needs more than the budget, edit the
visible state and put inspection detail behind fullscreen, drill-in, or the prose response.

For host-sized widgets, the visible budget is a hard fit contract: every semantic row, card, label,
control, and status mark that is rendered in the current tier must be fully visible. `overflow:
hidden` may crop chart ink, avatar masks, or ellipsis text, but it must never hide meaningful widget
content. Vertical scrolling is allowed only when the widget is explicitly an editor, terminal, log,
or large table.

When content is too long for the current tier, convert it:

| Overfull content | Compact conversion | Regular conversion | Expanded conversion |
|---|---|---|---|
| Long title | 2-4 word result label | short title + one support line | full title if it still wraps cleanly |
| Paragraph explanation | one visual mark or state | 2-3 short facts | evidence rows, not prose blocks |
| Dense chart labels | primary direct label only | staggered short labels | full direct labels with lanes |
| Many examples | count + representative item | 2-4 representative items | more examples only if rows fit |
| Controls | one essential control or none | controls adjacent to output | secondary controls after primary output |

<!-- preview-rule: adaptive.text-fit | section: Adaptive behavior | specimen: adaptive-tiers | coverage: covered | priority: core -->
## Text fit contract

Use this decision order for every title, value, label, explanation, control, chart annotation, and
identifier. **Edit before wrapping**; wrap before truncating; change structure before shrinking type.

1. **Edit meaning:** remove filler, keep the noun/value/action, and use a shorter domain-correct label.
2. **Convert form:** turn prose into a value, mark, row, state, count, axis, or representative item.
3. **Reorder:** place the answer first and move evidence to regular/expanded disclosure.
4. **Wrap deliberately:** allow a known line count only when all lines fit the density budget.
5. **Ellipsize selectively:** use one-line ellipsis only for a secondary label when the visible
   truncated form still retains its meaning and the full text remains available accessibly. Primary,
   control, and direct labels cannot ellipsize.
6. **Downgrade disclosure:** remove secondary rows or examples when the complete semantic unit
   cannot fit. Never crop half a row, control, value, or sentence.

Role rules:

- Primary values must never ellipsize, clip, or break across lines. Primary labels also cannot
  ellipsize or clip. Shorten the label or unit, or change the value presentation while preserving
  exact data.
- Titles may wrap to two lines only when the tier budget reserves both lines. Primary titles cannot
  ellipsize; edit compact titles into a complete 2-4 word result label. A secondary title label may
  ellipsize only under the selective rule above.
- Control labels and selected values must remain fully readable and cannot ellipsize. Replace long
  option prose with a short label plus an adjacent differentiator.
- Direct chart labels, targets, baselines, and status labels need dedicated lanes or anchors and
  cannot ellipsize. Never rely on hover to reveal a clipped label.
- CJK prose uses natural line breaks and a bounded line count. Do not add negative letter spacing or
  compress glyphs to force fit.
- Long URLs, hashes, paths, or unbroken identifiers may use `overflow-wrap: anywhere`; ordinary
  words and numeric values may not.
- Apply `min-width: 0` to grid/flex children. `overflow: hidden` is allowed for chart ink, masks, or
  a deliberate secondary-label ellipsis box that meets the selective rule above, never for a
  semantic container.

After rendering, inspect every semantic element boundary. Passing page-level scroll dimensions is
not enough if a child label, value, button, or row clips internally or overlaps a sibling.

## Texture disclosure

Texture follows information density, not viewport decoration:

| Tier | Keep | Remove first |
|---|---|---|
| Compact | one mark, short patterned rail, or local state carrier | background fields, grain, secondary ticks, long ASCII |
| Regular | one complete local texture system | duplicate motifs and nonessential annotations |
| Expanded | richer evidence, coordinates, or resolve trail | any pattern crossing labels or competing with the primary structure |

Across all tiers, texture never enters a label-safe zone, never causes horizontal overflow, and
never replaces a direct label. Reduce motif frequency and opacity before reducing text contrast.

<!-- preview-rule: adaptive.archetype-rules | section: Adaptive behavior | specimen: layout-archetypes | coverage: covered | priority: core -->
## Archetype adaptation

| Archetype | Compact | Regular | Expanded |
|---|---|---|---|
| Answer card | value + label | support facts | confidence, caveat, next action |
| Comparison matrix | one winner/exception statement; optional short decisive delta | 3-5 aligned criteria | full criteria and rationale rows |
| Data instrument | one chart mark or ranked top item | direct labels and baseline | reference lines, caveats, secondary series |
| Choice elicitor | current recommended choice | comparable choices | differentiators and natural prompt action |
| Control + output | output value | essential controls beside output | breakdown and sensitivity notes |
| Timeline / process | current/next state | axis or lane with key steps | blockers, owners, state details |
| Relationship map | core dependency | visible nodes and labeled links | groups, secondary links, failure paths |

<!-- preview-rule: adaptive.canvas-contract | section: Runtime boundary | specimen: runtime-boundary | coverage: covered | priority: core -->
## Canvas contract

Widgets may be embedded in a Daimon Canvas-like surface. They must tolerate resize, opening as a
full result, and state hydration.

- Use stable dimensions for boards, grids, tools, and counters.
- Use CSS container behavior, `data-size-tier`, or host-provided size state when available.
- Daimon provides the measured iframe tier as `html[data-daimon-size-tier]`: `compact` when the
  viewport is below `300px` wide or `220px` high, `regular` for the normal host range, and
  `expanded` at roughly `760 x 500` or larger. Treat this as the host's starting disclosure signal,
  not as permission to overflow; re-evaluate after content and safe-zone geometry are known.
- Mark semantic regions with `data-kimi-priority="p0|p1|p2|p3"` (or an equivalent explicit tier
  branch) so Compact can omit P1-P3 and reflow the remaining P0 group. Do not leave all tiers in one
  vertical DOM stack and expect the host scrollbar to become the adaptation mechanism.
- The canonical `assets/styles/kimi-fit.css` may implement this omission mechanically. Use it only
  with correctly marked regions; never mark a required P0 label, unit, safety stop, or control as P1
  merely to make a screenshot fit.
- Keep controls near the output at every size.
- Preserve selected state and local inputs when the host re-renders.
- Avoid sidebars inside widgets; the canvas or conversation is already the surrounding workspace.

## Surface parity and placement-first adaptation

Conversation preview, Canvas, fullscreen, and pin surfaces use the same Widget document, data
contract, and semantic hierarchy. A surface may reveal a different disclosure tier because its
measured iframe size differs, but it must not silently become a different authored composition.

Before writing markup:

1. Measure the actual Widget content box in CSS pixels.
2. Apply the Daimon host-control safe zone without treating it as a new layout mode.
3. Select the smallest tier that preserves the complete user job.
4. Select a Canvas placement that can contain that tier.
5. Define responsive layout only after the tier and placement contract are explicit.

The actual Canvas placement must be large enough for the promised default tier. A Widget that needs
P0 plus status, transfer, controls, or supporting facts must use at least the `5 x 8` reference
placement unless its Compact composition is explicitly designed and verified. Do not use the visual
size of a zoomed Canvas screenshot as evidence of the iframe's CSS size; use the iframe's measured
`clientWidth` and `clientHeight`.

When a smaller host forces Compact, keep the P0 group complete, retain one direct supporting
relationship when it is necessary to understand P0, recompose the layout into the available space,
and close all vacated space after removing secondary content. Do not hide a semantic region with
`display: none` unless the Compact contract explicitly names that region as omitted and the remaining
layout has been reflowed and visually verified.

The host safe zone is not a disclosure tier and does not change the disclosure tier by itself. It only
changes the first-row geometry. Move a title or P0 below the safe zone when necessary; do not remove
P1 content merely because host controls occupy the top-right corner.

The same Widget must preserve semantic parity across surfaces. Conversation may show P0, P1, and P2
when its measured height allows; Canvas may show Compact, Regular, or Expanded based on measured size;
Canvas must never lose required P0 meaning or leave a visually empty shell after reducing disclosure.

Do not use `100vh`, `min-height: 100vh`, viewport-sized spacer regions, or top-level `overflow: clip`
or `overflow: hidden` to fill or mask a host-sized frame. If height queries are needed, use a
dedicated outer size-query container and keep the content root in normal document flow.

<!-- preview-rule: adaptive.daimon-frame | section: Runtime boundary | specimen: runtime-boundary | coverage: covered | priority: core -->
## Daimon host frame

Daimon owns the outer frame. The same `index.html` can appear in chat, Canvas, fullscreen, a pin
window, or a Blueprint preview card. The widget should render as an interior thinking surface.

- Preserve `html` / `body` as transparent or inherited by default.
- Do not draw a second full-bleed card, browser window, or app shell to replace the host frame.
- Use widget-owned containers only for real content boundaries: chart frames, repeated items,
  selectable options, control groups, and confirmation states.
- Treat host hover/active/focus chrome as external. Inside the widget, show focus only on actual
  buttons, inputs, choices, or other keyboard targets.
- Apply the tokenized top-right host-control safe zone owned by `daimon-runtime-integration.md` on
  conversation and Canvas surfaces. Keep titles, P0, labels, legends, controls, and pointer-critical
  visuals outside it while allowing nonessential background material to continue behind it.
- At narrow or short placements, move the title or P0 below the safe zone when the remaining first
  row cannot hold the complete semantic group. Do not compress primary content beside host chrome.
- Expect preview cards to scale and clip the iframe. Keep the first read near the top-left content
  flow and avoid relying on an outer border to define the widget.

<!-- preview-rule: adaptive.daimon-grid | section: Adaptive behavior | specimen: adaptive-tiers | coverage: covered | priority: core -->
## Daimon placement grid

Use the widget's own iframe viewport for layout decisions. Canvas grid placements are roughly:

| Placement | Design size | Disclosure expectation |
|---|---:|---|
| Minimum | 240 x 180 | micro-width layout; usually Compact disclosure |
| Default | 420 x 320 | narrow-width layout; Compact or Regular disclosure by density |
| Large Canvas | 960 x 720 | wide-width layout; Regular or Expanded disclosure by density |
| Fullscreen | 1120 x 820 | wide-width layout; inspection detail only when it remains useful |

Grid mode uses 12 columns. One column is about 80px wide, the horizontal step is about 92px
including the gap, one row is 32px high, and the vertical step is about 44px including the gap.
Useful placement widths are about 3 columns / 264px, 5 columns / 448px, 8 columns / 724px, and
12 columns / 1092px.

Recommended breakpoints:

| Viewport width | Layout behavior |
|---:|---|
| `<300px` | micro-width: one column; title, primary metric, and one note; collapse tables to a value or sparkline |
| `300-519px` | narrow-width: one column; small metric grids may use two columns; edit visible rows to fit |
| `520-759px` | standard-width: two-column layouts and fuller labels when height and density permit |
| `>=760px` | wide-width: main/detail layouts, side-by-side charts, tables, or relationship-map detail when useful |

Width bands select layout opportunities, not semantic disclosure. Determine Compact / Regular /
Expanded separately from the available width, available height, and actual content density.

Use top-level `@media (min-width: ...)` breakpoints first, then component `@container` queries for
reusable parts. Put `min-width: 0` on grid/flex children. Ellipsis remains limited to secondary
labels whose visible truncated form retains meaning and whose full text remains available
accessibly; primary, control, and direct labels cannot ellipsize.

<!-- preview-rule: adaptive.height-model | section: Runtime boundary | specimen: runtime-boundary | coverage: covered | priority: core -->
## Height and scrolling

Daimon surfaces have different height models, so do not force a single full-screen app layout.

- Chat inline is content-driven: normal document flow lets the host measure `scrollHeight` and grow
  the iframe up to its cap.
- Canvas, fullscreen, and pin windows are host-sized: the iframe height is fixed by the placement or
  window.
- Do not set `height: 100vh`, `min-height: 100vh`, locked `html/body` height, or blank spacer
  height just to fill the frame.
- For fixed-header patterns, use `position: sticky` in normal document flow. Avoid nested scroll
  regions unless the widget is explicitly an editor, terminal, log, or large table.
- Reserve space for host chrome, hover controls, status pills, and resize handles. Widget content
  must not depend on the covered bottom-right corner being readable.
- In host-sized widgets, do not use vertical `overflow: auto`, `overflow: scroll`, hidden overflow,
  `<details>`, accordions, or collapsible panes to disguise overfull content. Edit disclosure
  instead. An explicit editor, terminal, log, or large-table job may use a bounded vertical scroll
  region because scrolling is intrinsic to that job. Never let a row, card, label, or control
  disappear halfway behind the frame.
- A non-editor Compact Widget must have `html[data-daimon-size-tier="compact"]` fit with
  `scrollHeight <= clientHeight + 1`; this is a hard release condition, not a visual preference.
  If it fails, remove P2/P3 first, then P1, then nonessential Grid/Pixel/Glass density, and close
  the vacated space. Never fix the failure by adding a scroll region or by shrinking every font.
- Do not use viewport-unit font sizes. The Canvas board may be zoomed; CSS pixels inside the iframe
  are design pixels. Respond to container width and breakpoint px instead.

<!-- preview-rule: adaptive.verification | section: Preflight checklist | specimen: pressure-tests | coverage: covered | priority: core -->
## Resize verification matrix

Before shipping a Canvas-capable widget, verify these sizes:

| Size | Must pass |
|---:|---|
| 240 x 180 | micro-width layout; no horizontal scroll; title and primary relationship visible |
| 420 x 320 | narrow-width layout; primary action and complete rows fit after edited disclosure |
| 960 x 720 | wide-width layout uses available columns without cramped labels |
| 1120 x 820 | wide-width fullscreen reveals useful inspection detail without dead empty frame |

At every size, `scrollWidth <= clientWidth + 1`, key content remains visible, and pointer targets are
at least 28px. Edit disclosure before text overflows. Only secondary labels with retained visible
meaning may ellipsize; primary, control, and direct labels cannot ellipsize.
For fixed-height Canvas, also verify `scrollHeight <= clientHeight + 1` by default. The only
exceptions are explicit editor, terminal, log, or large-table widgets; if the check fails for any
other widget, reduce disclosure or open the inspection detail in fullscreen.

<!-- preview-rule: adaptive.theme-verification | section: Preflight checklist | specimen: pressure-tests | coverage: covered | priority: core -->
## Light and dark verification

Run the four-size matrix in both Light and Dark. At each state verify:

1. Primary, secondary, tertiary, and disabled text preserve their hierarchy and meet readable contrast.
2. Kimi Blue and semantic status colors carry the same meaning without becoming full-surface fills.
3. Borders and surfaces remain distinguishable without creating nested-card noise.
4. Icons inherit `currentColor`; no icon disappears or changes meaning.
5. Texture stays visible but subordinate, never crosses a label-safe zone, and does not reverse its data meaning.
6. Focus, hover, selected, loading, success, warning, error, empty, and disabled states preserve layout.

Theme verification must use runtime token switching, not a separate CSS media-query simulation.

<!-- preview-rule: adaptive.failures | section: Preflight checklist | specimen: pressure-tests | coverage: covered | priority: core -->
## Adaptive failure signals

- Compact mode is a shrunken desktop widget with clipped text.
- A non-editor/log/terminal/large-table widget passes by adding a scroll pane, `overflow: hidden`,
  `<details>`, or an accordion instead of reducing visible content.
- Expanded mode only adds prose, not more structure.
- Controls move away from the output they affect.
- A widget needs a sidebar to explain itself.
- Text wraps unpredictably and changes the visual answer.
- The largest state looks like a dashboard collage instead of a clearer version of the same job.
- The widget draws a second outer shell that competes with the Daimon host frame.
- The layout depends on `100vh`, viewport-unit type, or physical screen width instead of the iframe
  or component container.
