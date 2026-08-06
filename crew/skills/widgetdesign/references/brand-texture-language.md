# Kimi Brand Texture Language

This is the sole canonical owner of Pixel-system roles, region count, scale, and tier behavior.
Other references route here instead of copying those contracts.

## Texture roles

Kimi texture is authored visual material, not a bitmap overlay added after layout. When present, use
one coherent Pixel system: one mark grammar, signal model, motion law, and color logic may continue
across coordinated regions. The system may carry information or bounded brand atmosphere, and either
role requires a named job and proof.

Classify the field before markup, testing semantic carrier before brand atmosphere:

1. **Semantic carrier**: encodes state, density, magnitude, uncertainty, completion, sequence,
   coordinate, or transformation.
2. **Brand atmosphere**: expresses build orientation, curiosity, awe, computational material, or a
   contextual subject without pretending to encode a datum.

Every motif needs a two-part proof before markup:

1. **Carrier**: the exact mark that holds the signal, such as a bar fill, reference band, local
   field, route, cell matrix, progress rail, or output trace.
2. **Role and job**: semantic carrier or brand atmosphere, followed by the exact information,
   subject, build orientation, or result relationship it serves.

If the carrier, role, or job cannot be named, remove the texture. Brand atmosphere must be authored,
bounded, recognizable as Kimi, subordinate to P0, and related to the task or result. It does not
legalize random noise, stock halftones, generic grain, or decorative terminal chrome.

### Semantic binding contract

A semantic ASCII/Pixel field is visual content. Its renderer must consume the same source data or
state used by the widget, not merely sit beside a matching label. A printed value or caption does not
count as a binding. For Regular and Expanded, bind at least two independent source signals when the
job provides them, and make at least one geometry channel change: orientation, position, spacing,
contour, density, count, extent, grouping, or order. Opacity, color, character choice, and motion may
carry an additional signal but cannot be the only binding.

Write each binding as `source path -> derived variable -> renderer property`, then run a
counterfactual signal test: change one source value with the title held constant and verify that the
field visibly changes in the intended channel. Change the title with source values held constant and
verify that the field does not change. Hard-coded mark counts, generic sine waves, random scatter, or
pointer sparkle fail when the named data never enters their calculation.

Use domain relationships, not a universal particle recipe:

| Phenomenon | Strong semantic bindings |
|---|---|
| direction / flow | angle -> orientation and drift; magnitude -> density/speed; variance -> turbulence |
| cadence / rhythm | cadence -> repetition spacing/rate; stride regularity -> variance; elapsed progress -> phase/extent |
| quality / concentration | measured level -> density/extent; threshold distance -> contour/state transition |
| progress / allocation | completion -> resolved extent; remaining amount -> sparse extent; threshold -> fixed reference |
| audio / frequency | amplitude -> vertical extent; frequency band -> horizontal position; playback -> phase |
| sequence / build | step index -> cursor position; state -> character family; completion -> aligned/resolved region |

Pointer response must also serve the phenomenon: disturb flow, inspect concentration, scrub phase,
reveal a local sample, or test a projected state. When no domain interaction exists, keep the semantic
field static rather than adding generic repel.

## Pixel field definition

A Pixel Field is a bounded field made from enough **small repeated units** to read as dot density,
cell density, halftone, character density, or a micro build trace at 100% Canvas zoom. Density,
spacing, unit size, value, and omission form the image or atmosphere.

Ordinary chart bars, large rectangles, color swatches, buttons, option tiles, content panels, and
large data blocks do **not** become a Pixel Field because they align to a grid. They keep their real
semantic role. A chart mark counts as Pixel only when it is intentionally constructed from repeated
small units and those units accurately encode the data.

## Pixel placement roles

Choose one placement role from the job; do not place every Pixel Field behind a panel:

1. **Open expressive field:** the preferred role for atmosphere, flow, contour, awe, negative space,
   or a broad abstract interaction. It occupies open space outside panels and may span a large blank
   region while staying behind label-safe zones.
2. **Glass backdrop field:** an optional local material cue beneath a bounded Glass panel. Use the
   canonical `.kimi-glass-stage` stack only when making the panel sample a task-derived field is part
   of the visual thesis. It is subordinate and never the only readable chart or data encoding.
