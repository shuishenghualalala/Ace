# Kimi Widget Grid and Composition

This file combines Kimi's official grid rules with Müller-Brockmann / International Typographic
Style discipline. Use it when designing widget layout, alignment, density, hierarchy, or visual
verification.

Source translation:

- The intended `hyperagent-public-skills/skill-muller-brockmann-grid-systems.json` source maps to
  real, verifiable grid engineering: one source of truth, same-content-box overlays, subgrid bands,
  8px baseline lock, and optical ink alignment. If the raw JSON is unavailable locally, use these
  translated rules as the canonical widget contract and keep the missing source as an audit caveat.
- The no-OCR Müller-Brockmann PDF is treated as visual/source inspiration, not a directly loaded
  text reference.
- Do not import the source palette literally. Classic red maps to Kimi's semantic brand accent:
  Kimi Blue only when it changes interpretation.

Reference map:

- `design-system.md` owns brand translation, visual job routing, color, typography, data, and QA.
- `adaptive-widgets.md` owns compact / regular / expanded disclosure and Daimon placement sizes.
- `adaptive-semantic-fit.md` owns P0-P3 disclosure and placement-first repair.
- `grid-and-layout.md` owns tier visibility, column ranges, and coordinate-grid taxonomy.
- `brand-texture-language.md` owns Pixel roles, options, routing, and area limits.
- This file owns grid construction: margins, columns, modules, baseline, optical alignment, and
  archetype-specific placement contracts.

<!-- preview-rule: grid.method | section: Grid and composition | specimen: grid-system | coverage: covered | priority: core -->
## Grid method

Treat the grid as a load-bearing system and visible Kimi family structure, not an independent overlay
style. Tier visibility comes from `grid-and-layout.md`; semantic disclosure and Pixel allowances come
from their owners above.

1. Derive the outer margin first.
2. Derive column gaps and internal spacing from that margin or the runtime spacing scale.
3. Place content on columns, modules, rows, or baseline steps.
4. Let hierarchy come from scale, position, density, and alignment before color or containers.
5. If a visual guide is shown, it must align with the same content box the widget uses.

Müller-Brockmann's useful lesson for widgets is not Swiss nostalgia. It is objective order: the
system should make the answer easier to inspect and harder to randomly restyle.

If a construction grid, ruler, baseline, or column field appears, it must describe the actual content
placement. A semantic grid additionally needs labeled chart, map, or process meaning. Decorative
overlays that do not share the content box are failures.

Prove placement with real content before showing visible lines. Construction is a legitimate brand-
structure job even when the lines do not encode a datum: values, labels, controls, marks, and captions
must still share the same content box, column lines, modules, and baseline rhythm.

A construction proof passes only when at least two major content edges and one row, baseline, or axis alignment use the same declared grid variables. A generic mark alone cannot satisfy construction proof;
marks may support the required row, baseline, or axis alignment but cannot replace it. A standalone
grid, ruler, dot matrix, or module diagram is not proof unless actual content occupies the same lines.

<!-- preview-rule: grid.kimi-units | section: Grid and composition | specimen: grid-system | coverage: covered | priority: core -->
## Kimi units

Use the **5% grid** as the Kimi composition reference for canvas-style or larger widgets:

- `X = round(shortest edge * 0.05)`.
- Outer margins are equal on all sides when the widget has a framed composition.
- Column gap = `round(X / 5)`.
- Content area is roughly 90% of the shortest-edge frame.

For compact inline widgets:

- Use the runtime 4 / 8 / 12 / 16 / 20 / 24 / 32 spacing scale.
- Preserve the same spirit: equal margins, predictable gaps, clear alignment.
- Do not force a visible grid if it costs legibility.

<!-- preview-rule: grid.baseline | section: Grid and composition | specimen: grid-system | coverage: covered | priority: core -->
## Baseline and modules

Use a practical baseline:

- 8px is the default baseline unit.
- Body line-height should usually be 24px.
- Dense labels can use 16px or 20px line-height.
- Display values should sit on a deliberate line-height, not browser default leading.
- Major vertical spacing should be a multiple of 4px, preferably 8px.

