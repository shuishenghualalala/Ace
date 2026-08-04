# Data Visualization

Canonical owner of chart marks, scale, labels, legends, targets, baselines, series, caveats, and
collision repair. Use with `archetype-measure.md` when a chart communicates more than one value.

## Chart decision

Before CSS, state the question, minimum mark type, scale/domain or normalization, primary
relationship, and label plan. Accuracy and readable comparison come before styling. Remove any
chart whose answer is clearer as one value, delta, state, or ranked list.

## Roles

- Apply the color-count decision table in `color-and-type.md` before assigning chart paint. Count
  chromatic semantic families, not neutral scaffolding or nonsemantic material/object colors. Most
  Answer/Measure charts stay monochrome; Compare is normally neutral plus one contrast.
- Current/selected/leading value may use Kimi Blue only when it is also a high-value selection,
  focus, interaction, or brand-identification point.
- Baseline/reference is neutral and visually subordinate.
- Target uses a distinct direct label and a blue tint only when it changes interpretation.
- Secondary series stay neutral or use chart tokens only when independent categories require them.
- Noise, unavailable data, and uncertainty use muted semantic roles plus labels/pattern/position.
- Caveat and source are compact metadata, never a large caption or competing card.

Use a sequential scale only for ordered magnitude and categorical chart tokens only for independent
named categories. A spectrum/EQ may preserve domain hue; simultaneous state categories use local
positive/warning/danger roles; an inspected real-object palette preserves identifying relationships.
Every semantic series, category, state, and ordered range has a stable identity. Inspected object or
material colors need no separate labels when they preserve recognition and encode no data or state.
Use at most the categories the question requires, never color alone, and never turn semantic states
into a full chart theme.

## Direct labels and legends

Direct labels next to a series, category, state, target, or baseline are preferred. Each semantic
series/category/state must be identifiable at the selected tier; individual data points and repeated
marks do not each need a name unless inspection of those values is part of the job. Color may
reinforce identity but cannot create it.

1. Reserve lanes for labels before drawing marks.
2. Shorten without ambiguity (`Baseline` to `B` only when the nearby key remains clear).
3. Stagger labels or move secondary labels to a separate aligned rail.
4. If collision remains, use a compact keyed legend, including for 3+ entries, only when it stays
   adjacent to the chart and every entry fits without collision or truncation.
5. If an adjacent keyed legend does not fit, reduce the series/category count or enlarge the tier.

Never overlap values, stack labels on one coordinate, clip axes, require hover to read a direct
label, or place labels under Pixel/Grid/Glass effects. A label-safe zone uses an opaque or tokenized
Glass knockout and does not change the data position.

## Mark rules

- Lines: use at most four series unless the user explicitly needs more; keep direct labels readable
  and use consistent stroke hierarchy.
- Bars/columns: consistent widths and baselines; values label marks directly. Bars and large
  rectangles remain chart marks, not a Pixel Field.
- Ranked multivariate rows: label | primary bar | secondary dot/reference | value/unit.
- Maps/heatmaps: sequential scale first; name categories and ranges.
- Axes, baseline, target, grid, direct labels, and content dividers follow the line hierarchy and
  alignment rules in `grid-and-layout.md`.

## Tier recomposition

- **Compact:** P0 value/unit/state only; no compressed chart or hidden secondary labels.
- **Regular:** primary marks, direct labels, baseline/target, and a short caveat when needed.
- **Expanded:** secondary series, axes/history/distribution, source, and evidence only when every
  label fits.

When space contracts, convert chart to value-plus-delta, ranked rows, or a shorter trend before
shrinking type. Close removed detail space and preserve the same measure job. Never obtain Compact
by scaling all chart marks, labels, and typography together.
