# Kimi Brand Source for Widgets

This bundled file is the frozen normative source of Kimi brand facts for widget generation, together
with `design-system.md`. Its upstream source is provenance only and is never required, read, fetched,
or resolved at runtime.

Provenance record:

- ID: `kimi-brand-guideline/DESIGN.md`
- Source date: `2026-07-09`
- SHA-256: `1bbd5266bb1b697a35041ad4ca3eb4b659f27bb5c69a2b18a339c1119566985d`

<!-- preview-rule: brand.source-facts | section: Brand foundation | specimen: brand-source | coverage: covered | priority: core -->
## Brand facts

- Brand keywords: **Avant-garde / Curious / Pure**.
- Slogan: **Born to build.**
- Product promise: Kimi is a lifelong partner in human progress and helps people build a more
  productive future.
- Logo idea: code cursor `|` plus bracket/arrow `< >`; pure technical DNA, immediate input state,
  and future direction.
- Art direction: spark curiosity and awe through structured information, code-like forms, and
  clarity emerging from complexity.

Widget translation: do not make a marketing slide. A widget should feel like a small generated
instrument: useful, quiet, exact, and ready to manipulate.

<!-- preview-rule: brand.palette-source | section: Color palette | specimen: color-palette | coverage: covered | priority: core -->
## Palette source

Core brand color:

| Token | HEX | Use in widgets |
|---|---:|---|
| Kimi Blue | `#81C4FF` | brand identity, selected data, meaningful progress, primary generated result |

Core palette roles:

| Color | HEX | Widget role |
|---|---:|---|
| Bright blue | `#00A1FF` | secondary blue scale, never default decoration |
| Light blue | `#91D4FF` | blue sequential tint, background tint under 25% |
| Cyan | `#00F6FF` | rare auxiliary accent for named meaning |
| Deep navy | `#002E58` | deep brand contrast, mostly brand/editorial specimens |
| Neutral gray | `#8E9390` | supporting neutral, separators, metadata |
| Soft gray | `#E1E3E6` | surface and low-emphasis fills |
| White / black | `#FFFFFF` / `#000000` | primary backgrounds and text base |

Secondary colors are low-saturation helpers, not a rainbow system. Use them only for named
semantics: warning, danger, success, category, or state. The deck defines no brand gradient system;
do not invent blue-purple gradient identity.

Blue shades from the deck:

`#EFF8FF -> #D9EDFF -> #91D4FF -> #81C4FF -> #3C8EF4 -> #1F4BAE -> #204289 -> #002E58`

Product UI tokens are theme-owned. Widget code should call semantic token roles (`surface`,
`label`, `separator`, `KMBlue`, danger/warning/positive) instead of copying light/dark hex values.
Use raw hex only when the widget is a palette specimen.

Color application:

- two-color = neutral + Kimi Blue for most generated tools;
- three-color = neutral + blue + one named semantic/category hue;
- multi-color = independent categories, brand art, or complex imagery only;
- avoid discordant color blocking, unauthorized mixing, and blue as generic decoration.

<!-- preview-rule: brand.type-source | section: Typography | specimen: typography-scale | coverage: covered | priority: core -->
## Typography source

| Role | Family | Widget use |
|---|---|---|
| Primary UI | Inter | English UI, body, labels, values |
| Technical | Geist Mono | code, prompt text, ASCII, coordinates, raw identifiers |
| CJK UI | MiSans / PingFang SC | Brand provenance for Chinese/Japanese/Korean UI text; the runtime sans token selects the available face |
| Editorial English | Sentient | rare brand/editorial title moments |
| Editorial Chinese | Source Han Serif CN | rare premium Chinese headline moments |

Widget type should be tighter than deck type. Use product scale first: 12 metadata, 14-15 dense
cells, 16 body, 17-20 title, 28-42 display values. Large deck typography is a reference for brand
moments, not routine inline widgets.

Widget CSS uses `var(--kimi-font-sans)`, `var(--kimi-font-mono)`, or the runtime serif token by role.
Runtime font tokens own all face selection and fallback chains; authored fallback stacks are not
allowed.

## Visual language source

The logo concept comes from cursor `|` and bracket/arrow `< >`. Widget motifs may borrow cursor,
bracket, ASCII, pixel grid, code-like marks, scan lines, and modular fields as either a semantic
carrier or bounded authored brand atmosphere. The latter may express build orientation, curiosity,
awe, computational material, or a contextual subject without claiming to encode a datum.

Do not use motifs as wallpaper or random decoration. Both roles must be authored, bounded,
subordinate to the answer, and justified under `brand-texture-language.md`. Natural/material texture
and photography remain limited to explicit brand/art-direction work with a named conceptual role.

<!-- preview-rule: brand.logo-boundary | section: Icon selection | specimen: icon-grid | coverage: covered | priority: supporting -->
## Logo boundary

Do not redraw, recolor, crop, mask, add effects to, or use the Kimi logo as casual widget
decoration. Use official logo assets only when the widget is explicitly about brand identity,
product identity, or a Kimi surface.

Ordinary widgets should express Kimi through grid, type, palette roles, icons, and visualized
reasoning. If a preview needs to show the logo rule, show the boundary and safe-space logic rather
than inventing a logo treatment.
