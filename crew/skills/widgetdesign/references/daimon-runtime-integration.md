# Daimon Runtime Integration

Use this file to align the standalone Kimi Widget design system with Daimon Blueprint Widget and
Canvas constraints. Daimon owns runtime mechanics. This package is the **Single-skill design authority**
for every visual and information-architecture decision inside that runtime.

## Runtime ownership

| Area | Owner | Kimi Widget role |
|---|---|---|
| Widget creation, files, data, events, iframe APIs | Daimon `widget` skill | Follow the existing `index.html`, `window.DaimonWidget`, slots, events, and file-resource rules. |
| Canvas placement, grid, mount ids, z-order, view state | Daimon `canvas` skill | Design for the existing 12-column Canvas grid and host-owned placement frame. |
| General Widget design and information architecture | This `widgetdesign` package | Own job routing, archetypes, host-frame posture, interaction, states, readability, and adaptive disclosure. |
| Kimi brand expression | This `widgetdesign` package | Own typography, palette, grid/composition, icons, data visualization, texture, and final visual feeling. |

The runtime skills are implementation contracts, not visual authorities. They do not need to load,
delegate to, or coexist with another Widget Design Skill.

## Runtime rules

- A Daimon Blueprint Widget has one responsive `index.html` at the Widget workspace root. Do not
  create extra pages for fullscreen, inputs, or Canvas.
- Put controls inside the single page. Submit user intent through Daimon widget APIs such as
  `window.DaimonWidget.emit("submit", payload)` when the runtime provides them.
- Render live data from `window.DaimonWidget.data.main` and subscribe to host updates where the
  Widget skill requires it. Sample data is preview-only.
- Use `DaimonCanvas.files` / `DaimonWidget` file-resource APIs for files. Do not expose raw local
  paths, `file:` URLs, resource-store internals, API keys, runtime logs, or local resource URLs.
- For Canvas-only work, create or reuse the Widget first, then place it with Canvas. Do not call
  conversation display APIs unless the user asked for chat/dock display too.

## No external dependencies

- Author widget output with native HTML, CSS, SVG, and JavaScript plus assets bundled in this skill
  or provided through Daimon file-resource APIs.
- Do not load Tailwind CDN, React, Vue, npm packages, remote fonts, external icon libraries,
  scripts, stylesheets, images, modules, or texture assets.
- Inline a shipped Kimi SVG with `currentColor` when an icon is needed. Do not replace it with emoji,
  a text initial, or a third-party icon.
- Stream useful static markup first and place optional interaction scripts last.
- When Glass is selected, read `assets/styles/kimi-glass.css` and inline only the selectors used by
  the output. Never link to the local skill path from an iframe and never recreate Glass tokens.
- For responsive semantic regions, read `assets/styles/kimi-fit.css`, add `data-kimi-root` to
  the content root, and mark P0/P1/P2/P3 regions. This asset only removes lower-priority regions in
  the host's Compact tier; the remaining P0 markup must still be measured and fit.
- For every conversation or Canvas Widget, read `assets/styles/kimi-host-safe-zone.css`, inline its
  utility, apply `.kimi-host-safe-context` to the outer content root, and apply
  `.kimi-host-safe-header` to the first semantic row. Daimon overrides its fallback tokens at runtime.
- When Pixel/ASCII is selected, inline the dependency-free renderer from
  `assets/scripts/kimi-pixel-field.js`; never load it as a network module.

## Theme ownership

- Daimon owns active Light/Dark theme values and synchronizes semantic runtime tokens into the iframe.
- This skill owns correct token use. Every surface, label, chart mark, icon, focus state, status,
  and texture carrier must resolve through semantic tokens.
- Do not use `prefers-color-scheme`, maintain a separate theme table, or hardcode theme colors.
- Generated widget code uses the canonical aliases in `runtime-core.md`. Raw `--seo-chat-*` and
  `--color-*` aliases are host compatibility details, not widget-authoring fallbacks.
- JavaScript-rendered SVG/canvas must read `window.DaimonWidget.theme`, `getToken(...)`, or
  `onThemeChange(...)` and redraw when the runtime theme changes.

## Security boundary

- Never expose raw local paths, `file:` URLs, resource-store internals, API keys, runtime logs,
  authentication values, or local resource URLs in markup, events, labels, or outgoing intent.
- Use only declared host APIs for files, data, events, and user intent.
- Sample data belongs only in preview/evaluation artifacts; a production widget reads runtime data.

