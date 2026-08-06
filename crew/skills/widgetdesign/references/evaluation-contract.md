# Evaluation Contract

## Object-led evaluation

For a named object/product/style, score object recognizability, useful affordances, craft, responsive
recomposition, accessibility, and runtime correctness. Object-led mode does not require Kimi likeness,
visible Grid, interactive ASCII/Pixel, or canonical Glass. Fail when the result loses the requested
object character or reuses the same default Kimi composition seen across unrelated widgets. When the
user explicitly requested Kimi styling, score the selected Kimi treatment separately without allowing
it to overwrite the object skeleton.

## Art-direction compiler evaluation

For every visually unspecified Default Kimi prompt, inspect the internal brief and generated artifact
together. Require a named semantic phenomenon and at least two semantic signal-to-visual mappings
when the selected carrier is semantic. If the selected carrier is brand atmosphere, require a named
atmosphere job and verify that it does not falsely claim to encode data. Pointer response, motion,
Grid, ASCII/Pixel, Glass, and color must implement their selected semantic or atmospheric role rather
than appear as generic polish. Fail when a detailed evaluator-authored Prompt is required to obtain
task-specific composition, when a semantic field survives an unrelated title swap, or when reduced
motion loses the phenomenon's direction, magnitude, state, or P0.

Fail caption-only ASCII when a value appears in a nearby label but never enters field geometry or
renderer configuration. Run the counterfactual signal test with the title held constant; at least one
geometry channel must visibly respond to the changed source value. Generic hard-coded mark counts,
sine waves, scatter, or pointer repel do not become semantic because their caption names a datum.

Fail a named empty visual region: a heading such as cadence, signal, flow, spectrum, pulse, or field
must have a visible carrier in the same region at Regular and Expanded. A construction Grid, blank
rectangle, or large reserved area beneath the label is not a carrier. If data is unavailable, the
widget must either use a clearly labeled illustrative sample or omit the named region entirely.

Fail component-only Pixel substitution: a Pixel-styled table, chart, calendar, badge, icon, or small
component cannot stand in for the required broad Default Kimi background/negative-space field. At
Regular and Expanded, the primary field must read as an environment at 100% Canvas zoom, occupy the
owner-defined footprint, remain behind label-safe content, and preserve optional pointer response.

## Zero-art-direction suite

Run the prompts in `docs/default-generation-prompts.json` without adding visual adjectives, tier
instructions, effects, palette requests, or implementation hints. This suite measures whether the
skill owns art direction when the user only names a job or object.

For each result, inspect Compact, Regular, and Expanded in Light and Dark. Fail prompt dependence when
the result becomes visually resolved only after the evaluator adds Grid, Pixel, ASCII, Glass, color,
motion, layout, or typography instructions. Also fail a primarily textual result on an empty surface,
an interchangeable generic card, default-blue styling without a named role, or a Regular composition
with no subject-, data-, state-, or interaction-derived visual carrier.

Record which missing rule caused the failure: default routing, visual thesis, carrier selection,
color decision, tier presentation, object fidelity, implementation compliance, or fit. Patch the Skill
first when the decision is absent; patch the generated implementation when the rule was present but
ignored.

Fail a framed outer stage when a root-sized or nearly full-footprint wrapper adds its own border,
rounded corners, shadow, and inset margin around the whole widget. The host owns that silhouette.
Grid or a host-matching base field may reach open edges; only smaller job-specific regions may be
bounded.

Render conversation previews and Canvas placements with host controls visible, not only in their
hidden resting state. Fail a top-right safe-zone collision when any host chrome covers a title, P0, unit, status, direct
label, legend, caveat, authored control, focus target, or pointer-critical visual content. Background
Grid/Pixel/image material may pass behind the controls only when its meaning survives occlusion.
Repeat with hover, keyboard focus, menu-open, saved, DEV, and touch/coarse-pointer states, then verify
that a no-overlay full-result releases the reservation. A clean resting screenshot alone cannot pass.

For each eligible Regular/Expanded result, verify a visible square Grid with equal horizontal and
vertical spacing, one task-derived interactive ASCII field with reduced-motion fallback, and canonical
Glass classes on every authored panel and control. A named boundary may omit Grid or ASCII, but the
review must record fit, accessibility, object recognition, task relevance, or performance. Fail dense
horizontal scan lines, static generic dots where ASCII interaction fits, ordinary white/gray panels,
   browser-default buttons, or local Glass recipes that bypass the bundled asset. For the optional
   `Glass backdrop field` role, fail an adjacent-only backdrop: the task-derived field must cross
   behind the panel, and at least one Light-mode backdrop cue must remain perceptible through the
   material at 100% zoom. For `open expressive field` and `Pixel display` roles, do not require panel
   overlap; evaluate the field in its own open region or P0 display region instead. Faint Grid on a
   neutral white surface is not sufficient evidence for a selected Glass backdrop.

