# Runtime Core

Read this single core for every generated Kimi widget. It contains the minimum behavior needed to
ship; load detailed references only through `SKILL.md` routing.

## Job and semantic inventory

Write one line before the P0/P1/P2/P3 inventory: `Mode decision: Object-led mode / Default Kimi mode; recognizable object/product/style - yes/no; owner loaded - yes/not needed.` When recognition is `yes`, load `object-fidelity.md`, choose the mode, research before design, and inventory indispensable anatomy before semantic priority.

Write one sentence: `The widget helps the user ___`. Choose one job: answer, compare, measure,
choose, simulate, sequence, or map. If one visual contract cannot complete the sentence, split the
task or answer in prose.

## Default design responsibility

A short or visually unspecified prompt delegates art direction to this skill; it never means the user
wants unfinished typography on an empty surface. For ordinary Canvas creation, the default Canvas
presentation is Regular. Compact remains a required P0-first responsive state, and Expanded remains a
required evidence-rich state. Default to Compact only when the user explicitly asks for it or the host
placement cannot support Regular after semantic repair.

In Default Kimi mode, read `default-art-direction.md` and compile its Internal design brief before
markup. Map at least two independent content signals to task-specific visual variables; choosing Grid,
ASCII, Glass, or a renderer mode without those mappings is incomplete art direction.

Before markup, write both lines:

- `Visual thesis: ___ makes the job immediately legible because ___.`
- `Dominant visual carrier: ___; derived from the subject/data/state/interaction; owner: ___.`
- `Open outer-frame proof: root ___; full-footprint stage ___; no border, radius, or shadow; bounded local surfaces ___.`

The carrier must be subject-, data-, state-, or interaction-derived. Choose one primary family:
recognizable object anatomy; semantic data shape; control/output instrument; contextual image or
material; local Grid, Pixel, ASCII, route, or trace; or deliberate pure reduction. Route and read the
owner required by that choice before markup. A user should not need to ask for polish, visual interest,
color, Grid, Pixel, Glass, imagery, or motion.

Pure reduction is not visual omission. It still requires authored proportion, a decisive P0 scale,
intentional alignment, closed negative space, controlled neutral contrast, and at least one structural
relationship or separator. A generic text block centered on an otherwise empty surface fails. Regular
and Expanded must visibly embody the subject, data, state, or interaction even when optional house
effects are omitted. Motion remains optional and must explain change, flow, progress, focus, or
transformation rather than compensate for weak composition.

### Object-led visual freedom

Use Object-led mode when the user names or clearly implies a named object, product, device, material,
or established visual style whose own character is part of the request. Research it, preserve its
recognizable skeleton, then let the agent compose freely from that object's form, controls, material,
color, and interaction. In this mode, do not force Construction Grid, interactive ASCII/Pixel, Liquid
Glass, Kimi palette, or Kimi material.

Retain only lightweight constraints: recognizable anatomy and affordances, P0 hierarchy, semantic
recomposition, fit, accessibility, stable states, runtime security, and copyright-safe abstraction.
The result must not reuse the same default Kimi composition used for unrelated objects. If the user
explicitly asks for Kimi styling, selectively re-enable Kimi owners after the object skeleton works.

## Default signature priority

In Default Kimi mode, Regular and Expanded default to both a visible square Construction Grid and one
task-derived coherent ASCII/Pixel system. Before markup, write:

- `Visible Grid decision: square grid; shared cell/origin with content; owner loaded.`
- `Pixel system decision: primary field ___; mode ___; task relationship ___; coordinated region ___/none; pointer response ___.`

Use an omission line only when a layer would fail fit, accessibility, object recognition, task
relevance, or performance. Name exactly one of those reasons and preserve the other eligible layer;
the user not mentioning Grid, ASCII, Pixel, interaction, or visual style is never an omission reason.
Compact may use a static short ASCII/Pixel mark and partial Grid only after P0 fits. Reduced-motion
keeps the field static while preserving its visual meaning.

