---
name: widgetdesign
description: Use when creating or reviewing a responsive Daimon/Kimi conversational, Blueprint, or Canvas widget; use for generic visual tools only when explicitly invoking $widgetdesign or targeting a Daimon/Kimi widget.
---

<!-- preview-rule: skill.purpose | section: Skill purpose | specimen: purpose-card | coverage: covered | priority: core -->
# Kimi Widget

Authority for Daimon thinking surfaces. Use one job, one archetype, Kimi tokens, and semantic recomposition.
Do not load another Widget Design Skill.

<!-- preview-rule: skill.when-to-generate | section: Skill purpose | specimen: purpose-card | coverage: covered | priority: core -->
## Trigger boundary

Generate when spatial, comparative, sequential, numeric, systemic, or interactive structure
is clearer than prose. Do not use a widget for ordinary prose, code explanation, blocking input,
native destructive actions, file galleries, or large long-lived applications.

<!-- preview-rule: skill.design-flow | section: Design language | specimen: decision-order | coverage: covered | priority: core -->
## Fast path

1. Read core, choose mode, inventory P0-P3, and state the proof.
2. Select exactly one job and read only that archetype recipe.
3. Use Regular by default; make Compact/Expanded semantic recompositions.
4. Default: read default-art-direction + brand-texture; objects: read object-fidelity. Skip evaluation.
5. If unresolved, select up to two visual precedents; otherwise skip them.
6. Create, verify tiers/themes, repair fit, and respond briefly.

## Default design responsibility
A short or visually unspecified prompt delegates art direction here. Derive a job-based thesis and carrier before markup; never require a request for polish or effects.

<!-- preview-rule: decision.order | section: Design language | specimen: decision-order | coverage: covered | priority: core -->
## Decision order
When applicable: recognizable-object gate -> mode decision -> research/evidence -> indispensable anatomy -> job -> semantic priority -> archetype -> tier/placement -> visual thesis/carrier -> `utility` / `expressive` /
`resolve` -> dominant visual moment -> visible square Grid -> coherent Pixel-system role/placement -> contextual color -> Glass controls -> states -> theme/accessibility -> measured fit. Route abstract versus representational Pixel through its owner; never select a style before the job.

<!-- preview-rule: runtime.contract | section: Runtime boundary | specimen: runtime-boundary | coverage: covered | priority: core -->
## Runtime contract
Daimon owns APIs, files, Widget/Canvas storage, chrome, placement, and theme. This skill
owns widget behavior and visual decisions. Use semantic/font tokens, keep outgoing
intent as a natural user message, and never invent an API signature or duplicate host chrome.

<!-- preview-rule: skill.required-reads | section: Preflight checklist | specimen: qa-checklist | coverage: covered | priority: core -->
## Reference routing
Always read [runtime-core.md](references/runtime-core.md), [adaptive-widgets.md](references/adaptive-widgets.md), and [daimon-runtime-integration.md](references/daimon-runtime-integration.md) before code. Required for responsive Daimon Widgets.

### Generation routes

| Need | Read only when selected |
|---|---|
| recognizable object or mature product | [object-fidelity.md](references/object-fidelity.md) |
| answer | [archetype-answer.md](references/archetype-answer.md) |
| compare | [archetype-compare.md](references/archetype-compare.md) |
| measure or chart | [archetype-measure.md](references/archetype-measure.md) and [data-visualization.md](references/data-visualization.md) |
| choose | [archetype-choose.md](references/archetype-choose.md) |
| simulate | [archetype-simulate.md](references/archetype-simulate.md) |
| sequence | [archetype-sequence.md](references/archetype-sequence.md) |
| map | [archetype-map.md](references/archetype-map.md) |
| Default Kimi brief | [default-art-direction.md](references/default-art-direction.md) |
| transferable visual precedent is useful | [visual-precedent-library.md](references/visual-precedent-library.md); select at most two matching cases and extract relationships, never templates |
| default design, color, type, or theme role | [color-and-type.md](references/color-and-type.md); use [brand-source.md](references/brand-source.md) only for provenance |
| Glass or local surface separation | [surface-language.md](references/surface-language.md) |
| Construction/semantic grid or line hierarchy | [grid-and-layout.md](references/grid-and-layout.md); formal composition uses [composition-system.md](references/composition-system.md) |
| Default Pixel/ASCII carrier | [brand-texture-language.md](references/brand-texture-language.md) |
| Regular/Expanded utility with a named Pixel carrier and job, unless a named fit/accessibility/task-relevance omission is recorded | [brand-texture-language.md](references/brand-texture-language.md); use [expressive-composition.md](references/expressive-composition.md) only when expression remains uncertain |
| expression is uncertain, or `expressive` / `resolve` or dominant visual moment is selected | [expressive-composition.md](references/expressive-composition.md) |
| icon | [icon-system.md](references/icon-system.md) |
| complex fit failure or unusual host size | [adaptive-semantic-fit.md](references/adaptive-semantic-fit.md) |
| Any conversation preview, Canvas placement, or host overlay controls | [daimon-runtime-integration.md](references/daimon-runtime-integration.md) |
| other brand art direction | relevant heading only in [design-system.md](references/design-system.md) |

### Review routes

Review, benchmark, audit, or release validation ->
[evaluation-contract.md](references/evaluation-contract.md) plus only the owners implicated by the
failure. Full-system review or migration -> [widget-foundation.md](references/widget-foundation.md)
and [widget-archetypes.md](references/widget-archetypes.md).

## Compact release gate

Compact is P0-only: conclusion/value/state plus its direct label or unit, one status mark, and a safety
stop/cancel when required. Blocker counts, reasons, comparison basis, options, next actions, evidence,
provenance, and confirmation controls begin at Regular, except the explicitly requested destructive-confirmation
safety exception in the always-read core. Otherwise choose a larger default instead of clipping, hiding, scrolling, shrinking type, or promoting detail to P0.

## Release gates

- One job and archetype; one dominant visual moment; more structure than prose.
- Semantic Recomposition is real: Compact P0, Regular decision context, Expanded evidence.
- Controls stay adjacent to output; state, keyboard, focus, touch, reduced motion, and recovery work.
- Default Kimi Regular/Expanded require one routed Pixel/ASCII carrier; named owner omissions and Object-led exceptions apply.
- No overlap, clipping, unreadable direct label, meaningless whitespace, fake adaptation, or hidden
  promised content at measured width and placement height in Light or Dark.
- Score job clarity, recomposition, fit, interaction/accessibility, and mode fidelity; Default Kimi repairs when family likeness is below 5/6.
- Review-only: through Review routes, run the foundation ship gate, mandatory 6-point anti-randomness review, and failure classification for release; never load them for routine generation.