## Canvas size contract

Daimon Canvas grid mode has 12 columns. A useful mental model is:

| Span | Approx size | Design expectation |
|---:|---:|---|
| `3 x 5` | `264 x 204` | compact answer, one visual proof, edited labels |
| `5 x 8` | `448 x 340` | default widget, primary visual plus essential facts |
| `8 x 12` | `724 x 516` | regular inspection, more rows or secondary structure |
| `12 x 16` | `1092 x 692` | full-width composition, benchmark or broad comparison |

The exact host may differ. Use the iframe/container size, not physical screen size, to decide
compact / regular / expanded disclosure.

When placing a newly generated ordinary Canvas Widget, use Daimon's canonical default placement
(`420 x 320`, approximately `5 x 8` grid units) unless the Widget was explicitly designed and
verified as Compact. Never request a `4 x 3` placement for a Regular Widget: it is a small host
surface, not a neutral default, and will make the shared document recompose before its promised P1
content can fit.

The host-control safe zone is not a layout mode and does not change the disclosure tier. It reserves
only the occupied top-right geometry for host chrome. The same responsive document must remain valid
in conversation, Canvas, fullscreen, and pin surfaces; surface-specific content forks are not allowed.

## Daimon host-control safe zones

Daimon overlays controls outside the iframe and above Widget content. The action group differs by
surface and state: a Canvas placement normally has fullscreen and menu controls, while a conversation
preview can expose save, open/fullscreen, refresh, source, DEV diagnostics, and a saved-Canvas link.
The Widget cannot move or restyle these controls and cannot receive pointer events through them.

Use the geometry measured by the host through these CSS tokens:

- `--daimon-widget-host-safe-inline-end`: occupied top-right width including leading clearance;
- `--daimon-widget-host-safe-block-start`: occupied top depth including bottom clearance.

The values update without reloading the iframe when action count or geometry changes. The current
conservative fallback is `190px` by `44px` for a conversation preview. Canvas uses `82px` by `44px`.
The host may publish `0px` by `0px` for a full-result, pin, or share surface without overlay chrome.
Do not infer the surface from viewport width and do not hardcode the current button count in Widget
markup. Consume the tokens through `assets/styles/kimi-host-safe-zone.css`.

No title, P0 value, unit, state, direct chart label, legend, caveat, provenance, authored button,
input, focus target, or pointer-critical visual region may intersect the resulting rectangle.

A background Grid, Pixel, image, or tonal field may continue through the zone when partial occlusion
does not change its meaning. Do not draw placeholder icons, empty buttons, a fake toolbar, or a
visible outlined box to represent the host controls.

For a normal first semantic row, use the shipped utility structurally:

```html
<main class="kimi-host-safe-context">
  <header class="kimi-host-safe-header">...</header>
</main>
```

This does not require an empty banner across the Widget. Content below the first row may use the full
width. If the remaining first-row width cannot contain the complete title or P0, move that semantic
group below the tokenized safe-zone depth; never shrink, clip, or ellipsize primary content to keep it
beside the host controls. A Widget without a header applies the same collision rule to its first
content row.

Treat the safe zone as occupied even when controls are invisible in a static screenshot. Test hover,
focus, menu-open, saved, DEV, and touch states in conversation and Canvas surfaces. Confirm a no-overlay
full-result state releases the reservation. The shared `index.html` must remain correct everywhere.

## Visual integration

Start from the Daimon host posture:

- host owns the outer frame;
- black, white, and gray carry most hierarchy;
- color is semantic signal;
- containers are used only when they clarify content boundaries;
- prose becomes values, rows, axes, nodes, choices, controls, or states.

Make the widget visually Kimi using `design-system.md`: brand-owned typography, Kimi Blue as a
semantic emphasis, the 5% composition grid for larger layouts, Kimi icon discipline, reduced data
visualization, and the information-carrying texture and motifs in `brand-texture-language.md`.

## Conflict resolution

When rules conflict, resolve by ownership:

1. Daimon `widget` and `canvas` rules win for files, APIs, data, events, placement, and security.
2. `design-system.md` wins for job routing, hierarchy, components, states, visual identity,
   typography, palette, data visualization, and final widget feeling.
3. `adaptive-widgets.md` wins when visual ambition or content density would break fit.
4. Component, icon, texture, and token details win only inside their owned semantic role.

Do not "fix" a Daimon runtime constraint with visual guidance. Adapt the design to the runtime.
