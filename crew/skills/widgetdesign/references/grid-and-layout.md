# Grid and Layout

This reference owns the visible grid policy inside a Kimi widget. Use `composition-system.md` for
columns, modules, baselines, shared variables, and optical alignment.

This is the sole canonical owner of exact Construction Grid tier counts, sampled row intervals, and
final rendered-strength targets. Other references route here instead of copying those constants.

<!-- preview-rule: vnext.visible-grid-gate | section: Layout patterns | specimen: grid-system | coverage: covered | priority: core -->
## Canvas versus widget grid

The Daimon host Canvas stays **blank** or uses its neutral runtime background token. Never add a
Canvas-wide graph-paper field, dot matrix, baseline, or permanent grid. Construction, semantic, and
Pixel systems stop at each widget boundary and must not masquerade as host chrome.

Canvas-wide family likeness comes from widgets sharing the grammar below inside their own bounds,
not from a global overlay. Multi-widget Anchor Mosaic curation remains an evaluation concern.

Object-led mode is exempt from the default visible Construction Grid. Use an object-native grid,
viewfinder, frame, seam, scale, or no visible grid according to the researched visual language. Do not
add the square Kimi field merely for family resemblance. Default Kimi mode follows the rules below.

## Construction grid

**Construction Grid as a Family Invariant** means a low-contrast visible grid may carry legitimate
Kimi brand structure even when it does not encode a datum. It communicates modular composition,
alignment, build orientation, and inspectability. Derive it from the real content box, columns,
modules, rows, and baseline; never draw a second decorative grid.

The 4px/8px baseline rhythm remains implicit by default: use it to place type, spacing, rules, and
marks, but do not paint every baseline across the widget. The visible family field uses square-cell
sampling: one cell variable controls both axes, and the horizontal interval must equal the vertical
interval; horizontal rows remain lighter than vertical module divisions, but never closer together.
The eye should register the answer first, the composition second, and the grid only after that.

Keep these three systems distinct:

| System | Job | Allowance |
|---|---|---|
| construction grid | visible Kimi family structure derived from real layout variables | clipped to the widget boundary; separate from semantic and Pixel systems |
| semantic grid | labeled coordinate grid for chart, map, process, scale, route, lane, or relationship meaning | does not consume the Pixel field allowance |
| Pixel field | one abstract or contextual Kimi signature region; a pixel-density field placed within coordinates is still a Pixel field | consumes the one Pixel field allowance |

A construction grid does not need chart semantics. A labeled coordinate grid is a semantic grid and
needs readable anchors, but it does not itself consume the Pixel allowance. A pixel-density field
placed within those coordinates is the one Pixel field and does consume the allowance. Pixel roles,
expression routing, and area limits belong to `brand-texture-language.md`.

Build the grid in this order:

1. Define one content box from runtime spacing tokens.
2. Choose the fewest columns and rows that express the dominant archetype.
3. Place real content on shared column lines and the implicit 4px/8px baseline rhythm.
4. Derive sparse visible module rows and column divisions from those same values.
5. Reserve one region for the dominant visual moment and space for the one routed ASCII/Pixel field.
6. Keep controls adjacent to their output and direct labels adjacent to their marks.

Whitespace remains an active module. Visible structure must support the reading order rather than
fill unused space.

## Grid by size

| Tier | Default behavior | Construction rule |
|---|---|---|
| Compact | partial or removable | At most one nonsemantic grid fragment after the P0 allowlist in `adaptive-semantic-fit.md`; Pixel treatment follows `brand-texture-language.md`. |
| Regular | visible by default | 2-4 logical content columns aligned to 48px square-cell sampling in both axes. |
| Expanded | visible by default | 4-8 logical content columns aligned to 56px square-cell sampling in both axes; evidence bands or semantic coordinates appear only when content supports them. |

Use separate rendered strengths for vertical modules and horizontal rows:

