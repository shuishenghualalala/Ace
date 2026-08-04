# Color and Type

Canonical runtime guide for Kimi widget color, typography, and theme roles. `brand-source.md` retains
official provenance; this file contains the decisions needed during generation.

## Color hierarchy

Before styling, make one contextual color decision: identify the neutral base, the single highest-value
accent or representational palette, and the semantic role of every chromatic family. Blue is not the
fallback accent. When the subject has recognizable material or environmental color, preserve that
relationship through runtime roles; otherwise choose the smallest semantic palette that clarifies P0,
state, series, selection, or control. Monochrome must be an intentional decision supported by strong
type, proportion, and structure, not the result of skipping art direction.

**Neutral-first structure:** establish hierarchy with neutral runtime surfaces, primary/secondary text,
spacing, weight, position, and separators before adding chroma. Kimi Blue is not a default background.
Blue is limited to a high-value interaction, focus, selection, or brand-identification point. A
primary result may use blue only when blue carries one of those meanings; importance alone is not a
color meaning.

Never use blanket saturated blue, a large blue fill or gradient, a glowing blue border, or a
meaningless blue button. Do not color every header, active state, control, or chart series blue.
Prefer a neutral control until it is the primary valid action, selected choice, or current focus.

### Color-count decision table

Count chromatic semantic families: hues that encode a series, category, state, or ordered level.
Neutral scaffolding such as surfaces, text, borders, baselines, and disabled treatment does not count.
Representational material or object colors do not count when they preserve recognition and carry no
data or state meaning. One accent plus a neutral baseline is one semantic hue, not two. Select the
smallest row that answers the job.

| Count | Use when | Typical application |
| --- | --- | --- |
| Monochrome | No chromatic semantic family is needed. | Answer, single-value Measure, or a neutral state with text/icon. |
| One semantic hue | One encoded distinction benefits from chroma. | An accent against a neutral baseline, selected against neutral unselected, or current against neutral reference. |
| Two semantic hues | Two independent encoded meanings must coexist. | Two named chart series or two simultaneous named states when neutral treatment cannot carry either. |
| Three+ semantic hues | Three or more independent encoded meanings are necessary. | A chart with three required series or simultaneous named state categories. |
| Representational multicolor | Hue is inherent to recognition rather than encoding. | A spectrum/EQ or an inspected real-object/material palette. |

Monochrome is the default for Answer. Compare and selected states do not force two chromatic colors;
neutral plus one accent, weight, position, shape, or text is normally enough. Two- and three-hue
semantic output requires a written category inventory before styling. Never add a color merely to
reach a row.

**Semantic versus representational color:** every color that encodes a series, category, state, or
ordered level needs an explicit stable name, placed directly beside the series/category/state or in
an adjacent key. Color never substitutes for `Selected`, `Baseline`, `Warning`, a series name, or
another semantic label. Do not require a separate label for every data point or repeated mark.
Inspected object, product, and material colors may remain unlabeled when they preserve recognition
and carry no data, category, state, or level meaning. If an object color also becomes an encoding,
the semantic use requires a stable name.

Use positive, warning, danger, and disabled tokens only on the affected state mark/control. Pair each
state with text plus position, icon, shape, or pattern. Auxiliary brand colors are for independent
categories or authored brand moments, not decorative mosaics. Avoid gradients unless an inspected
product palette or host token requires one.

## Theme tokens

Light and Dark are both neutral-first. Use runtime roles for primary/secondary/tertiary text,
surface/muted/raised surface, border, brand/accent, semantic states, chart series, focus, and motion.
The host owns values, contrast, and theme switching; widget code owns role-correct use. In both
themes, positive, warning, and danger stay local to the affected state and retain the same direct
semantic label. Disabled is neutral and is not a warning state.

Do not override inspected brand provenance or host tokens. When a recognizable object has an
inspected real-object palette, preserve its identifying relationships and map them to host roles where
possible. Never add literal runtime fallbacks. Never create a separate hardcoded Light/Dark palette or
assume translucent white works in both themes.

## Typography roles

- P0 result/value/state group: `--kimi-font-sans`; numeric values use tabular numerals.
- Title, body, labels, units, and caveats: `--kimi-font-sans`.
- Raw identifiers, coordinates, code, ASCII, and technical metadata: `--kimi-font-mono`.
- Serif/display: rare expressive/editorial brand moments only, never routine product UI.

Define exactly one semantic P0 group, not exactly one DOM element or value. An inseparable atomic pair
such as blood pressure, a score pair, elapsed/duration, or coordinates may live inside that group when
splitting it would destroy the meaning. Give the group one accessible name and shared emphasis;
internal values may remain individually readable. Unrelated values, conclusions, or states cannot
be separate focal points. Labels name the group; metadata qualifies context; a unit stays visually
attached but subordinate; a caveat limits interpretation without competing. Emphasize the P0 group
through at least two channels chosen from size, weight, position, spacing, or neutral contrast. Color
alone is not a channel, and equal-weight text is not hierarchy.

Large P0 values use `--kimi-font-sans` with tabular numerals, not monospace. Monospace is reserved for
technical syntax and aligned raw data, never used merely to make a hero metric feel technical.

Bind the widget root and technical runs to the runtime roles instead of authoring fallback stacks:

```css
.widget { font-family: var(--kimi-font-sans); }
.widget :is(code, pre, kbd, samp, .technical) { font-family: var(--kimi-font-mono); }
```

Use sentence case for product UI. Keep KIMI and model names such as KIMI K3 uppercase. Maintain
clear roles: P0 result/value strongest, title next, label/body supportive, metadata/caveat quiet. Do
not use viewport-width font scaling. Letter spacing is zero in compact product UI unless the runtime
type token explicitly defines otherwise.

## Typography scale

- Product body: 13-14px; compact labels: 11-12.5px; title labels: 14.5-17px.
- Primary display values: 28-42px, with a 48px hard ceiling in Compact.
- Expanded/editorial body: 16px; dense support: 14-15px; title/page labels: 17-20px.
- Product UI uses regular 400 and medium 500; heavier weights are editorial exceptions.

HTML widgets inherit controls and SVG text from the runtime sans role.
Numeric display values use tabular numbers; mono remains for code, raw identifiers, coordinates,
ASCII, and technical metadata.

## Semantic typography recomposition

- **Compact:** retain the one P0 group, its direct label, and attached unit/state. Remove or rewrite lower
  priorities; do not compress metadata or caveats into miniature type.
- **Regular:** retain P0 hierarchy, then add the P1 label/context/control needed to understand or act.
  Metadata and a short caveat appear only when they change interpretation.
- **Expanded:** preserve the same P0 scale relationship while adding P2 evidence and P3 metadata in
  subordinate groups. More space adds evidence; it does not create another hero.

Do not uniformly shrink all type or scale the whole composition between tiers. Recompose roles,
shorten language, change mark structure, and close removed-detail space.

## Fit

Primary titles and values never ellipsize. Shorten or recompose labels before reducing readable type.
Chinese/English must preserve meaning, line budgets, and mixed-script spacing. Large values keep
signs, decimals, separators, units, and tabular alignment at every tier.