3. **Pixel display:** a readable P0 title, value, status, or time composed from cells. Preserve a
   normal accessible text fallback; do not put the P0 display underneath blur just to create depth.

The field may also occupy a deliberate blank region below or beside the main output when that space
is part of the composition. Placement is a semantic decision, not a fixed corner or universal
panel-background recipe.

### Background field priority

For visually unspecified Default Kimi Regular/Expanded output, the primary Pixel/ASCII carrier
normally occupies the widget background or a broad negative-space region. It may wrap around a
foreground P0/control cluster, continue along an edge, or fill an open half of the stage. It should be
large and immediately perceptible like an environment, while label-safe zones keep text and controls
clear. A Pixel-styled table, chart, calendar, badge, icon, or small component remains an information
graphic or coordinated region; it does not satisfy the primary background-field requirement alone.

When a normal chart is also needed, split the layers: keep the chart readable in the foreground and
mount a separate broad Pixel/ASCII field in the open background or negative space. Do not repaint
the chart's bars as cells and call that background composition.

Use `assets/styles/kimi-pixel-background.css` for the stable layer stack. Mount
`assets/scripts/kimi-pixel-field.js` into `.kimi-pixel-background` and pass the stage as
`pointerTarget`, so the Canvas stays behind content while the full open region can respond to the
pointer. The foreground uses `.kimi-pixel-content`; P0 and dense labels may add
`.kimi-pixel-label-safe`. Do not add a border, radius, or shadow to the stage merely because it owns
the field.

Prefer a broad scatter/density field over a row of large regular squares. Useful transitions include
sparse-to-dense, noisy-to-resolved, input-to-output, unknown-to-clear, and dispersed-to-aligned. A
field may occupy a local background, edge, output-adjacent region, or part of a construction field,
but it must stop before primary copy, direct labels, values, controls, and status text.

Route each candidate through exactly one of these categories:

1. **Pixel display**: construct a readable short P0 value, status, or title from cells, as in a clock
   or terse build state. Preserve accessible semantic text and fall back to normal typography when
   the characters are ambiguous, too long, or cannot fit without reducing P0 clarity.
2. **Abstract interactive field**: the preferred Default Kimi treatment. Use a broad background or
   negative-space region whose direction, density, rhythm, contour, phase, or transformation derives
   from the task. Pointer response must have domain meaning and stay behind content.
3. **Normal information graphic**: charts, icons, thresholds, forecasts, and direct labels use
   ordinary SVG, chart, or local Kimi icon forms. Do not replace them with a Pixel silhouette merely
   to add brand character; combine them with a separate abstract field when useful.

## Signature families

| Family | Carries | Use | Avoid |
|---|---|---|---|
| **Dot / pixel density** | magnitude, confidence, completion, sample density | repeated cells, dot bands, filled modules | random confetti or decorative star fields |
| **Hatch / scan** | baseline, pending, projected, unavailable, uncertainty | sparse diagonal/linear marks inside a data or state region | full-page scan-line filters or moire |
| **Coordinate grid (semantic)** | scale, location, alignment, comparison | labeled chart/map grid; does not consume Pixel allowance | decorative graph-paper backgrounds or unlabeled coordinates |
| **ASCII / code field** | sequence, transformation, input/output, execution | short mono symbols, brackets, cursor, status trace | fake terminal chrome or paragraphs of code |
| **Resolve field** | curiosity, discovery, convergence, answer formation | dispersed marks becoming an ordered axis, route, or result | ambient particles with no before/after meaning |
| **Material grain** | rare conceptual material or editorial brand mood | restrained authored grain in explicit brand/concept work | generic texture, stock paper, film noise, or routine UI grain |

Texture may be geometric, symbolic, or material. Geometric and symbolic textures are useful across
utility widgets as abstract structure. Contextual fields may build a recognizable subject from the
same cells. Material grain remains rare because it is hard to author and bound meaningfully.

## Hybrid Pixel Routing

Object-led mode is exempt from the default interactive ASCII/Pixel field. Use object-native texture,
image structure, material, display behavior, or no texture according to the researched object. Add
Kimi ASCII/Pixel only when the user explicitly requests Kimi styling or it genuinely belongs to the
object's own visual language.

