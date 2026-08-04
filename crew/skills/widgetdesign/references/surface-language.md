# Surface Language

Canonical owner of local Kimi surface depth and Liquid Glass. Glass is the required material for any
authored panel or control, but it is not a reason to invent a panel or control the job does not need.

## Surface decision

Use an open/inherited widget surface first. Add a bounded panel only when it does one of these jobs:

1. separates a critical output or dense label zone from a meaningful Construction/Pixel field;
2. groups controls with the output they affect;
3. presents a floating overlay, transient inspector, or focus-sensitive tool region;
4. preserves readable hierarchy over a changing host-provided background.

If no panel or control is needed, spacing, alignment, and separators remain sufficient. In Default
Kimi mode, any authored
panel must use `.kimi-glass kimi-glass-panel` from `assets/styles/kimi-glass.css`. Every button, icon
button, slider, and segmented control must use its matching canonical class: `.kimi-glass-button`,
`.kimi-glass-icon-button`, `.kimi-glass-slider`, or `.kimi-glass-segmented`. This includes selected,
disabled, and destructive controls; semantic foreground/state tokens still carry their meaning.

### Glass footprint gate

Decide whether a region is eligible before assigning any Glass class. A container that includes the
primary chart, long list, table, map, or most of the widget content is not an eligible Glass panel.
Keep those structures on the open/inherited surface with grid alignment, spacing, separators, and
direct labels. A valid Glass region contains one bounded semantic cluster: P0 plus a short reason,
controls plus their immediate output, or a transient inspector. At least one major content region
must remain outside the Glass footprint in Regular and Expanded.

When a field is placed beneath Glass, it is supporting material, never the only readable copy of a
chart or data encoding. If marks must be inspected, compared, or directly labeled, render them above
or outside the blur. Do not place a complete visualization behind Glass and call the resulting blur
a backdrop. Pixel placement is chosen by the Pixel role; Glass does not claim every Pixel Field.

Never make every section a panel, nest Glass inside Glass, or combine Glass with card stacks. Use one
backdrop-filter owner: the panel owns blur; controls inside it use Glass surface, border, highlight,
lowlight, and shadow tokens without another blur layer. A control may set
`data-kimi-glass-standalone="true"` only when no Glass panel contains it.

## Pixel / Glass layer contract

When a Default Kimi widget uses both a Pixel Field and a Liquid Glass panel, choose the Pixel role
before choosing the layer. Pixel may occupy open space outside the panel, form a P0 Pixel display,
or act as a local Glass backdrop. Only the third role uses the following under-panel stack:

1. Put both inside one `.kimi-glass-stage`.
2. Render the Pixel Field as `.kimi-glass-backdrop` or `[data-kimi-glass-backdrop]` at layer `0`.
3. Render the panel as `.kimi-glass kimi-glass-panel` at layer `1`, so its translucent surface samples
   the field beneath it and the field remains perceptible through the Glass.
4. Keep the backdrop `pointer-events: none`; route pointer interaction through the stage or panel
   using `pointerTarget`, never by placing the Pixel canvas above controls.
5. Let the field cross the panel footprint when it has a meaningful task cue. A Pixel Field that sits
   only beside the panel is invalid only when it was explicitly chosen as the Glass backdrop role;
   an expressive open-space field is valid and should remain outside the panel.

This optional layering rule applies to abstract background, atmosphere, flow, density, contour, and
resolve fields only when the backdrop role is selected. A chart, direct label, threshold, map marker,
or other inspectable information graphic must remain above or outside the Glass when the user needs
to read or compare it; do not hide a complete visualization behind blur. In Compact, reduce or
remove a backdrop after P0 fit, but keep the same panel material and layer order when the panel remains.

Do not place the Pixel Field above the panel, put controls inside a pointer-blocking Pixel canvas, use
an opaque panel that hides the backdrop, or treat a randomly moving field as a sufficient backdrop.

Object-led panels and controls may use object-native material, geometry, and state treatment instead
of Glass. They still keep familiar affordances, readable contrast, focus, disabled/error states, and
the Compact performance fallback. Do not add Glass merely to make Object-led output look Kimi.

## Tokenized Glass roles

