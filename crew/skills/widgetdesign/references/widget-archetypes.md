# Widget Archetypes

This reference is the canonical owner of job-to-archetype selection and tier behavior. Pick one job
and one dominant contract; supporting components cannot turn the widget into a dashboard collage.

<!-- preview-rule: vnext.archetype-contracts | section: Layout patterns | specimen: layout-archetypes | coverage: covered | priority: core -->
## Job to archetype routing

| Job | User needs | Dominant archetype | P0 |
|---|---|---|---|
| answer | understand one conclusion | answer field | conclusion or result |
| compare | inspect meaningful differences | comparison matrix or aligned rows | winner, exception, or decisive delta |
| measure | read magnitude, trend, or threshold | metric plot or value-axis field | value plus unit/state |
| choose | make a bounded decision | choice field | selected/recommended option and action |
| simulate | change inputs and inspect consequences | control-output field | current output tied to inputs |
| sequence | understand order, progress, or next step | timeline or staged rail | current stage and next transition |
| map | understand dependencies, routes, or groups | relationship map | critical node/link/path |

If the request has several verbs, choose the job that resolves the user's immediate uncertainty.
State it as `The widget helps the user ___`. If one visual contract cannot complete that sentence,
split the task or answer in prose.

## Archetype contracts

- **answer:** lead with the conclusion; pair it with one structural proof, not a paragraph. P0 must
  remain meaningful without decorative treatment.
- **compare:** use a shared baseline, aligned attributes, and direct labels. Highlight only the
  decisive difference; never encode the comparison by color alone.
- **measure:** show value, unit, scale/threshold, and direction where relevant. A chart earns space
  only when it adds trend or distribution beyond the headline number.
- **choose:** make options mutually legible, mark selection and consequence, and keep the primary
  action adjacent. Do not preselect a risky or destructive choice.
- **simulate:** controls and their output share one visual field; every control has a visible effect,
  range, unit, and reset/default path. Separate assumptions from results.
- **sequence:** establish direction, current state, completed states, and next state. Use consistent
  connectors; do not render unrelated cards as a timeline.
- **map:** use stable nodes, labeled directional links, and grouping only when it changes meaning.
  Prefer the critical path over a complete unreadable network.

For every archetype, direct labels beat legends, semantic state beats decoration, and the dominant
visual moment must carry P0 or its proof.

## Adaptive archetype behavior

| Job | Compact: P0 | Regular: P0 + P1 | Expanded: P0 + P1 + P2 |
|---|---|---|---|
| answer | conclusion + state | conclusion + reason/next action | evidence, caveat, supporting facts |
| compare | winner/exception; optional short decisive delta as its direct label | key aligned rows | full decision rows and evidence |
| measure | value + unit/state | threshold and short trend | axes, history, distribution, explanation |
| choose | recommendation/current selection | essential options + action | trade-offs, consequences, secondary options |
| simulate | output + current input summary | essential controls beside output | additional controls, scenarios, assumptions |
| sequence | current stage + next | completed/current/next rail | full stages, timing, dependencies, exceptions |
| map | critical node or path | primary nodes + labeled links | groups, secondary links, failure paths |

Compact is a semantic edit, not a miniature Expanded widget. Regular must be sufficient to perform
the job. Expanded adds inspectable evidence without changing the job, visual direction, or dominant
archetype. If the archetype's minimum viable semantics cannot fit a tier, choose a larger default
size rather than clipping the contract.