Regular and Expanded Default Kimi widgets must include one broad ASCII/Pixel carrier: prefer a
task-derived semantic carrier; when that would be artificial, use a bounded brand-atmosphere field
with a named visual job. Use an interactive ASCII field when direction, contour, density, characters,
or transformation can derive from the subject, data, state, or interaction; use Pixel dot/cell marks
when discrete units or measured density communicate the job more clearly. Omission requires one
named reason from fit, accessibility, task relevance, performance, or no viable carrier role; absence
of user art direction is never a reason.

When present, use one coherent Pixel system. Compact permits one Pixel treatment maximum. Regular and
Expanded permit up to two coordinated regions: one primary abstract interactive field plus one
coordinated P0 Pixel display or continuation region. A second region must share the first region's
units, source relationship, motion behavior, and token logic; two unrelated motifs fail.

Route the primary field by task and expression:

| Expression | Required routing |
|---|---|
| `utility` | abstract interactive field supporting scan, monitoring, comparison, control, or state |
| `expressive` | abstract interactive field or, only when the subject requires it, a recognizable contextual field |
| `resolve` | abstract convergence/transformation field; contextual treatment only when the result has a real subject |

An abstract field may support `utility`, `expressive`, or `resolve`; expression comes from the user
job, not whether marks form an object. Operational measures still default to `utility`, and ordinary
progress is not `resolve`. Representational Pixel silhouettes are exceptional: use one only when the
subject itself matters, it is recognizable at 100% Canvas zoom without its title, and it communicates
better than an ordinary icon, SVG, chart, or image.

Contextual imagery must remain legible as Pixel construction rather than becoming a conventional
illustration. Expanded extends the same motif; it never introduces a second texture family.

Regular and Expanded Default Kimi widgets use one broad abstract field by default. Expression may
change its behavior without forcing a silhouette. Prefer semantic binding; otherwise name the field's
brand-atmosphere job and never present it as data. Omission requires one named reason from fit,
accessibility, task relevance, performance, or no viable carrier role. The field occupies background
or negative space and stops before label-safe zones.

## Usage by widget class

### Data and chart widgets

Use texture as a secondary encoding after position, length, and direct labels. Normal charts remain
normal information graphics; Pixel does not replace a clear line, bar, threshold, forecast, icon, or
direct label:

- put dot density inside a confidence or sample-size mark;
- hatch a projected, unavailable, baseline, target, or uncertainty band;
- use a labeled coordinate grid only inside the plotting field; it is a semantic grid and does not
  consume the Pixel allowance under `grid-and-layout.md`;
- keep an optional Pixel-density carrier coordinated with the broader Pixel system;
- use a sparse scan or pixel transition to show noisy input resolving into a selected result;
- combine color and texture when two states must remain distinguishable in both themes.

Reserve a **label-safe zone** around every title, direct label, value, tick, caveat, and control.
Texture stops before that zone. Never place a pattern beneath text and hope opacity will save it.

### Brand and concept widgets

Use a structured field to express curiosity and awe through transformation:

- raw marks -> aligned modules -> answer;
- prompt cursor -> build trace -> output;
- scattered evidence -> bracketed relation -> conclusion;
- pixel field -> K-like route -> next action.

An expressive field may use a contextual moon, portrait, map, route, evidence cloud, or generated
object only when that subject is immediately recognizable and necessary. Prefer an abstract field
when the Pixel construction would become an unclear pseudo-icon.

### Tool and control widgets

Keep controls clean. Put texture on the output or state it explains:

- a progress rail may gain pixel steps;
- a pending output may use a sparse scan carrier;
- a disabled range may use a local hatch plus label;
- an execution trace may use cursor/bracket marks;
- a generated result may resolve from a local dot field.

Do not texture button fills, input backgrounds, every selected control, or the whole tool surface.
The control remains familiar; the output carries the Kimi signature.

### Archetype routing

| Archetype | Preferred Pixel placement and job |
|---|---|
| Answer / Measure | broad trailing/underlay field beside the verdict or value; optional coordinated P0 Pixel display |
| Compare | exception cell, delta band, patterned reference region, or aligned Pixel difference |
| Choose | selected route or broad output field tied to the chosen option |
| Simulate | output trace, projected band, uncertainty field, or stepped result rail |
| Sequence | build lane, current cursor, Pixel checkpoint, or execution trace |
| Map | coordinate density, route field, cluster, or relationship region |