| Theme / tier | Vertical modules | Horizontal rows |
|---|---:|---:|
| Light Regular | 4% | 2% |
| Light Expanded | 5% | 2.5% |
| Dark Regular | 5% | 2.5% |
| Dark Expanded | 6% | 3% |

Compact may remove the Construction Grid entirely.

The Grid should be perceived after the content structure, not before it. At first glance it unifies
the field; on inspection it reveals alignment. If it reads as graph paper or competes with Pixel,
type, charts, or controls, reduce its final rendered strength rather than changing the square rhythm.

For generated HTML, read `assets/styles/kimi-construction-grid.css` and inline the
`.kimi-construction-grid` recipe. Use its `--kimi-grid-cell` for the visible field and align the
content origin and major edges to that same cell system. Do not author separate `background-size`
values for horizontal and vertical lines.

When Compact retains a fragment, its rendered strength stays weaker than the Regular target. These
values are reference targets, not hardcoded colors: use runtime semantic tokens, `currentColor`,
opacity, or `color-mix()`.

Treat those ranges as rendered contrast, not the opacity of an already-faint border token. If a
border token is 12% black and is mixed to 36%, the resulting roughly 4% line is too weak. Start from
text-primary or another known opaque runtime role, then mix to the intended final strength.

Recompose spans and line count by tier. Do not scale a desktop grid as a bitmap, preserve empty
columns after disclosure drops, or increase opacity to compensate for a smaller field.

## Line hierarchy and coordination

Use one ordered line system in both themes:

1. **Construction Grid is weakest.** It establishes family structure and real alignment.
2. **Content dividers, baselines, axes, data separators, and semantic coordinates are stronger.**
3. **Selected, focus, warning, and error lines are strongest.** They must remain identifiable
   without relying on color alone.

Align content dividers and semantic axes to the Construction Grid's real rows or columns whenever
possible. They may share position while using different semantic strength. Do not draw a divider
system that drifts between or competes with the background modules.

When a necessary divider or label region cannot align, repair in this order: recompose the content,
adjust spans/modules, reduce the local Construction Grid, then use one tokenized local Glass or
opaque label-safe knockout from `surface-language.md`. The knockout isolates critical copy; it does
not hide an unrelated second grid or incorrect placement.

Grid lines must not reduce readability of titles, key values, direct labels, chart annotations,
controls, or status text. Dense regions receive a clear label-safe zone. Verify the final rendered
contrast order in Light and Dark and in Compact, Regular, and Expanded rather than comparing source
opacity values alone.

## Construction proof

A visible construction grid passes only when all conditions hold:

1. At least two major content edges coincide with declared grid lines.
2. At least one row, baseline, or axis alignment coincides with the declared grid.
3. Both edge and alignment proofs use the same declared grid variables as the content box, columns,
   modules, and baseline. A generic mark alone cannot satisfy this proof.
4. No second decorative grid is drawn independently of layout.
5. The field stays inside the widget boundary and subordinate to P0 in Light and Dark.
6. Dense labels receive a local knockout or clear label-safe zone; a Pixel field never sits beneath
   text and relies on opacity alone.
7. Content dividers/axes align to declared rows or columns, and interaction/state lines remain
   stronger than both without creating an independent grid.

At 100% Canvas zoom, a reviewer must be able to identify the repeated column and sparse module-row field
without developer tools. One-off borders, section dividers, and comments in source do not pass.
Regular needs at least two repeated column divisions and three sparse module rows in the
rendered field; Expanded extends the same system. If the field is only discoverable by zooming,
inspecting CSS, or reading an aria label, increase final token contrast or line frequency while
keeping P0 dominant.

Low-contrast construction lines may continue behind the overall composition when text contrast stays
stable. Validate the selected tier and Canvas placement width and height before removing visible lines.
After that fit check, failure of construction proof means repair the shared layout variables or
remove the lines; it does not license an unrelated faint overlay.
