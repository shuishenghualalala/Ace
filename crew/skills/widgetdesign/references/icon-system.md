<!-- preview-rule: icons.system | section: Icon selection | specimen: icon-grid | coverage: covered | priority: core -->
# Kimi Widget Icon System

Read this when a widget uses icons — buttons with icons, toolbars, status markers, empty states,
or any icon-bearing element. The skill ships 105 Kimi icons as inline-ready SVG — a curated
widget subset of the full Kimi library (brand logos, rich-text formatting, and host-app-specific
icons are intentionally excluded).

## Where icons live

- `assets/icons/*.svg` — the icon source files (24×24 viewBox, `fill="currentColor"`)
- `references/icons/manifest.json` — machine-readable index: `name`, `file`, `category`,
  `aliases` (English + Chinese), `use_for`, `avoid_for`, `default_size`
- `references/icons/categories/*.json` — the same index split by domain: `general`, `arrows`,
  `chat`, `input`, `navigation`, `editor`, `media`, `file`, `status`, `image`, `data`, `social`,
  `system`

## How to use an icon in a widget

Before placing any icon, pass this gate:

1. **Intent**: the icon names a real action, object, state, or file type.
2. **Source**: the intent matches the local manifest's `use_for` and does not hit `avoid_for`.
3. **Markup**: a shipped SVG is inlined with `currentColor`; if no semantic match exists, document
   that result before considering a widget-local custom SVG.
4. **Size**: the rendered size fits the context table and never exceeds 24px.

If intent or size fails, remove the icon and solve the emphasis with type, grid, direct labels, data
marks, or an ASCII/pixel motif. If only the source gate fails because the shipped library has no
semantic match, a custom inline SVG is the last resort under the construction and accessibility
rules below. Do not make an icon gallery to prove the widget is visual.

The widget runs in a sandboxed iframe with no imports, no modules, and no external assets. **Inline
SVG is mandatory; a shipped asset is preferred; a documented widget-local custom SVG is allowed
only as the last resort defined below.** For a shipped match, copy the SVG content from
`assets/icons/` directly into the widget markup. Keep `fill="currentColor"` so the icon follows the
surrounding text color and both themes for free.

```html
<button aria-label="Search" style="color: var(--kimi-color-text-secondary)">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
    <path d="..." fill="currentColor"/>
  </svg>
</button>
```

Set the rendered size via the `width`/`height` attributes (or CSS); never edit the `viewBox` and
never restyle the path data.

<!-- preview-rule: icons.selection-flow | section: Icon selection | specimen: icon-grid | coverage: covered | priority: core -->
## Selection flow

1. Identify the UI intent in English or Chinese ("search", "delete", "上传").
2. Search `references/icons/manifest.json` by `aliases`, `category`, and `use_for`
   (grep is enough; category files are smaller if you already know the domain).
3. Check `avoid_for` to eliminate wrong matches — do not use `UploadIcon` for "download" or
   `ShareIcon` for "export to file".
4. Prefer the non-suffixed name. Suffixes mean variants: `_b` alternative style,
   `_c` directional variant, `_r` rotated/reversed. Use them only when context demands.
5. Read the matched file from `assets/icons/` and inline it.

If no shipped icon matches the semantic need, document the missing match and build a widget-local
custom inline SVG only as a last resort, following the construction and accessibility rules below.
Keep it local to that widget; do not silently add it to the shipped library. Do not substitute a
vaguely related icon, and never draw from memory of Lucide / Material / FontAwesome shapes.

## Style & construction (also for custom icons)

- **Type**: outlined linear icons, 1.8px stroke weight rendered as filled paths
- **Grid**: 24×24 viewBox
- **Color**: `currentColor` only — no hard-coded hex, no gradients, no shadows, no fills beyond
  the stroke shape
- **Construction**: consistent caps and joins; match the optical density of the shipped icons
- Custom icons must be visually indistinguishable in weight and style from the library ones;
  reference a shipped icon of similar complexity as the construction guide
- Custom icons remain inline and widget-local, use `currentColor`, and meet the same size and
  accessibility rules as shipped icons

<!-- preview-rule: icons.size | section: Icon selection | specimen: icon-grid | coverage: covered | priority: supporting -->
## Size

Icon size follows the host element, not arbitrary choice:

| Context | Icon size |
|---|---|
| Inline with 16px body text | 16px |
| Default buttons, chips, tabs (~32px controls) | 18px |
| Toolbar buttons, primary actions | 20px |
| Standalone icon buttons | 20–24px |
| Badge dots / micro indicators only | 12px |

Icon containers are square (`width == height`) for optical alignment. Do not scale outside this
table.

**24px is the hard ceiling.** If a spot seems to call for a larger icon — an empty-state
illustration, a hero mark, a big decorative glyph in a stat card — that is not an icon use case:
drop the icon entirely and solve it with typography, a number, or plain text hierarchy instead.
Icons scaled past 24px turn into decoration, and their 1.8px-stroke construction visibly degrades.
Never work around the ceiling by drawing a "bigger custom icon".

## Color

- Default: `currentColor`, inheriting the text color of the context
  (`--kimi-color-text-primary` for high-emphasis, `--kimi-color-text-secondary/tertiary` for
  supporting icons)
- Semantic overrides only: destructive `--kimi-color-danger`, success `--kimi-color-positive`,
  warning `--kimi-color-warning`, disabled `--kimi-color-text-quaternary`
- Icons are UI chrome under the black/white-first rule — never accent blue for a generic icon

<!-- preview-rule: icons.accessibility | section: Icon selection | specimen: icon-grid | coverage: covered | priority: supporting -->
## Accessibility

- Icon-only buttons need an accessible name: `aria-label` or visually hidden text.
- Decorative icons get `aria-hidden="true"`.
- Loading spinners expose `aria-busy="true"` on the parent control.

## Don't

- Do not use emoji as UI icons.
- Do not render any icon larger than 24px. If the design wants a bigger one, remove the icon and
  use text or layout instead.
- Do not fetch, link, or import icons from anywhere — inline only.
- Do not use external icon libraries' shapes. Use a shipped Kimi semantic match first; use a
  widget-local custom inline SVG only as the documented last resort above.
- Do not guess meaning from the filename alone — check the manifest's `use_for`/`avoid_for`.
- Do not use brand logos or wordmarks as UI icons in widgets.
- Do not put more than one leading + one trailing icon in a single button.