Do not fix the field to one corner or force the same dot rectangle onto every archetype. Family
resemblance comes from shared Pixel grammar and construction discipline, not repeated placement.

## Size adaptation

| Tier | Pixel treatment | Scale |
|---|---|---|
| Compact | optional; one Pixel treatment maximum: readable P0 display, quiet mark, or short band after the P0 allowlist; no hidden P1/P2 | normally <=15% unless the Pixel display is P0 |
| Regular | one primary abstract interactive field; optional coordinated P0 display or continuation | field uses at least one-third of the available Regular or Expanded stage or spans at least half of one axis |
| Expanded | extend the same system with more samples/space; optional coordinated P0 display or continuation | field uses at least one-third of the available Regular or Expanded stage or spans at least half of one axis |

Compact may omit the Pixel treatment entirely. A surviving option cannot become a contextual scene,
coordinate system, criterion, evidence, or other P1/P2 proof. Regular and Expanded keep at least one
base spacing unit between the field and titles, values, direct labels, controls, focus states, and
caveats. A tiny corner cluster fails when selected as the primary field. Expanded adds samples,
extent, or a coordinated continuation without introducing another motif family. When one-third area
is unavailable, span at least half of one axis so the field still reads as an environment, not an icon.

Scale is geometric footprint, not opacity. A broad field stays low-contrast and subordinate to P0;
it still fails when it covers content, becomes co-dominant, or does not fit placement height.

## Light and dark themes

Build texture from runtime tokens:

- neutral structure: `--kimi-color-border`, text tertiary/quaternary, muted surfaces;
- primary texture signal: `--kimi-color-text-primary` or a restrained accent when the semantic or
  contextual job warrants it;
- state texture: positive, warning, danger, or disabled tokens only on the affected carrier.

Use `currentColor`, borders, opacity, `color-mix()`, repeated gradients, inline SVG patterns, or DOM
cells that inherit token colors. Do not hardcode separate light/dark texture palettes.

Verify both themes:

1. labels remain the highest-contrast local element;
2. texture remains visible but secondary;
3. the field's role and meaning do not invert;
4. pattern frequency does not shimmer or produce moire;
5. removing color still leaves position, label, or texture differentiation.

Reduce texture opacity or frequency before reducing label contrast.

## Implementation patterns

For a Canvas-backed field, read `assets/styles/kimi-pixel-background.css` plus
`assets/scripts/kimi-pixel-field.js` and inline the dependency-free assets. Its default signature is
`mark: 'ascii'` and `pointer: true`; select one named `density`,
`contour`, `directional`, or `atmosphere` mode and task-related characters. Override with dot,
square, or cell marks only when they communicate the carrier more accurately. The renderer is a stable template, not permission to add a field:
the carrier, role/job, label-safe zone, and omission rules above still decide whether it is used.
Keep its canvas behind content, leave canvas `pointer-events: none`, and let its bounded container
receive pointer movement. Use Compact and reduced-motion static simplification.

Prefer small, deterministic constructions that survive sandboxing and resize.

For the primary background field, use this layer order and delegate pointer input to the stage:

```html
<section class="kimi-pixel-stage" data-kimi-pixel-stage>
  <div class="kimi-pixel-background" data-kimi-pixel-background></div>
  <div class="kimi-pixel-content" data-kimi-pixel-content>...</div>
</section>
<script>
KimiPixelField.mount(
  document.querySelector('[data-kimi-pixel-background]'),
  { mode: 'atmosphere', mark: 'ascii', pointer: true,
    pointerTarget: document.querySelector('[data-kimi-pixel-stage]') }
);
</script>
```

Use a task-derived mode such as `directional`, `contour`, or `density` when the field carries data;
use `atmosphere` only when it is explicitly a bounded brand-atmosphere layer. The normal chart or
table remains in `.kimi-pixel-content` and never replaces the background field.