Use modular fields when the widget contains many small comparable units: swatches, mini charts,
state nodes, checklist rows, benchmark samples, or option matrices. Use columns only when scanning
is mostly horizontal: comparisons, tables, chart labels, and control/output pairs.

<!-- preview-rule: grid.source-of-truth | section: Grid and composition | specimen: grid-system | coverage: covered | priority: core -->
## One grid source of truth

Define the selected tier's column values from `grid-and-layout.md` once on the widget root and derive
layout, visible lines, and measurement from those same values.

```css
.widget {
  --grid-cols: 8;
  --grid-gap: 12px;
  --grid-margin: clamp(12px, 5cqi, 24px);
  --baseline: 8px;
  --leading: 24px;
}
```

Rules:

- Content, optional guides, and overlay diagnostics must use the same content box.
- Place major elements by column line or module span, not by arbitrary pixel offsets.
- Keep line-height, vertical gaps, row heights, media heights, and control heights on 4px / 8px
  rhythm. Use 24px leading for body copy when space permits.
- Do not show a grid toggle or guide unless it reveals the grid the widget is actually using.
- Select tier visibility and column count from `grid-and-layout.md`. Do not redefine Compact semantic
  or Pixel allowances in this construction reference.

### Same content box rule

If a guide, ruler, grid proof, or diagnostic overlay is shown, it must live in the exact same content
box as the widget content:

- same outer margin;
- same column count;
- same gutter;
- same baseline unit;
- same module field.

A grid drawn on top of the widget but not sharing those values is decoration and must be removed.

Low-contrast construction lines may continue behind the overall composition when text contrast is
stable. Keep at least one base spacing unit between a Pixel field and titles, values, direct labels,
controls, focus states, and caveats. Dense labels require a local knockout or **label-safe zone**;
never rely on opacity alone to separate a Pixel field from text.

### Subgrid band rule

For larger widgets, think in horizontal bands that re-expose the parent grid. Each band spans the
full content box, then places children by column line or module span. In code, this may be CSS
`subgrid` where available or a repeated `grid-template-columns` fallback. The important rule is not
the CSS feature; it is that headline, marks, labels, controls, and captions snap to the same lines.

Compact widgets may skip explicit subgrid bands, but they must still preserve shared left edges,
equal insets, stable row heights, and direct labels.

<!-- preview-rule: grid.proof | section: Grid and composition | specimen: grid-system | coverage: covered | priority: core -->
## Grid proof

Before writing CSS, name the grid proof in plain terms:

1. **Content box**: the inset or margin that all major elements share.
2. **Primary axis**: the columns, rows, lane, or module field that carries the answer.
3. **Baseline rhythm**: the 4px / 8px rhythm used by labels, rows, rules, and controls.
4. **Visible role**: construction grid for family structure or semantic grid for labeled coordinates.
5. **Dominant contract**: the archetype contract that prevents component collage.
6. **Adaptive constant**: what stays structurally identical from Compact to Expanded.

The proof does not need to appear as text in the widget. It must be visible in the result. If the
visible layout cannot reveal the proof, the widget is not using a grid; it is merely arranged.

Name the proof with concrete values before CSS: `content box = X px / 5% shortest edge`, `gap = X /
5` or runtime token, `columns/modules = N`, `baseline = 8px`, and `key spans = ...`. "Aligned to
grid" is too vague to pass.

Use the Müller-Brockmann test: the system should make the widget harder to randomly restyle. If the
same content could be rearranged into cards, blobs, or a dashboard without losing anything, the grid
is not carrying enough meaning.

<!-- preview-rule: grid.archetype-contracts | section: Layout patterns | specimen: layout-archetypes | coverage: covered | priority: core -->
## Archetype grid contracts

Use these contracts after choosing the visual job router. They are stricter than visual preference:
they prevent generated widgets from drifting into random card layouts.

The contract should be testable in the rendered result. If the widget were cropped to its primary
content area, a reviewer should still be able to tell whether it is a matrix, data instrument,
choice elicitor, control/output band, timeline, relationship map, or answer card. Labels support
the structure; they should not be the only evidence of the archetype.

