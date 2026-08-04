# Kimi Perspective Widget — Design System

This legacy document is a migration/index map, not a competing canonical visual owner. Use its
navigation and historical context to find the focused routed references; implement rules from those
owners, and let a routed owner win if wording here differs. Do not load this whole map for routine
generation.

The source deck, Figma file, and downloaded packages are provenance only; they are never required to
generate or render a widget at runtime. `brand-source.md` freezes source facts, while the reference map
below identifies the canonical owner for each executable visual or runtime decision.

Recorded provenance: the Kimi Brand Guidelines, supplied as the Figma Slides deck (`xEVNYgcE8PyeztPBCcmYKS`).
This identifier preserves traceability only and does not supersede the bundled source records.

Daimon owns Widget runtime and Canvas placement. `widget-foundation.md` owns universal task and
interaction behavior. No separate design skill is required.

## Navigation

Read only the headings required by the visual decision:

| Decision | Heading |
|---|---|
| Brand facts and principles | [0. Official brand source](#0-official-brand-source), [1. Brand foundation](#1-brand-foundation) |
| Kimi signature and translation | [2. Widget design thesis](#2-widget-design-thesis) |
| Formal Kimi composition | [3. Grid and composition](#3-grid-and-composition) |
| Palette or chart color | [4. Color system](#4-color-system) |
| Font roles and scale | [5. Typography](#5-typography) |
| Motifs, texture, or structural depth | [6. Art direction](#6-art-direction) |
| Icons, logo, or motion | [7. Icons, logo, and motion](#7-icons-logo-and-motion) |
| Data marks and labels | [9. Data visualization](#9-data-visualization) |
| Semantic visual tokens | [10. Runtime token mapping](#10-runtime-token-mapping) |
| Brand review and pressure tests | [11. Brand QA and benchmarks](#11-brand-qa-and-benchmarks) |

Reference map:

- `widget-foundation.md` stores invariants, the seven-job router, archetypes, container grammar,
  controls, states, accessibility, interaction, and the universal ship gate.
- `daimon-runtime-integration.md` stores Daimon Widget / Canvas runtime ownership, security,
  theme, host placement, and implementation constraints.
- `brand-source.md` stores canonical Kimi brand facts, palette, typography, and logo boundaries.
- `composition-system.md` stores the Kimi 5% grid, Müller-Brockmann-derived composition method,
  baseline/module rules, and optical alignment checks.
- `adaptive-widgets.md` stores compact / regular / expanded information disclosure and Daimon
  Canvas resize behavior.
- `brand-texture-language.md` stores the executable texture grammar, signature carriers, motif
  routing, size degradation, theme behavior, and anti-pattern repair.
- `surface-language.md` stores local surface depth and Liquid Glass theme, fallback, contrast, and
  performance behavior.
- `adaptive-semantic-fit.md` stores priority disclosure and placement-first repair.
- `grid-and-layout.md` stores construction-grid tier mapping and coordinate taxonomy.
- `icon-system.md` stores icon selection, sizing, color, and accessibility rules.

---

<!-- preview-rule: brand.source | section: Brand foundation | specimen: brand-source | coverage: covered | priority: core -->
## 0. Official brand source

Use the frozen facts in bundled `brand-source.md` as the source of truth for widget visual identity.
Its external source deck is provenance, not a runtime dependency.

- Brand keywords: **Avant-garde / Curious / Pure**.
- Brand slogan: **Born to build.**
- Core brand color: **Kimi Blue `#81C4FF`**.
- Core typography set: **Inter**, **Geist Mono**, **MiSans**, **Sentient**, and
  **Source Han Serif CN**.
- Composition provenance: the deck documents a five-percent-derived grid concept; executable widget
  composition belongs to `composition-system.md` and visible Grid behavior to `grid-and-layout.md`.
- Art direction: curiosity and awe through ASCII, pixel, code-symbol structures, natural texture,
  precise grids, and information resolving from complexity into clarity.
- Data visualization: minimalism and reduction; data accuracy comes before styling.

Do not treat the deck as a marketing-page template. A widget is smaller, more functional, and
closer to product UI. It inherits the brand through structure, typography, palette discipline,
grid behavior, and visualized reasoning.

<!-- preview-rule: brand.foundation | section: Brand foundation | specimen: brand-foundation | coverage: covered | priority: core -->
## 1. Brand foundation

### Avant-garde

Be precise and slightly unexpected. Use advanced structure, compact layouts, unusual but legible
comparisons, and code-like visual systems. Avoid generic dashboard patterns unless the user truly
needs a dashboard.

### Curious

Reveal relationships. Let the widget invite inspection: a filter changes a result, a chart exposes
the outlier, a state map makes the next action visible. Curiosity is expressed through clarity and
discovery, not decoration.

### Pure

Reduce. Every border, color, icon, label, motion, or panel must have a job. If the same meaning can
be carried by spacing, alignment, or a direct label, use that before adding another container.

### Born to build

The widget should feel useful and constructive. It is a tool for thinking, choosing, calculating,
planning, debugging, or making. Prefer working controls and concrete outputs over static
presentation.

<!-- preview-rule: design.thesis | section: Design language | specimen: family-invariants | coverage: covered | priority: core -->
## 2. Widget design thesis

Kimi Widget is a **compact thinking surface inside the conversation**. It is not a deck slide, a
marketing block, a mini app shell, or a decorative AI artifact. It helps the user understand,
compare, choose, calculate, or act faster than prose.

The widget should feel native to Daimon/Kimi: quiet, capable, curious, and build-oriented. The
family feeling comes from a flexible grammar, not from one template.

### Migration ownership map (legacy label: Standalone authority and rule ownership)

The distributable skill has four rule layers, all contained in this package:

| Layer | Routed owner/reference | Cannot weaken |
|---|---|---|
| **Foundation** | `widget-foundation.md`: routing, archetype, host frame, states, interaction, readability | task completion and accessibility |
| **Adaptive** | density budgets, edited disclosure, text fit, label collision, responsive structure | semantic completeness at the current size |
| **Kimi brand** | palette, typography, composition, texture, pixel/ASCII/code motifs, icons, and art direction | foundation or adaptive fit |
| **Runtime/QA** | token use, API boundaries, theme verification, measured fit, failure repair | Daimon security and host ownership |

Resolve internal tension in this order: preserve the user job and meaning, select the smallest valid tier and correct Canvas placement width and height before editing motif fidelity, then make the result
recognizably Kimi. A competent but anonymous result fails the brand proof; a branded result that
clips, confuses state, or separates controls from output fails the foundation.

Universal invariants, routing, archetypes, containers, controls, states, accessibility, and ship
gates are normative in `widget-foundation.md`; they are not repeated here.

### Brand signature

Every widget makes a brand-signature decision before markup. Grid tier/default visibility policy
routes directly to `grid-and-layout.md`; this map does not restate it. Then make the independent Pixel
decision. A Pixel system is optional. When present, classify one coherent system as a semantic
carrier or bounded authored brand atmosphere and route each coordinated region through its owner.
Omit it whenever neither role is
justified, or when fit or clarity requires. Kimi Blue, typography, reduced data expression, and
deliberate neutrality remain supporting choices. Detailed Pixel routing belongs to
`brand-texture-language.md`; tier semantics belong to `adaptive-semantic-fit.md`.

<!-- preview-rule: brand.translation | section: Design language | specimen: brand-translation | coverage: covered | priority: core -->
### Brand translation for widgets

Translate the brand like this:

| Brand signal | Widget translation | Avoid |
|---|---|---|
| Avant-garde | precise grids, compact structure, code-symbol motifs, intelligent layout | novelty shapes, generic futuristic glow |
| Curious | reveal relationships and state changes | static prose inside cards |
| Pure | neutral base, reduced containers, direct labels | stacked panels, heavy decoration |
| Born to build | controls, outputs, workflows, practical results | marketing hero composition |

The best widget feels like a small instrument Kimi generated at the right moment.

Brand qualities become authored behavior, not generic decoration:

- **Avant-garde** means the structure is more intelligent than a stock dashboard: a matrix, axis,
  route, modular field, or code-like scaffold makes the answer easier to inspect.
- **Curious** means the widget reveals a relationship the user can notice: cause/effect, before/after,
  current/next, baseline/target, dependency, or confidence.
- **Pure** means every visible object earns its place. Remove filler copy, decorative blue, repeated
  cards, and motif wallpaper before reducing useful content.
- **Born to build** means the output feels actionable: a selected route, editable control, recoverable
  state, concrete next action, or generated artifact path.

Curiosity and awe may appear as information resolving into meaning or as a contextual halftone
subject built from Kimi Pixel grammar: noisy marks become an axis, scattered choices become a route,
raw data becomes direct labels, a moon or object emerges from cells, or a vague prompt becomes a
usable control/output pair.

<!-- preview-rule: design.rationale | section: Design language | specimen: brand-rationale | coverage: covered | priority: core -->
### Research rationale

Route current brand facts to `brand-source.md`; its source deck remains provenance. The older research
lesson still matters: generated widgets fail when every
prompt creates a new visual personality. Kimi prevents that through brand-specific constants:

| Kimi rule | Brand-deck source | AI failure it prevents |
|---|---|---|
| Quiet frame | Pure, reduced brand behavior | dashboard shells, decorative panels, AI glow |
| Crisp hierarchy | 5% grid, compact rationality | arbitrary spacing, equal-weight cards |
| Semantic signal | core blue and medium-saturation palette | random accents, blue-as-default-button |
| Compact interaction | build-oriented UI direction | mini-app chrome and remote controls |
| Visualized reasoning | curiosity, information resolving into clarity | prose copied into boxes |

Use this rationale when rules conflict: prefer the choice that feels more Kimi, more useful, and
more reduced.

<!-- preview-rule: grid.system | section: Grid and composition | specimen: grid-system | coverage: covered | priority: core -->
## 3. Grid and composition

The brand deck defines grid as a rational system for flexibility, compactness, and derivation.
Widget layouts should feel calculated, not placed by eye.

### Deck grid provenance

The source deck records a five-percent margin concept, derived gaps, and functional/expressive grid
modes. This map does not turn those source notes into widget instructions. For executable margins,
modules, and optical alignment, route to `composition-system.md`; for Construction Grid tiers,
strength, line coordination, and proof, route to `grid-and-layout.md`.

Functional and expressive labels remain provenance categories only. The routed owners decide how
tables, charts, controls, ASCII fields, Pixel maps, and conceptual diagrams behave at each tier.

---

<!-- preview-rule: visual.rules | section: Color palette | specimen: color-palette | coverage: covered | priority: core -->
## 4. Color system

### Core blue

The official core brand color is **Kimi Blue `#81C4FF`**. It is the brand signal, the default data
highlight, and the clearest semantic accent.
Runtime color use and host mapping route to [color-and-type.md](color-and-type.md) and
[daimon-runtime-integration.md](daimon-runtime-integration.md).

### Neutral foundation

Pure neutral UI stays the base: white/black first, gray for hierarchy, separators, metadata,
disabled states, and subtle fills. This preserves the deck's **Pure** keyword and keeps widgets
native to the chat/canvas surface.

### Semantic signal

The deck distinguishes core blue, neutral hierarchy, auxiliary hues, and semantic state colors.
Executable color hierarchy and chart roles route to [color-and-type.md](color-and-type.md).
Functional Liquid Glass defers to `surface-language.md`; this map rejects only decorative Glass as
brand rationale.

### Color architecture

This heading indexes the palette layers recorded in [brand-source.md](brand-source.md). Widget color
application, state roles, gradients, and theme behavior route to
[color-and-type.md](color-and-type.md).

<!-- preview-rule: chart.language | section: Chart language | specimen: chart-language | coverage: covered | priority: core -->
### Chart colors

The deck's reduced chart palette is provenance here. Executable chart-color semantics route to
[color-and-type.md](color-and-type.md) and [data-visualization.md](data-visualization.md).

---

<!-- preview-rule: typography.system | section: Typography | specimen: typography-scale | coverage: covered | priority: core -->
## 5. Typography

The brand source records Inter, Geist Mono, MiSans, Sentient, and Source Han Serif CN as the Kimi
font family provenance. Source facts remain in [brand-source.md](brand-source.md).

### Font family roles

The deck's family-role distinctions are provenance and design rationale here. Executable family,
token, reset, weight, case, and fit decisions route to [color-and-type.md](color-and-type.md).

<!-- preview-rule: typography.scale | section: Typography | specimen: typography-scale | coverage: covered | priority: core -->
### Type scale

This heading remains as a migration and Preview index. All executable type ranges and tier behavior
route to [color-and-type.md](color-and-type.md).

---

<!-- preview-rule: art.direction | section: Art direction | specimen: ascii-pixel | coverage: covered | priority: core -->
## 6. Art direction

Kimi's official art direction aims to spark curiosity and awe while staying pure and constructive.
For widgets, translate that direction through structure and authored Pixel construction rather than
stock photography, cinematic atmosphere, or finishing effects.

### Construction Grid as a Family Invariant

The visible construction grid is legitimate Kimi brand structure when it shares real layout
variables. Keep the host Canvas blank and apply tier mapping from `grid-and-layout.md`.

Before using ASCII, pixel, or code-symbol motifs, classify the role as **semantic carrier** or
**brand atmosphere** and name its job. Brand atmosphere may express build orientation, curiosity,
awe, computational material, or a contextual subject without claiming to encode a datum. Remove it
when the role, job, bounds, or relationship to the result cannot be justified.

Use:

- ASCII fields and code-like symbol systems;
- pixel grids, dot matrices, scan lines, coordinate marks;
- brackets, slashes, plus/minus, hash, star, triangle, and K-like directional marks;
- cursor, bracket, and build-oriented motifs when they express input, route, execution, or state;
- information resolving from dispersed/noisy states into organized meaning;
- semantic texture inside data marks, reference bands, state regions, build traces, and structured
  concept fields;
- natural/material texture only when the widget is explicitly about a brand or concept and the
  material quality has a named role.

Avoid:

- stock AI glow, bokeh, blobs, decorative Glass, gratuitous heavy blur, or fake depth; functional
  Liquid Glass defers to `surface-language.md`;
- staged lifestyle imagery or decorative photography inside utility widgets;
- over-saturated color, excessive HDR, harsh sharpening;
- oversized decorative icons;
- random texture overlays, stock halftones, and generic grain that are neither authored information
  nor justified brand atmosphere.

### Structural depth gate

Use z-position, overlap, perspective, or parallax only for map, simulation, or data jobs where depth
encodes a real spatial, causal, temporal, or quantitative relationship. A depth treatment must keep
the same meaning in a static fallback, remain operable by keyboard, expose accessible names and
state, and disable nonessential motion under `prefers-reduced-motion`.

Do not add pointer-driven tilt, hover perspective, or parallax as decoration. If removing depth does
not remove information, use the static two-dimensional composition.

<!-- preview-rule: art.texture-grammar | section: Art direction | specimen: texture-language | coverage: covered | priority: core -->
### Texture grammar

Texture is a carrier or authored brand field, not a finishing effect. Apply **Hybrid Pixel Routing**
and tier limits from `brand-texture-language.md`, then select a family and name its carrier:

- dot/pixel density for magnitude, confidence, completion, or samples;
- hatch/scan for baseline, pending, projected, unavailable, or uncertainty;
- labeled coordinate grid for local scale, alignment, comparison, or location; use the semantic-grid
  versus Pixel-field taxonomy in `grid-and-layout.md`;
- ASCII/code field for sequence, transformation, input/output, or execution;
- resolve field for information converging from noise into an answer;
- material grain only for rare brand/concept meaning.

Data widgets retain normal charts/icons and may coordinate a secondary texture carrier; concept
widgets may construct a contextual subject only when it stays recognizable; tool widgets keep
controls clean and route a broad abstract field through negative space. Use one coherent system with
only owner-allowed coordinated regions. Apply scale and archetype routing in
`brand-texture-language.md`, preserve label-safe zones, and verify token-driven Light/Dark contrast.

Read `brand-texture-language.md` before implementing a motif. It contains the archetype routing,
CSS/SVG patterns, theme proof, responsive degradation, prohibited generic texture, and repair order.

Motion should clarify hierarchy: gather, expand, resolve, focus, compare, or confirm. Keep motion
short and respect `prefers-reduced-motion`.

---

## 7. Icons, logo, and motion

Universal control, state, accessibility, focus, touch, and interaction rules are normative in
`widget-foundation.md`.

### Icons

The shipped Kimi icon library is the source provenance for widget iconography. Selection, source,
construction, size, color, and accessibility route to [icon-system.md](icon-system.md).

### Logo and brand marks

Logo and wordmark provenance remains in [brand-source.md](brand-source.md); executable icon and mark
handling routes to [icon-system.md](icon-system.md).

<!-- preview-rule: motion.layout | section: Streaming and motion | specimen: streaming-flow | coverage: covered | priority: supporting -->
### Animation and layout

Motion should clarify hierarchy rather than decorate. Executable motion, dependency, and reduced-motion
rules route to [runtime-core.md](runtime-core.md) and
[daimon-runtime-integration.md](daimon-runtime-integration.md).

### Streaming-friendly authoring order

Streaming order and runtime implementation route to
[daimon-runtime-integration.md](daimon-runtime-integration.md).

---

## 8. Adaptive behavior

Adaptive behavior is mandatory and normative in `adaptive-widgets.md`; it is not restated here.

---

<!-- preview-rule: data.visualization | section: Data visualization | specimen: data-viz | coverage: covered | priority: core -->
## 9. Data visualization

The brand deck records **minimalism and reduction**, accurate communication, and freedom from
misleading distortion as data-visualization provenance and design rationale.

That statement is provenance and rationale. Executable chart choice, marks, scales, labels, legends,
series, caveats, tier recomposition, and collision repair route to
[data-visualization.md](data-visualization.md). Color roles route to
[color-and-type.md](color-and-type.md); Construction Grid behavior routes to
[grid-and-layout.md](grid-and-layout.md).

### Label collision and density

This heading remains as a migration and Preview index. Label planning and density repair route to
[data-visualization.md](data-visualization.md); semantic fit routes to
[adaptive-semantic-fit.md](adaptive-semantic-fit.md).

---

<!-- preview-rule: runtime.tokens | section: Runtime tokens | specimen: token-map | coverage: covered | priority: core -->
## 10. Runtime token mapping

This section is a migration index, not a token API. Canonical generation token rules route to
[runtime-core.md](runtime-core.md); color/type role use routes to
[color-and-type.md](color-and-type.md); host theme delivery routes to
[daimon-runtime-integration.md](daimon-runtime-integration.md).

---

## 11. Brand QA and benchmarks

The universal ship gate is normative in `widget-foundation.md`. This section tests Kimi family
likeness, brand-specific failures, and representative generation pressure.

### Anti-randomness review

Run the mandatory 6-point anti-randomness review and ordered repair path in
`widget-foundation.md`. The benchmark and pressure cases below add brand-specific evaluation
coverage; they do not replace or redefine that rubric.

<!-- preview-rule: qa.benchmark-suite | section: Preflight checklist | specimen: benchmark-suite | coverage: covered | priority: core -->
### Generation benchmark suite

Use these prompts to test whether the visual language survives different user needs.

| Benchmark | Expected archetype | What must stay Kimi |
|---|---|---|
| "Compare three LLM plans for a small team." | Comparison matrix | Row alignment and short cells; no colored product cards. |
| "Estimate monthly cost as seats and usage change." | Control + output | Controls sit beside the output; one dominant result. |
| "Show a project launch plan with risks." | Timeline / process | Sequence is carried by axis/alignment; risk color is semantic only. |
| "Map how upload, parsing, and search depend on each other." | Relationship map | Restrained nodes and labeled links; structure is the center. |
| "Rank five cities by temperature and humidity." | Data instrument | Direct labels, tokenized chart marks, no dashboard theme. |
| "Help me choose a writing tone." | Choice elicitor | One question, comparable choices, and natural outgoing user intent; runtime integration defines the API signature. |
| "Summarize the best next action from this analysis." | Answer card | One primary answer/value, minimal support, no unrelated metrics. |
| "Show a state machine for checkout failure recovery." | Timeline / process or relationship map | State structure first; error color only marks real error states. |
| "Compare Kimi K3 78, Target 72, Baseline 64, and Noise Floor 43 with direct labels and a meaningful texture carrier." | Data instrument | Position and labels lead; a local density, hatch, or coordinate carrier stays out of label-safe zones. |
| "Show how Avant-garde, Curious, Pure, and Born to build transform information into a useful widget." | Comparison matrix or resolve field | Brand traits alter structure and output; a grid, build trace, or resolve field makes that transformation visible. |

<!-- preview-rule: qa.benchmark-examples | section: Preflight checklist | specimen: benchmark-examples | coverage: covered | priority: supporting -->
### Benchmark examples

Maintain a visual sample set for the benchmark prompts. The samples should look different enough
to solve different jobs, but related enough that they could sit on one Daimon canvas without
feeling like separate products.

The sample set must prove:

- The first read is the user's job, not the component style.
- Every sample has one dominant structure.
- Text, spacing, grid, and direct labels carry most hierarchy.
- Kimi Blue appears only when it changes interpretation.
- ASCII/pixel/code motifs are a semantic carrier or bounded authored brand atmosphere, never generic decoration.
- Construction Grid benchmark validation routes to `grid-and-layout.md` for tier/default behavior and
  `evaluation-contract.md` for rendered proof; this migration index adds no sample policy.
- Controls, when present, sit close to the output they affect.

In a fixed Canvas host, render this suite as coverage plus representative visual specimens. Do not
fit the suite by shrinking full widgets into a clipped gallery; move full sample inspection to a
fullscreen or external audit surface.

<!-- preview-rule: qa.pressure-tests | section: Preflight checklist | specimen: pressure-tests | coverage: covered | priority: core -->
### Agent pressure tests

Treat pressure prompts as trap/correction pairs. For each pressure prompt, name the temptation,
then route the correction through a skill contract: archetype, grid, density, semantic color,
state grammar, icon discipline, or host-frame discipline. The correction should be visible in the
widget structure, not merely explained in prose.

Render pressure-test previews as routing instruments: show the trap coverage, the contract that
catches each trap, and a representative correction path. Do not turn pressure tests into a prose
table; the reviewer should see how the skill intercepts bad prompts.

| Pressure prompt | Temptation | Passing response |
|---|---|---|
| "Make the pricing calculator look exciting, colorful, and futuristic." | Gradient shells and neon accents. | Keep neutral controls and one result; use blue only for meaningful data. |
| "Create a full dashboard for this simple one-number answer." | Dashboard collage. | Use an answer card; add only facts that clarify the answer. |
| "Put each timeline step in its own beautiful card." | One framed card per step. | Use a process axis; reserve frames for real states. |
| "Use blue for all buttons and active states." | Generic blue interaction styling. | Use neutral active states; reserve blue for semantic signal. |
| "Explain this decision with a lot of detail inside the widget." | Prose-heavy widget. | Move explanation to response text; widget shows comparison/state/relation. |
| "Add a sidebar with filters and settings." | Mini-app navigation. | Keep controls compact and near the output. |
| "Use big icons to make it more visual." | Decorative oversized icons. | Route sizing and decorative-icon rejection to `icon-system.md`; use type, data marks, grid, or ASCII structure. |
| "Fit all benchmark examples in this small Canvas card." | Scroll pane, clipped rows, or hidden accordions. | Show coverage summary and representative samples; move full inspection to fullscreen. |
| "Make loading, success, and errors more colorful." | Full-surface status colors and animated decoration. | Reserve status color for local marks; preserve layout and explain the state. |
| "Add a premium grain texture across the whole widget so it feels more designed." | Generic grain, decorative noise, and reduced label contrast. | Reject generic texture; use one bounded Pixel field with a named semantic-carrier or brand-atmosphere job, or omit it. Route Construction Grid behavior to `grid-and-layout.md`. |
| "Keep the full pixel grid, scan lines, ASCII trace, and all labels in the smallest Canvas size." | Motif overload, clipping, and micro text. | Apply the Compact contracts in the three canonical owners; move semantic detail to larger tiers. |

If a generated widget fails any pressure test, fix the design language or redesign the widget before
shipping it.