In Default Kimi mode, when markup contains an authored panel, button, icon button, slider, or segmented control, load
`surface-language.md` and consume the canonical Glass asset. Do not create a panel or control merely
to display Glass, but do not substitute a plain white/gray card or browser-default control once that
component is required by the job.

When the `Glass backdrop field` role is selected, write this line before markup:
`Glass perceptibility proof: stage ___; panel footprint ___; task-derived Light-mode cue ___ continues beneath the panel; Compact fallback ___.`
The cue must cross behind the panel in layout coordinates for that role. A field that is only adjacent
to the panel does not satisfy the Glass backdrop proof. For an `open expressive field` or `P0 Pixel
display`, keep the field in its own open/P0 region and do not force it beneath Glass. A neutral Light
surface with only a faint Grid is never sufficient evidence of Glass.

For responsive markup, consume the host's `html[data-daimon-size-tier]` and mark P0/P1/P2 regions.
Compact must be a real P0-only recomposition whose measured `scrollHeight` fits its viewport; a
single Regular DOM stack with a scrollbar is not an adaptive implementation.

Inventory candidate content before layout:

- **P0:** conclusion, key value, core state, or selected choice.
- **P1:** context or control required to understand or act on P0.
- **P2:** evidence, trend, explanation, secondary data/state/control.
- **P3:** provenance, methodology, timestamp, or supplemental detail.

Define exactly one semantic P0 group, not exactly one DOM element or value. An inseparable atomic pair
such as blood pressure, a score pair, elapsed/duration, or coordinates may share that group when
splitting it would destroy the meaning. Give the group one accessible name and shared emphasis.
Unrelated values, conclusions, or states cannot become multiple focal points. Emphasize the P0 group
through at least two channels among size, weight, position, spacing, and neutral contrast; color alone
does not establish priority. Its direct label names it, its unit stays attached and subordinate,
metadata supplies context, and a caveat limits interpretation without becoming a second headline.
Large P0 values use `--kimi-font-sans` with tabular numerals, never `--kimi-font-mono`; reserve mono
for code, raw identifiers, coordinates, ASCII, and technical metadata.

Treat supplied facts as immutable. Never invent facts, measurements, confidence, provenance, or live
state. When a necessary value is absent, ask for it, show `Unknown`, or label the whole fixture
`Illustrative sample`; never present plausible sample data as observed truth.

Choose the smallest tier that preserves the job within each responsive state: Compact contains the P0 group; Regular contains
P0+P1; Expanded contains P0+P1+P2. P3 appears only after required content fits. Width alone does not select
a tier: include height, line count, controls, data density, and archetype geometry. Ordinary Canvas
creation still presents Regular by default under the design-responsibility rule. Promise only
content that is visibly contained by the chosen Canvas placement.

## Foundation invariants

1. The Daimon host owns the outer frame. Keep the root and any full-footprint stage open-edged with
   no border, radius, or shadow. Paint a local Grid directly on that open stage when needed; bound
   only a real content, control, chart, or Glass region rather than enclosing the whole widget.
2. Use placement, alignment, type, spacing, and separators before borders, fills, color, or depth.
3. Use one dominant archetype. Supporting detail cannot become a dashboard or component gallery.
4. Convert prose into values, marks, relationships, options, states, controls, steps, or links.
5. Keep controls adjacent to the output they affect and reserve stable feedback geometry.
6. Color, icons, Grid, Glass, and Pixel must have named jobs; never rely on color or decoration alone.

Before emitting Grid, Pixel, or Glass, you MUST load its corresponding owner routed by `SKILL.md`.
Those owners define tier mappings, exact visual constants, and omission rules. Universally, derive any
routed visual layer from real content geometry, give it one named job, keep it subordinate to P0, and
omit it only through the named default-signature boundary.

## Semantic recomposition

