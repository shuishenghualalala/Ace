# Default Art-direction Compiler

Use this owner for every Default Kimi mode generation. A visually unspecified prompt is an
incomplete brief, not a low-design request. The agent owns the missing art direction.

## Internal design brief

Before markup, complete these lines internally:

- `Semantic phenomenon: ___` - task behavior such as flow, contour, density, rhythm, progress,
  route, comparison, transformation, frame, or resolve.
- `Signal-to-visual mapping: A -> ___; B -> ___.` Use at least two independent content signals and
  two legible visual or motion variables.
- `Semantic carrier code proof: source A -> derived variable -> renderer property; source B -> derived variable -> renderer property; counterfactual signal test ___.`
- `Carrier and mode: ___; asset/implementation: ___.`
- `Pointer meaning: ___; no generic hover sparkle.`
- `Motion meaning: ___; static/reduced-motion meaning: ___.`
- `Grid/surface/color: ___; label-safe zone: ___.`
- `Glass footprint gate: eligible local cluster ___; primary chart/list outside ___; or no Glass.`
- `Pixel placement role: open expressive field / optional Glass backdrop field / P0 Pixel display; geometry ___; label-safe zone ___.`
- `Tier composition: Compact ___; Regular ___; Expanded ___.`

The agent must not expose the internal brief during routine generation. It must implement the brief
and report only the useful result. A widget must not reuse a generic field without task-specific
mappings, even when that field is attractive.

## Default carrier gate

For every visually unspecified Default Kimi widget, Regular and Expanded must emit one broad
ASCII/Pixel carrier or one readable P0 Pixel display. Prefer a task-derived semantic carrier; when
that would be artificial, use a bounded brand-atmosphere field with a named job such as balancing
negative space, build orientation, curiosity, or transition. Load
`brand-texture-language.md` and use `assets/scripts/kimi-pixel-field.js` when the carrier is a field.
The field must occupy meaningful open space and perform that named semantic or atmospheric job; it is
not random noise or an afterthought. Prefer a background or negative-space field behind/beside the
main content. A Pixel-styled table, chart, badge, or small component does not satisfy this broad field
gate by itself. A stacked bar chart, cadence strip, histogram, or striped table may be a secondary
information graphic, but it cannot be the only carrier when the Default Kimi gate calls for a
background field. Choose a visual precedent only to improve its composition. Omit
the carrier only for a named `fit`, `accessibility`, `task relevance`, `performance`, or `no viable
carrier role` reason in the internal proof. The user not asking for design details is never an
omission reason. Object-led mode follows object fidelity and is exempt unless Kimi styling is
requested.

## Named visual region gate

A label that names a visual region, such as `cadence`, `signal`, `flow`, `spectrum`, `pulse`, or
`field`, is a content promise. Regular and Expanded must populate that same region with a visible
carrier; a heading above an empty Grid or blank rectangle fails even when another chart exists
elsewhere. For packet cadence, prefer repeated Pixel/ASCII marks whose spacing, density, grouping,
or interruption expresses packet interval, jitter, burst, or loss. If live data is unavailable, use
clearly labeled illustrative sample data rather than leaving the named region empty.

## Mapping method

1. Name the phenomenon before choosing a renderer.
2. Inventory available signals without inventing live facts. Use a clearly labeled illustrative
   sample when the user requests a widget but supplies no data.
3. Map position and geometry first, then density, texture, color, and motion.
4. Trace every semantic mapping to a real data property or state variable in code. Run the
   counterfactual signal test: change one source value while keeping the title fixed and confirm the
   field changes; changing only the title must not alter the field.
5. Give pointer response a domain meaning: displace flow, inspect a point, scrub a sequence, attract
   related marks, or reveal local state.
6. Make reduced motion preserve direction, magnitude, state, and P0 without animation.
7. Reject Glass when the candidate region contains the primary chart, long list, table, map, or most
   content. Keep Glass bounded to one P0/control cluster and leave the main structure open.
8. Choose one Pixel placement role from the job. Prefer an open expressive field in negative space
   or a readable P0 Pixel display when those carry the visual thesis. Only when the role is
   `Glass backdrop field` should a task-derived cue continue beneath the actual Glass footprint;
   a visually rich field beside the panel cannot make that panel translucent.
9. Reject a carrier that works unchanged after swapping in an unrelated title and value.

## Wind / vector field canonical recipe

For wind, use one broad ASCII directional field in open or negative space, outside the main panel,
with a label-safe P0 instrument. It may cross beneath the panel only when the selected placement role
is `Glass backdrop field`:

- `windFromDeg -> flowAngleDeg = (windFromDeg + 90) % 360`; the field moves downwind in screen space;
- `speedKmh -> density, opacity, and driftSpeed`; faster wind reads denser and drifts faster within a
  restrained range;
- `gustKmh -> local variance`; gusts perturb spacing or short-lived displacement without becoming a
  second chart;
- direction label plus speed is the P0 atomic group;
- use `mode: 'directional'`, `mark: 'ascii'`, `pointer: true`, and `pointerMode: 'repel'` so pointer
  displacement reads as local turbulence;
- reduced motion -> static directional field with the same angle and density;
- Compact keeps P0 and a short static vector mark; Regular restores the interactive field and gust
  context; Expanded adds a directly labeled history/evidence strip.

Do not copy this recipe onto unrelated widgets. Timer fields should resolve with remaining time;
budget fields should express allocation and threshold; maps should use route/coordinate behavior;
media in Default Kimi mode may map playback energy or position to a waveform. Object-led mode skips
this owner unless the user explicitly requests Kimi styling.