The Daimon host owns the canonical `--kimi-glass-*` values. Widget CSS consumes these tokens and
must not redefine them with weaker local aliases. This keeps chat, Canvas, Light, and Dark output
consistent while preserving one literal approved material recipe.

| Role | Required source |
|---|---|
| surface | `--kimi-glass-surface` |
| border | `--kimi-glass-border` |
| highlight | `--kimi-glass-highlight` |
| lowlight | `--kimi-glass-lowlight` |
| shadow | `--kimi-glass-shadow` |
| blur | `--kimi-glass-blur` |
| text/state | ordinary semantic text and state tokens, never inherited translucent color |

Light and Dark share the same role names and material geometry. The host resolves colors; the widget
does not branch or copy theme values.

### Light material contract

The host values are exact and are not starting points for interpretation:

| Role | Light value |
|---|---|
| surface | `rgba(255, 255, 255, 0.4)` |
| border | `rgba(255, 255, 255, 0.3)` |
| highlight | `rgba(255, 255, 255, 0.8)` |
| lowlight | `rgba(255, 255, 255, 0.1)` |
| shadow | `rgba(0, 0, 0, 0.1)` |
| blur | `5px` |

Use this exact widget CSS after the stable fallback:

```css
.glass-surface {
  background: var(--kimi-glass-surface);
  backdrop-filter: blur(var(--kimi-glass-blur));
  -webkit-backdrop-filter: blur(var(--kimi-glass-blur));
  border: 1px solid var(--kimi-glass-border);
  box-shadow:
    0 8px 32px var(--kimi-glass-shadow),
    inset 0 1px 0 var(--kimi-glass-highlight),
    inset 0 -1px 0 var(--kimi-glass-lowlight);
}
```

Use the same material geometry at every tier. If the result feels strong in isolation, fix the local
backdrop, region size, or content hierarchy instead of creating a second Compact recipe.

### Dark material contract

Dark keeps `5px` blur, `0 8px 32px` outer-shadow geometry, and both one-pixel inset edges. Only the
host-provided color and alpha values change for contrast. Never add an independent dark Glass recipe
inside a widget.

Glass must preserve contrast for primary text, chart labels, direct labels, values, focus, warning,
error, and selected state. State colors remain semantic foreground marks, never translucent material.

## Tier behavior

- **Compact and Regular:** use the same tokenized material recipe: translucent surface, 5px blur,
  border, top/left highlight rails, and shallow shadow. Compact reduces information and backdrop
  density, not Glass opacity or material family.
- **Unsupported fallback:** when `backdrop-filter` is unavailable, use the stable semantic fallback;
  this is a capability fallback, not a size-tier variant.
- **Regular:** one required authored panel uses the same Glass material roles as Compact.
- **Expanded:** the same region may gain clearer material depth or a larger label-safe knockout; do
  not introduce additional Glass families.

Glass radius is no larger than 8px. Use a runtime radius token only when its resolved value stays
within that cap. Do not copy large consumer-glass card radii into widgets.

## Fallback and performance

For generated HTML, read `assets/styles/kimi-glass.css` and inline the panel, button/icon-button,
slider, and segmented selectors the widget actually uses. Do not link to the asset path from
the sandboxed iframe, copy it as a remote dependency, or redefine any `--kimi-glass-*` token. The
asset is the executable baseline; this reference owns the decision to use it.

Author a stable fallback before `backdrop-filter`:

```css
.glass-surface {
  background: var(--kimi-color-surface-raised);
  border: 1px solid var(--kimi-color-border);
}

@supports (backdrop-filter: blur(1px)) {
  .glass-surface {
    background: var(--kimi-glass-surface);
    border: 1px solid var(--kimi-glass-border);
    box-shadow:
      0 8px 32px var(--kimi-glass-shadow),
      inset 0 1px 0 var(--kimi-glass-highlight),
      inset 0 -1px 0 var(--kimi-glass-lowlight);
    backdrop-filter: blur(var(--kimi-glass-blur));
    -webkit-backdrop-filter: blur(var(--kimi-glass-blur));
  }
}
```

The host token names are canonical. Do not replace them with local alpha values. Keep one backdrop-filter layer per widget and avoid overlapping blur regions, animated blur, full-widget blur,
or blur beneath scrolling data. Electron Canvas performance is part of correctness.