| Archetype | Grid contract | Must preserve | Avoid |
|---|---|---|---|
| Answer card | 1 strong value column plus 1 support column at regular/expanded sizes | value, label, 2-4 facts on shared baselines | unrelated metric collage |
| Comparison matrix | criteria rows x option columns; winner indicated by one column rule or mark | row alignment, short cells, direct criteria labels | option cards with paragraphs |
| Data instrument | label column + mark field + value column; chart marks share a baseline or axis | direct labels, baseline/reference line, accurate scale | legends far from marks |
| Choice elicitor | equal-width choice modules; selected state does not resize the row | comparable choices, one natural action | wizard/sidebar form |
| Control + output | controls and output share one band; output sits beside or immediately below controls | cause/effect proximity, one dominant result | remote settings panel |
| Timeline / process | one axis or lane; states occupy ordered modules | current/next/blocker state visible before prose | one card per step |
| Relationship map | nodes on a module field; links use consistent direction and labels | group logic, labeled links, stable node spacing | decorative node cloud |

Compact preserves archetype recognition through the arrangement of P0 itself, not supporting anatomy;
use `adaptive-semantic-fit.md` for the allowlist. Larger tiers add rows, labels, and evidence inside
the same archetype contract instead of switching layouts.

Nested module fields must be derived from the parent content box. They either start/end on parent
column lines or declare the parent-derived inset, gap, and baseline. Arbitrary inner insets or
independent mini-grids fail the proof.

<!-- preview-rule: grid.optical-alignment | section: Grid and composition | specimen: grid-system | coverage: covered | priority: supporting -->
## Optical alignment

Large display type can look off-grid even when its layout box is aligned. If a widget uses a very
large numeral, masthead, or coordinate label, align the perceived ink edge with nearby content:

- keep the text box on the grid;
- compare the visible glyph edge with adjacent labels or rules;
- nudge only when the visual edge is clearly wrong;
- never let optical nudging break scanning alignment for tables or controls.

This matters most in expanded widgets and brand/editorial specimens. It is rarely needed in compact
utility widgets.

For widget generation, do not add runtime canvas measurement unless the widget contains very large
display type or a visible grid proof. Usually the practical rule is enough: align large numerals by
their perceived ink edge with the nearest label or rule, then verify visually at compact and
expanded sizes.

Remember: box-on-grid is not always ink-on-grid. A display number can have a perfectly aligned CSS
box and still look wrong because the glyph side-bearing shifts the visible ink. Fix the perceived
edge, not the DOM rectangle, and only where the correction is visible.

<!-- preview-rule: grid.verification | section: Preflight checklist | specimen: pressure-tests | coverage: covered | priority: core -->
## Grid verification

Before shipping a grid-dependent widget, check:

1. Shared content box: content and every visible construction line share the same margins and variables.
2. Construction proof: at least two major content edges plus one row, baseline, or axis alignment use
   the same declared grid variables; a generic mark alone does not pass.
3. Baseline lock: text rows, dividers, controls, and chart bands use 4px / 8px multiples.
4. Optical display alignment: large values look aligned by ink, not merely by layout box.
5. Adaptive stability: Compact, Regular, and Expanded keep the same contract and reveal more
   structure instead of changing visual language.
6. Density fit: the number of rows, repeated items, and labels fits the host height without clipped
   partial rows.
7. Label safety: dense labels use clear zones and Pixel fields remain outside protected content.

<!-- preview-rule: grid.failures | section: Preflight checklist | specimen: pressure-tests | coverage: covered | priority: core -->
## Composition failure signals

Redesign when you see any of these:

- A grid overlay is decorative and does not match the content.
- Everything is boxed, so hierarchy depends on card count instead of structure.
- Margins differ randomly from side to side.
- Chart marks, labels, and controls do not share alignment lines.
- A timeline is one card per step instead of an axis or lane.
- A matrix becomes paragraphs in option cards.
- Display type is oversized but not anchored to the surrounding information.
- A visible grid is independently drawn and cannot explain where content sits.
- Compact, Regular, and Expanded use unrelated layouts instead of one adaptive contract.
