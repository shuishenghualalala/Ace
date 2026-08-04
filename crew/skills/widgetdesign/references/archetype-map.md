# Map Archetype

Use when the user needs dependencies, routes, groups, ownership, or a critical path.

- **Dominant structure:** stable nodes with labeled directional links; group only when it changes
  meaning.
- **P0:** critical node, link, route, or failure path.
- **P1:** primary nodes and labeled links needed to understand P0.
- **P2:** groups, secondary links, alternate/failure paths, and evidence.
- **Compact:** critical node/path. **Regular:** primary network. **Expanded:** secondary structure
  without becoming unreadable.
- Prefer the critical path over completeness. Do not use unlabeled color coding, crossing links when
  routing can be simplified, or decorative nodes with no semantic relationship.