This reference defines how canonical widget output is judged. Evaluation tests the skill's decisions
and the preview's mapping to them; it does not make preview CSS a hidden dependency of the skill.
Use `adaptive-semantic-fit.md`, `grid-and-layout.md`, and `brand-texture-language.md` as the contract
owners; this file defines samples and failure evidence rather than restating their mappings.

<!-- preview-rule: vnext.evaluation-contract | section: Preflight checklist | specimen: build-readiness | coverage: covered | priority: core -->
## Evaluation surface

Treat the team preview as an evaluation surface: each widget should look like a result generated by
following this package alone. Use a blank Daimon-like host Canvas and keep visible grids inside each
widget boundary. In a multi-widget preview, Anchor Mosaic may establish one anchor widget per topic,
place related widgets by reading order and content weight, and allow at most one strong visual widget
per viewport. This curation rule does not change an individual widget's runtime contract.

Evaluate default size before resize variants. A default passes when its width and placement height
follow minimum viable semantics, all promised content is visible, whitespace reflects hierarchy,
and the dominant visual moment is immediate. Then evaluate Compact, Regular, and Expanded, plus
Light and Dark themes.

For any recognizable object or mature product, score these separately:

- **Object recognizability:** title-free recognition, indispensable anatomy, category affordances,
  operational state, skeleton continuity across tiers, and evidence from inspected sources.
- **Kimi likeness:** token roles, type hierarchy, composition, restraint, and only the routed motifs
  that remain subordinate to recognition and use.

Score these separately. Object recognizability is a release gate: strong Kimi likeness cannot rescue
a generic dashboard, missing anatomy, or a title-dependent object. Kimi motifs may be omitted when
they harm recognition or use without lowering the object score.

## Failure classification

Classify before patching:

| Pattern | Owner to repair first |
|---|---|
| same failure across multiple widgets | skill rule or shared reference |
| one widget fails while peers pass | implementation's mapping to the skill |
| one archetype repeatedly fails | `widget-archetypes.md` contract or benchmark anatomy |
| host background/container differs from Daimon | preview environment |
| widget structure ignores a clear rule | widget implementation |
| only handcrafted preview CSS creates the result | skill is underspecified; strengthen the canonical rule |

Record the violated gate, tier, theme, language/content case, and first repair. Do not compensate for
a skill failure with one-off decoration or for a preview bug by weakening the skill.

Use the repair order in `adaptive-semantic-fit.md` and `brand-texture-language.md`.

## Build Readiness benchmark

Build Readiness is the canonical benchmark for Semantic Recomposition. Its content inventory is:

- **P0:** readiness verdict `Ready` or `Blocked`, completion value, and direct label.
- **P1:** blocking count, passed-check count, and the next required action.
- **P2:** evidence by check, trend/progress detail, warnings, and explanatory status.
- **P3:** provenance, run timestamp, source/version, and audit notes.

The canonical report is expression `utility`, Pixel role `semantic carrier`, and field treatment
`abstract`. A memorable module matrix does not change that routing.

The target anatomy is concrete:

| Tier | Required result |
|---|---|
| compact | P0 verdict and completion value form the single dominant moment; no criterion, next-state context, evidence, caveat, or P1/P2 proof mark; visual allowances match the owners |
| regular | P0 plus P1 counts and next action; construction and Pixel mappings match the owners; essential action remains adjacent |
| expanded | P0 + P1 + P2 with inspectable evidence and trend; richer construction remains within owner mappings and placement |

Use these reproducible composition rules:

- **Compact:** one column. Put the direct label first, then group verdict and completion value on one
  shared baseline as the dominant moment; place the minimal status mark at the trailing edge. The
  value is 1.8-2.4 times body text, never wider than its line, and remaining space closes below it.
- **Regular:** use a 7/5 or 2/1 split on the visible construction grid. The leading region carries the P0 label, verdict, and value;
  the secondary region contains three aligned gate rows plus one next action directly below them.
  A local carrier may occupy only unused space behind or beside the gate region and must encode the
  same passed/blocked ratio. Keep at least one base spacing unit between carrier and text/control.