Glass requires a local material stage behind it: a meaningful Construction Grid, Pixel Field, chart
field, tonal field, or content field that visibly continues beneath the translucent region. The stage
must support the job and must not be ornamental noise. At 100% zoom, verify that the backdrop is
perceptible through the surface while P0 content remains fully readable. A flat white-on-white Glass is a failure even when DevTools reports that `backdrop-filter` is active.

### Perceptible backdrop geometry (Glass backdrop role only)

When the `Glass backdrop field` role is selected, the task-derived backdrop continues beneath the Glass panel
in layout coordinates; it must not stop at its edge or sit only in an adjacent column. An adjacent-only
backdrop is a layout failure because blur cannot sample pixels that are not behind the panel. Grid alone on a neutral Light surface
is also insufficient: blur can erase faint lines while white over white remains white. Preserve at least one Light-mode backdrop cue through the material, such as a
task-derived color/tonal band, Pixel/ASCII density change, chart trace, image region, or other
non-neutral semantic mark. Open expressive fields and P0 Pixel displays use their own regions and do
not need to overlap a panel.

For generated HTML, use the stacking helpers from `assets/styles/kimi-glass.css` when Glass is
selected. The backdrop renderer or field uses both `.kimi-glass-backdrop` and
`data-kimi-glass-backdrop`; the attribute value names its job, not its appearance:

```html
<section class="instrument kimi-glass-stage">
  <div class="task-field kimi-glass-backdrop"
       data-kimi-glass-backdrop="air-quality-density"></div>
  <div class="kimi-glass kimi-glass-panel">...</div>
</section>
```

The task field supplies the actual Pixel, chart, tonal, image, or content-derived marks. The helper
only establishes safe overlap and stacking. Size the field so a meaningful cue crosses the panel
footprint in Regular and Expanded; do not move the cue to the side merely to keep text pristine.
Instead, keep the cue subordinate, preserve a label-safe zone, and let the canonical Glass material
mediate it. Compact keeps the same Glass material while reducing backdrop density or removing the
backdrop when fit, accessibility, or performance requires it; only unsupported `backdrop-filter`
capabilities use the opaque fallback.

The backdrop helper intentionally uses `pointer-events: none` so it cannot block Glass controls.
For an interactive `KimiPixelField`, mount the field inside the backdrop but pass the stage as
`pointerTarget`:

```js
const stage = document.querySelector('.kimi-glass-stage');
const backdrop = stage.querySelector('[data-kimi-glass-backdrop]');
KimiPixelField.mount(backdrop, { pointer: true, pointerTarget: stage });
```

Pointer coordinates are still resolved against the backdrop container. Do not restore pointer events
on the visual layer or place it above the panel.

## Grid and label-safe knockout

A local Glass surface may act as a label-safe knockout when Grid or Pixel marks would cross critical
copy. It must cover only the content region, align to the same real columns/modules where feasible,
and preserve the line hierarchy in `grid-and-layout.md`. Glass cannot conceal an unrelated second
grid, poor placement, text overflow, or chart-label collision.

## Anti-pattern repair

| Failure | Repair |
|---|---|
| every section is translucent | return to one open layout and keep one justified surface |
| one Glass panel wraps the primary chart, long list, or most content | keep only the local P0/control cluster in Glass; move the main structure to the open surface |
| a chart or data field is readable only beneath blur | move the inspectable visualization above or outside Glass; keep only a subordinate cue behind it |
| nested cards or Glass | flatten hierarchy; use spacing/alignment/separators |
| flat white-on-white surface hides the material | add one meaningful local material stage or use the canonical opaque Compact/fallback state |
| Pixel/tonal field is adjacent to the panel while the Glass backdrop role is selected | extend the task-derived field beneath the panel and keep one Light-mode cue perceptible through it; otherwise keep an open field outside the panel |
| widget redefines weaker Glass values | delete local aliases and consume the canonical host tokens |
| blur lowers copy/chart contrast | use opaque fallback or local knockout; raise text/state contrast |
| Compact looks noisy or slow | reduce backdrop density and simplify P0 content; keep the same Glass material |
| Glass hides Grid conflict | align dividers and Grid first; use Glass only for the remaining local label zone |