```css
.projected-band {
  color: var(--kimi-color-text-tertiary);
  background: repeating-linear-gradient(
    135deg,
    color-mix(in srgb, currentColor 16%, transparent) 0 1px,
    transparent 1px 6px
  );
}

.density-field {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 3px;
}

.density-field > i {
  aspect-ratio: 1;
  background: color-mix(in srgb, var(--kimi-color-accent) var(--density), transparent);
}
```

Use inline SVG patterns when marks need clipping inside a chart shape. Keep pattern ids local to the
widget and set strokes/fills through `currentColor`. Use DOM cells for discrete state and CSS
gradients for continuous or repeated structure. Never fetch texture assets from external URLs.

## Cursor, bracket, pixel, and code rules

- Cursor `|` means input focus, current execution point, or next insertion.
- Brackets `< > [ ] { }` mean grouping, boundary, route, or transformation only when readable.
- Slash, plus/minus, hash, star, and triangle marks label operations, deltas, coordinates, or states.
- Pixel cells may encode discrete units, stages, samples, or confidence, or construct a justified
  atmospheric subject; they are not retro decoration.
- Mono typography is limited to the motif, coordinate, code, raw id, or trace. UI labels stay in
  the brand sans role.
- Motion may gather, scan, resolve, focus, or confirm once. Respect reduced motion; brand atmosphere
  does not justify a perpetual loop.

## Failure and repair

Reject:

- full-surface random noise or material grain;
- a generic texture chosen only to make the widget feel premium;
- decorative gradients, bokeh, glow, blobs, film grain, paper grain, or scan filters;
- unlabelled patterns that require guessing;
- texture under labels, values, caveats, or controls;
- high-frequency lines that shimmer at Canvas zoom;
- every region using a different motif;
- a tiny icon-like Pixel cluster pretending to be the primary interactive field;
- a forecast, chart, threshold, or familiar icon redrawn as an unreadable Pixel silhouette;
- compact widgets retaining background texture while hiding useful information;
- Kimi Blue plus texture everywhere, which turns signal into wallpaper.
- a row of chart bars, large blocks, swatches, buttons, or option tiles mislabeled as Pixel material;

Repair in this order:

1. Restore the user job and dominant archetype.
2. Reselect the smallest valid tier that contains every priority promised by that tier.
3. Correct the Canvas placement width and height so all promised content is visible.
4. Only after tier and placement fit pass, name the Pixel role/job and restore label-safe zones.
5. Route the field by archetype and move it away from protected content.
6. Reduce Pixel area, cells, density, contrast, and contextual detail.
7. Reduce construction-grid contrast or remove secondary lines.
8. In Compact, remove the selected Pixel option, then the one nonsemantic grid fragment.
9. Verify Light/Dark and Canvas zoom without changing the valid placement.
10. If neither information nor recognizable Kimi brand identity is lost by removal, remove it and
   strengthen grid, type, or data reduction instead.

Never shrink, simplify, or remove the grid/Pixel system to compensate for a wrong tier or undersized
Canvas placement.

## Texture proof

Before finalizing, answer:

- What family is used?
- Which regions belong to the one coherent system, and which is the primary field?
- If a coordinated region exists, how does it share units, source logic, motion, and tokens?
- Is a Pixel display still readable with equivalent accessible semantic text?
- Which concrete information remains a standard chart, SVG, or icon?
- Is its role semantic carrier or brand atmosphere, and what job does it perform?
- Which source paths enter the renderer, and which geometry channel changes?
- Does the counterfactual signal test prove the field changes with data rather than its caption?
- What remains in compact, regular, and expanded sizes?
- Where are the label-safe zones?
- Does Light/Dark preserve hierarchy and meaning?
- Does its full footprint fit the selected placement height?
- Would removing it make the widget less informative or less recognizably Kimi?

At 100% Canvas zoom, the field must be recognizable as Pixel material without developer tools. DOM
presence, an aria label, or source comments alone do not pass. Verify that multiple cells, dots,
modules, hatch marks, or halftone steps remain visibly distinct in both themes; a reviewer should be
able to point to the field and name its role. Increase cell size, final token contrast, or local area
before adding another motif, and never solve invisibility by putting texture beneath protected text.

The last answer must be yes for information or justified brand identity, not merely for decoration.
