# Widget Foundation

Universal, non-negotiable behavior for every Daimon/Kimi widget. Read this before widget code.
Brand styling may extend this foundation but cannot weaken task completion, fit, accessibility,
state clarity, host ownership, or interaction.

## Contents

- [Invariants](#invariants)
- [Seven-job router](#seven-job-router)
- [Layout archetypes](#layout-archetypes)
- [Container grammar](#container-grammar)
- [Controls and interaction](#controls-and-interaction)
- [State grammar](#state-grammar)
- [Accessibility](#accessibility)
- [Render and ship gates](#render-and-ship-gates)
- [Anti-randomness review](#anti-randomness-review)

<!-- preview-rule: family.invariants | section: Design language | specimen: family-invariants | coverage: covered | priority: core -->
## Invariants

1. **Quiet frame:** the Daimon surface owns the outer frame. The widget root and any
   full-footprint stage remain open-edged; bound only a smaller region with a real containment job.
2. **Crisp hierarchy:** use placement, alignment, type, spacing, and separators before shape or
   color.
3. **Semantic signal:** color and icons carry meaning, not decoration; never rely on color alone.
4. **Compact interaction:** controls stay adjacent to the output they affect.
5. **Visualized reasoning:** convert prose into relationships, quantities, states, options, steps,
   or spatial structure. If the result remains mostly prose, answer in text.

<!-- preview-rule: job.router | section: Design language | specimen: job-router | coverage: covered | priority: core -->
## Seven-job router

Choose the immediate user job first. If several apply, route the decision the user needs now.

| Job | Dominant form | Default archetype | Avoid |
|---|---|---|---|
| Answer | one result plus short proof | Answer card | metric collage |
| Compare | aligned criteria and options | Comparison matrix | paragraph cards |
| Measure | marks with direct labels | Data instrument | decorative dashboards |
| Choose | comparable choices | Choice elicitor | long wizards |
| Simulate | controls adjacent to output | Control + output | remote settings |
| Sequence | axis, lanes, states, arrows | Timeline / process | one card per step |
| Map relationships | nodes and labeled links | Relationship map | unlabeled color coding |

<!-- preview-rule: layout.archetypes | section: Layout patterns | specimen: layout-archetypes | coverage: covered | priority: core -->
## Layout archetypes

Pick one dominant archetype and name its grid contract plus what must remain visible.

- **Answer card:** title -> primary value/result -> 2-4 proof facts -> optional action.
- **Comparison matrix:** criteria as rows, options as columns, short aligned cells.
- **Data instrument:** question -> marks -> direct labels -> compact detail.
- **Choice elicitor:** one question -> 2-6 comparable choices -> selected state/action.
- **Control + output:** nearby controls -> one prominent result -> secondary evidence.
- **Timeline / process:** axis or lanes -> current/next/blocker states -> compact labels.
- **Relationship map:** consistent nodes -> directional labeled links -> groups only when useful.

Secondary content must remain subordinate. Never render the archetypes as a component gallery.

<!-- preview-rule: container.grammar | section: Layout patterns | specimen: container-grammar | coverage: covered | priority: core -->
## Container grammar

Default to a transparent/inherited base, open layout, shared alignment, and fine separators. The
root and any full-footprint stage that spans nearly the whole widget must not create a second card
silhouette. Do not inset a full-size stage merely to draw a rounded boundary around the Grid.

```css
.widget-root,
.widget-root > .full-footprint-stage {
  border: 0;
  border-radius: 0;
  box-shadow: none;
}
```

A host-matching base fill or local Construction Grid may reach the widget edges, as in an open
instrument surface, but it cannot use an enclosing stroke, rounded clipping, or floating shadow.
Use a bordered container only for a smaller selectable option, control cluster, repeated item needing
scan boundaries, chart/table/tool frame, confirmation surface, or justified local Glass panel.

Do not duplicate host selection chrome, draw fake browser/app shells, nest cards, add one panel per
section, or wrap all content and Grid in a framed outer stage. Put focus outlines on real controls,
not a fake outer wrapper.

<!-- preview-rule: components.layout | section: Controls and states | specimen: controls-states | coverage: covered | priority: core -->
## Controls and interaction

- Use the native control matching the value type. Keep controls neutral unless the selected value
  itself carries semantic state.
- Keep controls adjacent to their output and reserve space for feedback so state changes do not
  shift layout.
- Write outgoing intent as a natural user message. Defer the actual API signature to
  `daimon-runtime-integration.md`.
- Destructive and secondary row actions may reveal on `:hover`, but must also reveal through the
  control's `:focus-visible` or row `:focus-within`. Keep them in the DOM and tab order.
- Use an opacity-based reveal that preserves tab order, such as `opacity: 0; pointer-events: none`
  at rest and `opacity: 1; pointer-events: auto` on hover/focus. Never use `visibility: hidden` or
  `display: none` for a focusable action. Keep actions persistently visible for `hover: none` or
  `pointer: coarse`.

<!-- preview-rule: components.state-grammar | section: Controls and states | specimen: controls-states | coverage: covered | priority: core -->
## State grammar

Show state through position, label, mark, reserved space, disabled affordance, or progress before
color. Keep semantic color local.

| State | Required carrier |
|---|---|
| Idle / ready | stable neutral label and enabled control |
| Selected | checkmark, pressed segment, or local row emphasis |
| Loading / pending | reserved geometry, concise progress, `aria-busy` |
| Success | confirmation row or completed state node |
| Warning | caveat, blocker, or threshold mark near the risk |
| Error | concise cause plus recoverable action |
| Empty | expected structure plus next action |
| Disabled | preserved geometry, reduced contrast, reason when needed |

## Accessibility

- Every action is keyboard reachable and has a visible `:focus-visible` state. Icon-only controls
  need accessible names; decorative marks are hidden from assistive technology.
- Do not use hover as the only path to a control or label. Preserve touch and coarse-pointer access.
- Preserve meaning without color, animation, texture, or depth. Honor reduced motion; any structural
  depth needs a complete static fallback and keyboard operation.
- Keep labels, values, controls, and direct chart labels readable without hover. Follow the strict
  text-fit and pointer-target rules in `adaptive-widgets.md`.

<!-- preview-rule: adaptive.summary | section: Adaptive behavior | specimen: adaptive-tiers | coverage: covered | priority: core -->
Adaptive behavior is mandatory and fully defined in `adaptive-widgets.md`: semantic disclosure is
selected from width, height, and content density, never from a width band alone.

<!-- preview-rule: generation.protocol | section: Design language | specimen: decision-order | coverage: covered | priority: core -->
## Render and ship gates

Before markup, provide concrete answers for:

| Proof | Passes when |
|---|---|
| Job | one immediate job and one dominant archetype |
| Relationship | the main value, state, choice, sequence, or link is visible |
| Density | required rows, labels, and controls fit the selected disclosure tier |
| Layout | content box and shared alignment/axis are named |
| Failure | the likeliest clipping, state, label, theme, or interaction failure is prevented |

<!-- preview-rule: qa.anti-randomness | section: Preflight checklist | specimen: anti-randomness | coverage: covered | priority: core -->
### Anti-randomness review

Score every generated widget with the 6-point family likeness rubric. A shippable widget should
score at least **5/6**.

If the score is below 5/6, revise the structure before revising visual expression. Patch in this
   order: job/router -> dominant archetype -> density tier/placement -> grid contract -> semantic
   signal -> brand signature. Do not raise the score with arbitrary blue, icons, cards, labels, wallpaper,
   or random decoration. A motif must be a semantic carrier or bounded authored brand atmosphere as
   defined by `brand-texture-language.md`.

In review widgets, show the failing dimension and repair path as visible marks. The anti-randomness
surface should locate drift, show the 6-point family likeness score, and route the patch; it should
not become another prose checklist.

| Point | Dimension | Passes when |
|---:|---|---|
| 1 | Job and router | The immediate user job is clear and routed through the visual job router. |
| 1 | Dominant archetype | One archetype controls the layout; secondary details stay subordinate. |
| 1 | Quiet frame and hierarchy | The host background is preserved and hierarchy comes before containers. |
| 1 | Semantic signal | Color, icons, and status marks carry data or state-grammar meaning; state color stays local. |
| 1 | Visualized reasoning | Prose becomes relationships, quantities, states, choices, controls, or steps; states are structural marks, not prose-only notices. |
| 1 | Kimi brand signature | A justified grid, type, blue, texture, pixel/ASCII/code, cursor/bracket/build, or pure-reduction decision is visible; motifs are a semantic carrier or bounded authored brand atmosphere and adapt by size. |

Brand-specific failure signals:

- Blue used for all buttons or active states.
- Serif/display typography used in routine UI.
- ASCII/pixel motifs used as wallpaper, random decoration, or unauthored atmosphere.
- Data visualization becomes a colorful dashboard skin.
- Loading, success, warning, or error turns into a full-surface color theme.
- Small Canvas widgets hide overpacked content behind scroll panes, clipped rows, or accordions.
- The widget looks like a slide or website section instead of a compact tool.

Patch failures in this order:

1. **Job/router**: reduce to the immediate user job and one archetype.
2. **Density tier / placement**: reselect the smallest valid tier and correct Canvas width/height
   under `adaptive-semantic-fit.md`.
3. **Grid contract**: put content back on shared axes, modules, rows, or lanes.
4. **Semantic signal**: remove decorative blue and keep status color local.
5. **Brand motif**: remove wallpaper and random decoration; keep only a semantic carrier or bounded
   authored brand atmosphere under `brand-texture-language.md`.
6. **Theme/token**: replace hardcoded colors with token roles and verify light/dark.

<!-- preview-rule: qa.checklist | section: Preflight checklist | specimen: qa-checklist | coverage: covered | priority: core -->
Ship only when all apply:

1. The host frame remains quiet and the widget is more structure than prose.
2. Controls, state, recovery, keyboard access, focus, touch, and accessible names are complete.
3. Runtime APIs and semantic tokens follow `daimon-runtime-integration.md`; font tokens own all
   fallbacks.
4. Compact / Regular / Expanded disclosure, strict text fit, both themes, and measured dimensions
   pass `adaptive-widgets.md`. Vertical scrolling is limited to explicit editor, log, terminal, or
   large-table jobs.
5. Relevant grid, brand-boundary, icon, texture, data-label, reduced-motion, and static-depth rules
   pass their routed references.
6. No semantic content hides behind clipping, accordions, hover, unreadable type, or decoration.