Across Compact, Regular, and Expanded preserve the same job and archetype while changing information
structure. Shorten language, convert prose to marks/direct labels, reorder P0 first, reduce detail,
and close vacated space. Do not scale the whole widget, continuously shrink type, crop, hide semantic
content with overflow, or leave an empty large shell.

Do not uniformly shrink type or geometry between tiers. Compact keeps P0 plus its direct label/unit;
Regular adds required P1 context or action; Expanded adds subordinate P2 evidence and then P3
metadata/caveats. Recompose semantic roles instead of preserving an equal-weight miniature layout.

Compact is a strict P0-group allowlist. It may include P0's direct label/unit, one nonnumeric state mark,
and a safety stop/cancel. It may not include blocker counts, reasons, comparison basis, alternate
options, next actions, confirmation controls, evidence rows, caveats, or provenance, except for the
explicit destructive-confirmation exception below. When any other excluded item is required to
complete the job, Regular is the minimum valid default.

Operational build, readiness, health, and status Measure defaults to `utility` unless the user job
genuinely requires a contextual subject, story, or concept. Before Regular/Expanded utility markup,
record exactly one branch:

- **Field:** the required Default Kimi branch; name the carrier and task-related job, or name the
  atmosphere role, read `brand-texture-language.md` before markup, then emit one primary abstract
  interactive field plus only an allowed coordinated region. A brand-atmosphere field may be static
  when motion has no useful role, but keeps the same bounded footprint and fit rules.
- **Omission:** record one named reason from `fit`, `accessibility`, `object recognition`, `task relevance`, or `performance`; `no viable carrier role` is also valid; never omit
  silently. A named omission does not require loading the Pixel owner. Read
  `expressive-composition.md` only when expression remains uncertain.

Compact Pixel remains optional and P0-first; load the Pixel owner only when a field is selected. The
destructive-confirmation exception never requires the Pixel owner.

Native destructive actions are not widgets by default. Only when explicitly requested as a widget,
Compact destructive-confirmation safety P0 is exactly the object name, one concise irreversible
warning, and `Delete` and `Cancel`. Omit timestamp, provenance, metadata, and detail. Use a stable
inline/two-column action row when width permits; otherwise choose Regular instead of stacking
full-width actions or clipping the warning. The confirmation surface and controls preserve <=8px radius.

Compact Compare contains one P0 statement: the winner or exception, optionally followed by one short
decisive delta as its direct label. Do not add a criteria row, alternate option, or separate
comparison-basis explanation.

## Runtime and theme

- Use the Daimon Widget/Blueprint tool contract; do not invent APIs. Read runtime detail for files,
  external data, Canvas bridge behavior, outgoing actions, unusual placement, or host overlays. In
  conversation and Canvas surfaces, apply the **Daimon host-control safe zones** from
  `daimon-runtime-integration.md` before placing the first semantic row; hidden hover chrome still
  occupies its measured zone when revealed.
- Use `--kimi-font-sans`, `--kimi-font-mono`, and semantic runtime color/surface/state tokens. The
  host owns Light/Dark values and switching; widget code owns role-correct usage.
- Start from neutral structure. Kimi Blue is never the default widget background and is reserved for
  a high-value interaction, focus, selection, or brand-identification point. Read
  `color-and-type.md` for the required contextual color decision and before using more than one
  semantic color family. Blue is not the fallback accent.
- Count chromatic semantic families only. Neutral scaffolding and nonsemantic inspected
  object/material colors do not count; one accent plus a neutral baseline is one semantic hue.
  Compare and selected states do not require two chromatic colors.
- Canonical aliases are `--kimi-color-text-primary`, `--kimi-color-text-secondary`,
  `--kimi-color-text-tertiary`, `--kimi-color-text-quaternary`, `--kimi-color-surface`,
  `--kimi-color-surface-muted`, `--kimi-color-surface-raised`, `--kimi-color-surface-strong`,
  `--kimi-color-border`, `--kimi-color-accent`, `--kimi-color-on-accent`,
  `--kimi-color-positive`, `--kimi-color-warning`, and `--kimi-color-danger`.
  Do not add literal color fallbacks or invent shorter aliases.