- **Expanded:** use stable P0, evidence/trend, and owner-defined construction/Pixel spans.
  Evidence rows share a baseline and direct status labels. The P0 value stays the largest type;
  evidence never becomes a second focal chart. Extend the same abstract Pixel system rather than
  adding another motif family.
- **Whitespace:** reserve open space around the P0 group, not between related label/value or
  control/output pairs. Removing P1/P2 must also remove their tracks so smaller tiers never inherit
  hollow regions.

P3 appears only when the expanded anatomy already fits, below the evidence as a quiet provenance
line rather than a new panel. Across tiers, keep the same answer archetype, direct labels, strong type
hierarchy, shared left edge and baseline logic, and no hollow reserved regions. The benchmark fails
for clipped content, decorative pixels, duplicated focal points, or a compact version that is merely
scaled down.

## Canonical visual samples

| Sample | Required proof |
|---|---|
| Build Readiness | `utility`; semantic carrier; abstract Pixel field using dot density for completion/readiness modules; sparse visible construction grid with horizontal rows lighter than vertical modules |
| Lunar / Concept | visible construction grid plus a contextual halftone moon or subject field |
| Compare | grid aligns comparable values; Pixel treatment emphasizes a meaningful difference region |
| Compact stress | P0 survives and any optional visuals satisfy the three canonical owners |
| Light / Dark | the same structure and semantic roles use runtime tokens without separate hardcoded palettes |

Each sample must state the Pixel role as semantic carrier or brand atmosphere, name the field's job,
show its archetype-routed placement, and pass tier scale, label-safe-zone, and coordinated-region proof.
Family likeness must come from shared grammar without repeating one corner pattern.

Capture a 100% Canvas-zoom screenshot in Light and Dark. Fail when a reviewer cannot identify the
repeated construction field and the routed Pixel material without inspecting DOM, CSS, comments, or
accessibility labels. A few section borders do not count as a construction background. A few
isolated squares and tiny low-contrast cells that disappear at normal viewing distance do not count
as a Pixel field. For Build Readiness, verify a bounded output-adjacent dot field in Regular and
Expanded when the field branch is selected; for an omission branch, verify a named
fit/accessibility/task-relevance reason rather than accepting silence. Compact remains P0-first and
evaluates Pixel only when an optional field is selected.

Reject a tiny icon-like Pixel cluster used as the main visual, or a Pixel forecast/chart that is less
readable than a standard information graphic. Test one readable P0 Pixel display with equivalent
accessible text, and one Regular/Expanded sample with a broad abstract interactive field plus an
optional coordinated continuation. Both regions must visibly share units, signal logic, motion, and
tokens; unrelated motifs fail coordinated-region proof.

### K3 placement-height regression

Reproduce the case where Expanded notes exist in the DOM but fall outside the visible Canvas
placement. Fail the tier whenever its selected width or **placement height** does not visibly contain
all **promised content**, including notes, evidence, labels, controls, and caveats. DOM presence,
hidden overflow, clipping, or an off-placement bounding box cannot satisfy fit proof.

## Skill-only blind test

Run the blind test without preview source, benchmark CSS, or prior generated markup:

1. Give a model a new widget prompt and only the distributable `widgetdesign` package.
2. Ask for compact, regular, and expanded outputs plus the P0-P3 inventory, selected job/archetype,
   default-size rationale, expression level, dominant visual moment, and grid/carrier proof.
3. Render all tiers on the blank host Canvas in Light and Dark.
4. Repeat with Chinese, English, long text, and numeric-heavy content, including large signed values,
   decimals, units, and localized separators.
5. Score semantic tier correctness, archetype continuity, fit, dominant-moment singularity,
   construction proof, Pixel-system role/locality/coordination, tier scale, label safety, accessibility, and the
   mandatory 6-point family likeness.

Pass only when every P0 and every item promised by the selected tier is visible within the placement,
each tier honors its priority contract, no primary text truncates, all fit checks pass, and family
likeness scores at least 5/6. If a result needs knowledge of preview code to pass, update the relevant
canonical reference and rerun the isolated test.

For Compact, inspect both the manifest and rendered text. Fail immediately if the manifest classifies
a blocker count, criterion, comparison basis, alternate option, tradeoff, reason, next-state context,
next action, evidence/proof mark, caveat, or control as P0; if its disclosure claims P1/P2; or if any
such item appears in the rendered output. Fit alone cannot pass a semantically over-disclosed Compact
widget. Optional visuals must pass the Compact contracts in `grid-and-layout.md` and
`brand-texture-language.md` after the semantic allowlist.
