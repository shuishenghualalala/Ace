# Adaptive Semantic Fit

This reference is the canonical owner of information priority and Semantic Recomposition. Use
`adaptive-widgets.md` for host dimensions, CSS fit mechanics, height, scrolling, and theme checks.

<!-- preview-rule: vnext.semantic-fit | section: Adaptive behavior | specimen: adaptive-tiers | coverage: covered | priority: core -->
## Priority model

Inventory all content before choosing dimensions:

| Priority | Definition | Examples | Presence |
|---|---|---|---|
| P0 | task conclusion, key value, core state, or selected choice | readiness result, total, current status, chosen option | every tier |
| P1 | context required to understand or act on P0 | direct comparison basis, threshold, next action, primary control | Regular and Expanded |
| P2 | evidence, trend, explanation, secondary data/state/control | chart detail, reasons, supporting rows, confidence | Expanded |
| P3 | supplemental note, source, provenance, audit detail | methodology, timestamp, low-priority metadata | only when space remains |

Minimum viable semantics means P0 plus any direct label, unit, or threshold without which P0 would
be misleading. These dependencies share P0 **visibility**, not P0 priority. A control, next action,
comparison basis, or explanatory context remains P1 even when important; if the user cannot complete
the job without it, the default tier is at least Regular. Only a safety-critical stop/cancel control
may accompany P0 in Compact, and it does not make Compact the default. Derive the default size from
the smallest tier that contains the original priorities completely. Never promote content to P0 to
justify a smaller box, set a fixed default size first, or trim meaning to fit it.

## Semantic Recomposition

Enter motif recomposition only after restoring the user job, selecting the smallest valid tier, and
correcting Canvas placement width and height for all promised content. When a valid placement then
contracts, preserve meaning in this order:

1. Rewrite long text into shorter, semantically complete language.
2. Convert prose into direct labels, values, states, or visual marks.
3. Change information structure, such as table to ranked rows or chart to value-plus-delta.
4. Reorder so P0 and its direct label lead.
5. Lower disclosure: move P2/P3 to a larger tier rather than hiding it in place.
6. Reduce Pixel footprint, cells, coordinates, and contextual detail for the selected tier.
7. Remove the Pixel field when fit still fails, then remove secondary construction-grid lines or
   Compact grid fragments.
8. Only then omit P2 and P3 content that the current tier does not contractually include.

Do not solve fit by continuously shrinking type. Preserve one job and one archetype across tiers;
the structure may recompose, but the answer and interaction model remain recognizable.

## Tier contracts

| Tier | Required anatomy | Allowed additions |
|---|---|---|
| Compact | P0 only, with its direct label/unit and any critical state | visual allowances only after the audit; use `grid-and-layout.md` and `brand-texture-language.md`; safety stop/cancel only |
| Regular | P0 + P1, complete decision context, essential controls | use tier visuals from `grid-and-layout.md` and `brand-texture-language.md` |
| Expanded | P0 + P1 + P2, evidence/trend/detail that explains the result | use tier visuals from `grid-and-layout.md` and `brand-texture-language.md`; P3 only if all required content fits |

Compact is P0-only. Complete the semantic audit here before applying the optional visual allowances
owned by `grid-and-layout.md` and `brand-texture-language.md`. Neither allowance may carry, label, or
imply P1-P3.

Before rendering Compact, run a **presence audit** over every visible datum and interactive element.
Each item must be exactly one of: P0, P0's direct label or unit, one non-numeric status mark, or a
safety stop/cancel control. A blocking count, comparison basis, option set, next action,
confirmation control, explanatory sentence, or evidence row remains P1 or P2 and must be absent.
Do not relabel these items as "critical state," "archetype continuity," or "minimum context" to keep
them in Compact. If any is necessary to complete the job, choose Regular as the minimum default.

Use this mechanical denylist when auditing Compact: blocker/open-gate count; blocker identity;
comparison basis; preference or criterion; alternate option or tradeoff; evidence row; reason;
next action; confirmation control; source or provenance; explanatory sentence. A verdict may state
`Hold`, `Blocked`, or `Specialist`, but the reason, count, alternatives, and decision criterion remain
P1-P3. The Compact disclosure manifest must say `P0 only`; any `P0 + P1` or `P0-P2` claim fails.

Run visual fit only after that content audit. Optional allowances from the grid and texture owners do
not change the `P0 only` disclosure claim and cannot smuggle in P1-P3 data, labels, controls, or
reasons. After the selected tier and Canvas placement width/height pass fit, remove the Pixel option
first and then the grid fragment whenever P0 fit, contrast, or clarity suffers.

Width alone does not select a tier. Use width, height, content density, line count, controls, and
archetype geometry. Derive the default Canvas placement from the smallest tier whose width and
**placement height** visibly contain all **promised content**. DOM presence does not count when a row,
note, label, control, or caveat sits outside the visible placement. An Expanded container with little
content must not preserve an empty large template: tighten the default size or use whitespace to
clarify a real dominant relationship.

## Forbidden fake adaptation

Reject a tier if it relies on any of these:

- scaling the whole widget or steadily shrinking all fonts;
- cropping, clipping, or `overflow: hidden` to conceal semantic content;
- `ellipsis` on the primary title, key value, core state, direct label, core chart label, or critical
  control;
- packing P0-P3 into Compact;
- hiding rows, controls, or sentences behind accidental overflow;
- retaining a large shell after evidence has been removed;
- changing jobs or dominant archetypes between tiers.

Truncation may be used only for a nonessential secondary identifier when its identity remains clear
and full access is available under the runtime contract.

## Fit verification

Test Compact, Regular, and Expanded with real content. A tier passes only when:

1. required priorities match its contract and minimum viable semantics remain complete;
2. no title, value, label, chart mark, state, or control overlaps, clips, or escapes its box;
3. P0 is readable first and no primary text uses ellipsis;
4. all rendered rows and controls are fully visible at measured width and height;
5. long text wraps to an intentional line budget; Chinese and English keep the same meaning;
6. large numeric values, signs, decimals, units, and localized separators fit without glyph scaling;
7. removing P2/P3 in smaller tiers closes space rather than leaving a hollow template;
8. the tier remains usable in Light and Dark themes and under text expansion;
9. every item promised by the tier is visible within the selected Canvas placement height, including
   notes or evidence that exist in the DOM.

On failure, restore the user job and dominant archetype, reselect the smallest valid tier, and correct
Canvas placement width and height before rerunning Semantic Recomposition. Degrade or remove the
Pixel field and construction-grid lines only after that tier and placement fit check passes.