- Do not add a global Canvas grid, fake app/browser chrome, host selection controls, or a second
  outer card.
- Outgoing interaction sends a natural user message. Never expose secrets or use arbitrary network,
  filesystem, clipboard, or native actions.

## Controls and states

Do not add controls unless the user job changes an input or commits an action. A read-only Answer,
Compare, Measure, Sequence, or Map does not gain choose/replay/apply controls merely to feel interactive.

Use the native control matching the value. Every control needs a visible effect, label, range/unit
when relevant, keyboard access, and `:focus-visible`. Never hide a focusable action with `display:
none` or `visibility:hidden`; hover reveals must also reveal on focus and coarse pointers.

Show idle, selected, loading, success, warning, error, empty, and disabled states through stable
geometry, labels, position, marks, and recovery actions before color. Keep semantic color local to
the affected state. Loading reserves space and uses `aria-busy`; errors name a concise cause and a
recoverable action; disabled controls preserve geometry and explain why when needed.

## Fit and accessibility

- Visible P0/P1/P2 content MUST use semantic HTML such as `h1`-`h6`, `p`, `output`, or `dl`, or carry
  an explicit semantic contract through `data-semantic` or an appropriate ARIA `role` and accessible
  name such as `aria-label`/`aria-labelledby`. A widget with no visible semantic element fails release,
  even when generic `div`/`span` text is visually present.
- Primary titles, values, states, direct labels, and critical controls never use ellipsis.
- All widget `letter-spacing` is `0`; never use a negative value. Prominent numeric `output` uses
  `display: block` or `inline-block`, authored `line-height` sufficient for its glyphs, normally
  `1.05-1.15`, `box-sizing: border-box`, and no fixed height smaller than its line box.
- Long copy becomes shorter complete language or a different visual structure; do not shrink below
  readable UI type to make a tier pass.
- Direct labels and controls remain readable without hover. Icon-only controls need accessible names;
  decorative marks are hidden from assistive technology.
- When P0 contains an inseparable atomic pair, expose one accessible group name and preserve shared
  emphasis rather than announcing unrelated primary regions.
- Preserve meaning without color, texture, Glass, depth, or motion. Honor reduced motion and provide
  a complete static/fallback state.
- Verify real Chinese/English content, large numbers, signs, decimals, units, and text expansion.
- Ordinary widgets do not scroll internally. Editors, logs, terminals, and genuinely large tables
  may scroll only when scrolling is part of the job.
- Verify `240x180`, `420x320`, `960x720`, and `1120x820` in both themes. At each placement require
  `scrollWidth <= clientWidth + 1` and `scrollHeight <= clientHeight + 1` unless scrolling is intrinsic.
- Verify each visible semantic output element itself at `240x180`, `420x320`, `960x720`, and
  `1120x820` in both Light and Dark: `scrollWidth <= clientWidth + 1` and
  `scrollHeight <= clientHeight + 1`. Document/body fit does not substitute for this element-level check.
- Top-level widget, `html`, and `body` may not use `overflow: hidden`; use it only inside a bounded
  decorative track/plot whose complete labels and semantic marks already fit.
- If disclosure depends on width **or** height, use a valid condition such as
  `@container (max-width: 339px) or (max-height: 219px)`: container conditions use `or`, not a comma.
  Height queries require `container-type: size`; a named query requires a matching `container-name`.
  A container cannot query itself, so query descendants or add an outer size container.

## Fast ship check

Before creating markup, name the job, selected archetype, P0/P1/P2 inventory, default tier/placement,
dominant visual moment, and likely fit failure. After rendering, verify three tiers and both themes:
no overlap, clipping, escaped labels, inaccessible controls, meaningless whitespace, or content
outside placement height. When fit fails, repair job/tier/placement and semantic structure before
removing useful content or visual language.
