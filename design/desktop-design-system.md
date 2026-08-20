# Crew Desktop Design System

> Status: proposed specification v1.7 for the desktop UI refactor.
>
> Product: Crew desktop application.
>
> Scope: `Ace/desktop` and its preview mode.
>
> Visual reference: [ace-ui-design-system.html](./ace-ui-design-system.html).
>
> This document defines the target contract. It does not claim that the current
> `desktop` CSS or `gemini_v3` preview already complies with it.

## 0.1 Brand assets, external-agent identity, and system menu bar

The approved visual source files are preserved in `design/reference-assets/`.
Production consumers use the following small, stable set of assets:

| Surface | Asset | Rule |
| --- | --- | --- |
| Desktop/Web product logo and Crew assistant avatar | `crew-logo-trans.png` | Use the supplied brand mark; preserve its transparent background and aspect ratio. |
| 外援 navigation/mode entry | `external-agent.png` | Use this standalone monochrome mark for the 外援 navigation and mode entry only. Session, Composer-agent, and “我的外援” identity surfaces use the pixel initial button below. |
| Desktop running-intro character | `crew-jump-agent.png` | Use the approved small yellow assistant with its white face and generous transparent canvas for the Composer running hint. Keep the restrained hop animation and DOM reuse; do not crop or enlarge the character; load the production copy through a bundle-relative path. |
| macOS/desktop menu bar | `desktop/assets/menubar/{default,working,notification,done,rest}.png` | Default and rest use transparent black-line Template Image assets; working, notification, and done retain their supplied color. Never use a white-only or filled color mascot directly as a Template Image. |

The menu-bar status contract is shared by Desktop Renderer and the main-process
tray service:

1. Priority is `有通知 > 工作中 > 完成 > 休眠 > 默认`.
2. A new Work notification latches `有通知` until the user clicks the menu-bar
   icon. The same click clears notification and completion indicators and opens
   Crew.
3. A session that finishes in the background shows `完成` until that click;
   active-session completion is not treated as an unread completion.
4. When there is no active task, no pending attention, and no completion state
   for five continuous minutes, the icon changes to `休眠`. Otherwise it remains
   `默认`.
5. Renderer reports only the semantic state through IPC; image paths, scaling,
   tooltip text, and raster/template policy remain main-process owned. The
   default/rest production PNGs use solid black primary linework and state marks
   on transparency so macOS can recolor them by theme. The active default source
   is `design/reference-assets/crew-menubar-default-template-2.png`; the active
   rest source is `design/reference-assets/crew-menubar-rest-template-2.png`.
6. macOS assets render as a 44x44 physical-pixel representation with scale
   factor 2, producing a 22pt Retina status item. Do not resize directly to a
   single-density 22x22 bitmap; that path visibly pixelates line art.
7. State artwork is normalized optically, not only by source-canvas dimensions.
   Default/rest stay at the canonical scale; the smaller `done` mascot is
   enlarged 1.16x before the final 44x44 crop, while its celebratory decoration
   remains secondary. Working receives only a restrained 1.02x correction.

External-agent session identity is rendered as `kimi · Agent` (agent name first)
wherever the session identity label is shown. The supplied external-agent mark
is only used for navigation and mode entry; session identity surfaces use the
provider/display badge initial so the label and avatar follow the same rule.

The default Composer mode uses the supplied Crew black-line mark from
`crew-menubar-default-template-2.png` in both its trigger chip and the
“智能体” popover row. The Composer 外援 entry uses the
standalone `external-agent.png` mark; after a specific external agent is
selected, the trigger changes to that provider's initial avatar. Icons keep the
existing 16–18px optical size and do not change selection, disabled, or popover
behavior.
When “新建对话” is invoked from an external Agent or Team session, the new
normal Crew draft resets to the configured Crew default model. External runtime
model ids are session-owned and must never leak into a new Crew draft.

In “我的外援” cards, Composer external-agent lists, session history, and ACP
conversation turns, the identity is a single pixel-cute initial button. The
letter uses the existing `display_badge` rule when supplied (for example kimi
→ `K`); if the field is absent, it falls back to the first uppercase character
of `provider`. These identity buttons use a chunky rounded outline, muted
surface, compact monospace letter, and restrained inset depth; they do not
combine `external-agent.png` with a provider badge. The standalone
`external-agent.png` remains reserved for the 外援 navigation/entry icon.
Long Composer lists must keep the mode switch and search field fixed while the
agent/team list owns the scroll viewport.

Provider tone is deterministic: known providers use a stable pattern slot and
other providers use a normalized-name hash across six slots. All provider
avatars use the current theme's black/white surface and ink; the provider is
distinguished only by a quiet hard-edged pixel pattern (checker, grid, stripe,
or cross). The same initial, slot, and full-surface pattern are shared across
session, Composer, and Hub avatar sizes so the provider remains recognizable
without introducing a provider color palette.
The stable slots are: Kimi diagonal stair, Codex checker, Hermes vertical bars,
Claude horizontal bars, Gemini double dots, and Sites compact plus.
The tile repeats from the avatar's top-left corner across the complete surface
at a 4–8px rhythm and uses 10% theme ink. This keeps the full avatar visually
intentional while the initial remains the strongest element. The pattern must
not introduce provider-specific color or high-contrast decoration.

Runtime cards under “发现外援” use the same standalone `external-agent.png`
mark as the Sidebar 外援 navigation item. They do not use the provider initial
avatar, because they identify an external runtime integration rather than a
configured agent identity.

In the main conversation, every assistant turn (built-in Crew, an external
provider, or an external team) is inset by 16px so its avatar center aligns
with the running-intro assistant below while preserving the avatar-to-name and
avatar-to-body spacing. User turns are inset by the same 16px from the right,
forming a symmetric conversation gutter without changing bubble padding. The
running-intro slot uses the same 920px centered content track and 16px inner
gutter as messages, with a 38px avatar axis and 12px text gap. Alignment must be
derived from that shared responsive geometry; fixed negative translations are
not allowed. The approved yellow assistant retains its small subject scale and
white face inside the transparent source canvas.

## 1. Purpose

The design system exists to make current and future Crew pages feel like
one product while allowing each page to retain the layout required by its job.

It must:

1. Preserve the current light, quiet, work-focused product character.
2. Replace page-specific visual inventions with shared foundations and patterns.
3. Let a developer create a new page without inventing colors, spacing, controls,
   states, or responsive behavior.
4. Keep behavior, accessibility, and long-content handling part of the design
   contract, not optional polish.
5. Provide one preview mode that renders production UI with realistic fixtures.
6. Make CSS ownership and verification rules explicit enough to prevent another
   cascade of patch files.

## 2. Source hierarchy

Behavior and appearance have different authorities:

1. Approved product and feature specifications define behavior. For the office
   assistant, use `Ace/docs/specs/work-mode.md`.
2. This specification is the normative source for visual, interaction,
   accessibility, responsive, and CSS ownership contracts.
3. The Living Design System HTML is the executable reference and visual
   acceptance surface for this specification. It demonstrates the contract but
   does not independently redefine it.
4. Focused behavior documents in `Ace/docs/desktop` and
   `Ace/docs/frontend` define already implemented feature details.
5. The historical Soft Pixel direction in
   `Ace/doc/desktop/crew-soft-pixel-ui-design.html` is visual reference.
6. Existing production behavior and verified accessibility wins are preserved
   unless an approved specification changes them.
7. Existing production styling and the current `gemini_v3` appearance are
   migration evidence, not design authority.
8. For existing general-assistant surfaces, the current `master` branch is the
   layout-parity baseline for region ownership, pane position, scroll owner,
   overlay anchor, and mode-specific information. It is not a color, component,
   icon, or CSS implementation authority.

When a product requirement and a visual rule appear to conflict, preserve the
product behavior and adapt it using this system rather than silently dropping
the behavior.

If this document and the Living HTML disagree, this document controls. The
discrepancy must be resolved in the same change: either correct the HTML or,
when the HTML exposes a missing shared rule, amend this document first and then
update the HTML. Production styling never becomes the standard merely because
it already exists.

## 3. Product design principles

### 3.1 Quiet workspace

Crew is an operational desktop tool. Content, status, comparison, and
repeated actions take priority over decorative composition.

- Use pure white as the light canvas and pure black as the dark canvas. Neutral
  gray may separate regions, borders, and secondary content; normal product
  chrome must not carry a gray-blue or other chromatic cast.
- Prefer whitespace, alignment, and thin dividers over floating cards.
- Keep primary actions visible without making every action bright.
- Do not use gradients, neon glow, glassmorphism, decorative color bars, or
  marketing-page composition in product views.

### 3.2 One system, several page shapes

Pages may use grids, tables, lists, split panes, boards, or chat streams. They
must still share:

- page header anatomy;
- typography and spacing scales;
- control heights and radii;
- semantic colors and state language;
- loading, empty, error, and permission states;
- minimum-window behavior;
- focus and keyboard behavior.

Uniformity means shared rules, not forcing every page into a card grid.

### 3.3 Borders before shadows

Use a `1px` semantic border for stable structure. Use shadows only when an
element leaves normal document flow, such as a menu, popover, dialog, toast, or
floating composer.

Cards do not lift on hover unless the entire card is an interactive command.

### 3.4 Motion communicates change

Motion is for feedback, state transition, and spatial continuity.

- No spring bounce, glow pulse, or continuous motion for decoration.
- Press feedback may move a control down by `1px`; do not scale it.
- Only live or indeterminate status may animate.
- All non-essential motion must stop under `prefers-reduced-motion`.

### 3.5 Exceptions stay local

The pixel-art Studio may use a dark scene canvas with warm cream/yellow pixel
chrome and its own typography. The composer, toolbar, drawer, popovers, and
other interactive chrome use the warm pixel surface; the room artwork may keep
its dark canvas. Its skin is scoped to the Studio root. It must not change the
global document theme or leak selectors into normal product pages.

### 3.6 One product, two assistant modes

Crew has two user-facing product modes:

- `Crew 通用助手`;
- `Crew 办公助手`.

They share the application shell, account, model, Workspace, security,
Agent/Team, Skill, MCP, expert, plugin, browser, and file foundations. They do
not share one undifferentiated navigation or history list.

Rules:

1. Product mode is an application-level context, not a chat segmented control.
2. The mode selector lives at the top-left identity position and displays the
   full current name.
3. Each mode restores its own last location and navigation state.
4. A conversation retains the mode in which it was created.
5. The office assistant may show Agent conversations as clearly labelled,
   read-only history and context sources.
6. The general assistant does not mix office items into its history.
7. Product mode and Agent execution mode are separate concepts in code and UI.
8. Chat history is mounted only for a chat workspace. Hub, Task, Audit,
   Security, System, Settings, and other feature pages do not reserve a history
   column merely because the last route was a conversation.
9. Context-collapse belongs to the conversation Inspector or its adjacent pane.
   It is not a persistent primary-navigation command.
10. General-assistant history preserves the `master` information density and
    fields. Work history may add item, source, snapshot, and synchronization
    metadata, but those fields never leak into general-assistant rows.

### 3.7 Current Desktop surface baseline

The July 2026 master merge added or materially changed product surfaces that
must be preserved during the UI refactor. They are part of the migration
baseline, even when their current CSS is not yet compliant.

| Surface | Current capability | Design-system responsibility |
| --- | --- | --- |
| Agent Hub | Runtime discovery, external Agent and Team creation, assignment, model selection | list-management structure, entity identity, forms, availability and feature-gated states |
| Wiki workspace | source navigation, Markdown detail, upload/compile progress, graph exploration, persistent Wiki Agent | three-region knowledge layout, local scrolling, graph controls, ingest state, embedded chat |
| Team collaboration | members, dependency graph, task nodes, execution events, artifacts, cancellation | inspector/board pattern, status hierarchy, dense operational scanning |
| Chat interaction | process timeline, follow-up questions, permission approval, images, attachments, model state | one turn structure, blocking approval pattern, media actions, busy/idle semantics |
| Skills and plugins | installed/market views, search, categories, install/uninstall, account-level plugin toggle | shared Hub shell, persistent search focus, card action hierarchy, policy-disabled state |
| History and identity | Crew, external Agent, and Team sessions | stable identity marker plus text; identity never relies on color alone |
| Usage | provider-reported and estimated token records | data provenance badge and consistent numeric presentation |
| Studio | scoped dark skin, reduced chat chrome | local-skin exception without global token leakage |

Newly merged behavior is not permission to copy its raw colors, duplicate
selectors, oversized radii, or feature-local controls into the target system.
Migration preserves capability and verified interaction behavior while
re-expressing appearance through the shared contracts below.

## 4. Theme contract

Supported user choices:

- `system`
- `light`
- `dark`
- `high-contrast`

The light theme is the brand baseline and the primary visual acceptance target.
Its application, canvas, and normal surface base is `#FFFFFF`. The dark theme
uses `#000000` for the application and canvas base. Neutral surfaces step away
from those bases only enough to express hierarchy; they never introduce a blue,
purple, brown, or other color cast.

Removed choices:

- `sepia`
- `sidebar-gray`
- user-selectable accent colors

The product mark keeps its brand colors, but routine product chrome and primary
commands use the theme's black/white action pair. Status colors are semantic
and cannot be repurposed as page decoration. Entity illustration and chart
colors are limited token families, not page accents.

Theme rules:

1. Theme selection is stored once and applied once before first paint.
2. `system` resolves to `light` or `dark`.
3. Component CSS reads semantic or component tokens only.
4. Theme selectors override semantic tokens, not individual page selectors.
5. A page cannot switch to a different theme on its own.
6. The Studio skin is a scoped exception, not a theme preference.
7. `high-contrast` remains an independent explicit mapping; it is not obtained
   by increasing contrast on the normal dark theme.
8. Theme choice, persistence key, `system` resolution, and pre-first-paint
   application remain behaviorally unchanged while token values are replaced.

## 5. Token architecture

Tokens have three layers:

```text
primitive values -> semantic meaning -> component contract
```

### 5.1 Naming

```text
Primitive:  --mw-gray-100, --mw-space-3, --mw-radius-2
Semantic:   --mw-bg-canvas, --mw-text-primary, --mw-border-default
Component:  --mw-button-bg, --mw-input-border-focus, --mw-table-row-hover
```

Rules:

- Raw color values exist only in `tokens.css` primitive declarations. Theme
  mappings, component rules, shell rules, feature rules, skins, utilities,
  charts, and page rules reference tokens; they never contain raw color values.
- Named `white` and `black` values are raw colors too; feature CSS uses
  `--mw-white` and `--mw-black`, and the production color audit blocks regressions.
- Themes override semantic tokens by referencing primitives.
- Component tokens reference semantic tokens.
- Do not create aliases solely to preserve old names.
- Add a component token only when a shared component needs a stable contract.
- Feature-local values belong under the feature root, not `:root`.

## 6. Primitive tokens

### 6.1 Neutral and semantic primitive palette

| Token | Value | Purpose |
| --- | --- | --- |
| `--mw-white` | `#FFFFFF` | light app, canvas, and surface base |
| `--mw-black` | `#000000` | dark app/canvas and light primary action base |
| `--mw-gray-25` | `#FAFAFA` | light raised or grouped surface |
| `--mw-gray-50` | `#F7F7F7` | light subtle fill |
| `--mw-gray-100` | `#F0F0F0` | light selected and separated region |
| `--mw-gray-150` | `#E8E8E8` | subtle border |
| `--mw-gray-200` | `#D9D9D9` | default border |
| `--mw-gray-300` | `#B8B8B8` | strong border and disabled control edge |
| `--mw-gray-500` | `#6B6B6B` | muted text; WCAG AA where required |
| `--mw-gray-650` | `#4F4F4F` | secondary text |
| `--mw-gray-900` | `#171717` | light primary text and dark raised surface |
| `--mw-gray-950` | `#0A0A0A` | dark subtle surface |
| `--mw-brand-500` | product-mark value | product identity only, never routine chrome |
| `--mw-success` | semantic green | success text and icon |
| `--mw-warning` | semantic amber | warning text and icon |
| `--mw-danger` | semantic red | destructive text and icon |
| `--mw-info` | semantic blue | informational state only |
| `--mw-chart-*` | finite chart series | charts and legends only |
| `--mw-symbol-*` | finite entity palette | product entity illustrations only |

Status soft fills, chart series, and entity colors are declared once in the
primitive section and must maintain text/boundary contrast. Their raw values
never appear in chart configuration, SVG consumers, or feature CSS.

### 6.2 Theme base mappings

| Semantic role | Light | Dark | High contrast |
| --- | --- | --- | --- |
| app / canvas | `--mw-white` | `--mw-black` | explicit HC base |
| surface | `--mw-white` | `--mw-gray-900` | explicit HC surface |
| subtle / selected | neutral gray primitives | neutral gray primitives | explicit HC pair |
| primary text | `--mw-gray-900` | `--mw-white` | explicit HC foreground |
| primary action | `--mw-black` on `--mw-white` | `--mw-white` on `--mw-black` | explicit HC action pair |

Changing theme values must be possible by editing primitive/semantic mappings
without touching feature selectors. If a theme change requires page-by-page
overrides, the ownership model has failed.

### 6.3 Spacing

Use a `4px` base grid.

| Token | Value |
| --- | --- |
| `--mw-space-0` | `0` |
| `--mw-space-1` | `4px` |
| `--mw-space-2` | `8px` |
| `--mw-space-3` | `12px` |
| `--mw-space-4` | `16px` |
| `--mw-space-5` | `20px` |
| `--mw-space-6` | `24px` |
| `--mw-space-8` | `32px` |
| `--mw-space-10` | `40px` |
| `--mw-space-12` | `48px` |

Values outside this scale require a geometry reason, such as icon dimensions or
one-pixel borders.

### 6.4 Radius

| Token | Value | Use |
| --- | --- | --- |
| `--mw-radius-1` | `4px` | compact badges and tiny controls |
| `--mw-radius-2` | `6px` | buttons, inputs, tabs |
| `--mw-radius-3` | `8px` | panels, cards, tables |
| `--mw-radius-4` | `12px` | popovers, dialogs, composer |
| `--mw-radius-full` | `9999px` | status dots, avatars, short badges only |

Do not use `16px`, `20px`, or `24px` as routine card radii.

### 6.5 Elevation

| Token | Value | Use |
| --- | --- | --- |
| `--mw-shadow-none` | `none` | normal content |
| `--mw-shadow-raised` | `0 2px 0 rgb(46 51 54 / 6%)` | pressed-material detail |
| `--mw-shadow-float` | `0 10px 28px rgb(46 51 54 / 12%)` | menu and popover |
| `--mw-shadow-overlay` | `0 18px 48px rgb(46 51 54 / 18%)` | dialog and drawer |

### 6.6 Motion

| Token | Value | Use |
| --- | --- | --- |
| `--mw-duration-fast` | `100ms` | press and hover |
| `--mw-duration-normal` | `160ms` | menus and local disclosure |
| `--mw-duration-slow` | `220ms` | dialogs and drawers |
| `--mw-ease-out` | `cubic-bezier(0.22, 1, 0.36, 1)` | entry and movement |
| `--mw-ease-standard` | `cubic-bezier(0.4, 0, 0.2, 1)` | color and opacity |

Never use `transition: all`.

## 7. Semantic tokens

Every theme must provide:

```css
--mw-bg-app;
--mw-bg-sidebar;
--mw-bg-canvas;
--mw-bg-surface;
--mw-bg-subtle;
--mw-bg-selected;
--mw-bg-overlay;

--mw-text-primary;
--mw-text-secondary;
--mw-text-muted;
--mw-text-inverse;
--mw-text-selected;
--mw-text-link;

--mw-border-subtle;
--mw-border-default;
--mw-border-strong;
--mw-focus-ring;

--mw-action-primary;
--mw-action-primary-hover;
--mw-action-primary-text;
--mw-action-danger-text;

--mw-status-success;
--mw-status-success-bg;
--mw-status-warning;
--mw-status-warning-bg;
--mw-status-danger;
--mw-status-danger-bg;
--mw-status-info;
--mw-status-info-bg;
```

Semantic tokens must resolve to a primitive or another previously resolved
token. A declaration must never reference itself, including through a fallback.
Typography tokens derived from the adjustable UI base size must resolve to
stable integer pixels at every supported setting so layout and visual checks do
not drift through floating-point rounding.

Dark and high-contrast themes change these meanings. They do not restyle
individual features.

State colors are foreground/background pairs. A component cannot use
`--mw-bg-selected` without `--mw-text-selected`, or a semantic action
background without its matching text token.

Required contrast pairs:

| Pair | Light | Dark | High contrast |
| --- | --- | --- | --- |
| Selected | primary text on neutral selected fill | primary text on neutral dark selected fill | black on white |
| Primary action | white on black | black on white | explicit high-contrast pair |
| Danger action | danger pair | danger pair | explicit high-contrast danger pair |

The selected pair applies to navigation, segmented controls, selected rows,
secondary emphasis buttons, and text selection. Important selected states also
use structure such as an indicator, check, border, or `aria-current`; color is
not the only signal.

## 8. Typography

### 8.1 Font families

```css
--mw-font-sans:
  -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
  "Microsoft YaHei", system-ui, sans-serif;

--mw-font-mono:
  "SFMono-Regular", Consolas, "Liberation Mono", monospace;
```

Do not depend on fonts that are not bundled with the application. Emoji are not
navigation or control icons.

### 8.2 Type scale

| Role | Size / line-height | Weight | Use |
| --- | --- | --- | --- |
| Page title | `24px / 32px` | `650` | one per page |
| Dialog title | `20px / 28px` | `600` | modal and drawer title |
| Section title | `16px / 24px` | `600` | page sections |
| Item title | `14px / 22px` | `600` | list and card title |
| Body | `14px / 22px` | `400` | default content |
| Compact body | `13px / 20px` | `400` | dense rows and sidebar |
| Label | `13px / 20px` | `500` | fields and control labels |
| Caption | `12px / 18px` | `400` | timestamps and metadata |
| Code | `12px / 19px` | `400` | code, ids, commands |

The production token names are:

```text
--mw-type-page-{size,line,weight}
--mw-type-dialog-{size,line,weight}
--mw-type-section-{size,line,weight}
--mw-type-item-{size,line,weight}
--mw-type-body-{size,line,weight}
--mw-type-compact-{size,line,weight}
--mw-type-label-{size,line,weight}
--mw-type-caption-{size,line,weight}
--mw-type-code-{size,line,weight}
```

Rules:

- Letter spacing is `0`.
- Text uses `--mw-type-*` tokens when it matches the canonical scale. Deliberate
  intermediate text sizes use `calc()` from `--mw-font-ui-size` so the default
  remains exact and the user font-size preference still applies.
- Pixel font sizes are reserved for glyphs inside fixed-geometry controls such
  as close buttons, carets, avatars, and icon-only actions. Every exception is
  registered by selector and value in `audit-font-sizes.mjs`; an unregistered
  pixel size fails the audit.
- Do not use uppercase transformation for Chinese table headers.
- Use tabular numerals for metrics, durations, and aligned numeric columns.
- Truncate only when the full value is available through a tooltip or detail
  view.
- Page descriptions should be one concise sentence and use secondary text.

## 9. Icons and imagery

1. Reuse the canonical product sprite and the existing product icon family.
2. Use one stroke language: `1.75px` to `2px`, round caps and joins.
3. Normal UI icon sizes are `16px`, `18px`, `20px`, and `24px`.
4. Icon-only buttons require an accessible name and tooltip.
5. Status icons must be paired with text when the meaning is important.
6. Do not hand-draw a second icon for an existing action.
7. Agent avatars may carry identity color; page chrome may not inherit it.
8. Primary navigation and page-level feature commands use a `20–24px` visual
   size inside stable `32–40px` hit targets. Their drawn shape should occupy at
   least 72% of the icon viewBox so a nominally correct icon does not look tiny.
9. Equivalent icons normalize viewBox, optical center, stroke weight, and
   baseline. File, folder, settings, navigation, and welcome icons must not mix
   thin miniature glyphs with heavier peers.
10. Yellow/amber is reserved for warning state or an explicitly documented
    entity illustration. It is not the default folder, security, or navigation
    color.
11. The `128px` application rail uses `22px` navigation and footer-command icons
    beside centered labels. At viewports below `1180px`, the rail uses the
    `56px` compact token and hides labels while retaining accessible titles and
    the Crew mark. Rail instances use `2px` primary strokes and `1.75px`
    secondary strokes; their outline follows row text color while entity fills
    retain the restrained product symbol palette.

### 9.1 Icon families

The target Renderer has one SVG sprite as the single source of truth. Its
symbols belong to three different families and must not be treated as
interchangeable decoration.

| Family | Existing symbols | Purpose |
| --- | --- | --- |
| Action | `icon-plus`, `icon-send`, `icon-refresh`, `icon-expand`, `icon-search`, `icon-filter`, `icon-settings`, and related `icon-*` symbols | commands in buttons, menus, fields, and toolbars |
| Product entity | `skill-badge`, `icon-expert-picker`, `icon-agent`, `icon-team`, `icon-wiki`, `avatar-*`, `team-*` | identify a Skill, Expert, Agent, Team, Wiki, or Team role |
| Runtime state | `status-running`, `status-waiting`, `status-complete`, `loading-*` | communicate execution state or indeterminate activity |

Rules:

1. Do not use a product entity symbol as a generic command icon.
2. Do not use a runtime state symbol as a category or identity marker.
3. A Skill uses `skill-badge`; an Expert picker uses `icon-expert-picker`;
   ordinary Agent and Team references use `icon-agent` and `icon-team`; Wiki
   navigation and knowledge sources use `icon-wiki`.
4. Named Agent avatars and Team-role illustrations are used only where
   identity helps scanning, such as list rows, selectors, assignment views,
   process timelines, and detail headers.
5. Product symbols may use the restrained Soft Pixel illustration palette.
   Their colors do not become page accent colors.
6. Important runtime state always includes a text label. Animation is limited
   to currently running, connecting, streaming, or indeterminate activity.

### 9.2 Product symbol contract

Canonical source:

```text
Ace/desktop/assets/crew-ui-symbols.svg
```

Production components reference symbols from this sprite through local
`#semantic-id` values. Electron uses `file://`, where Chromium does not render
cross-file SVG `<use>` references, so the Desktop build injects this canonical
source into the generated Renderer HTML. The Living HTML carries a generated,
byte-checked projection of the same source for standalone viewing. Neither
generated projection is an independently editable icon source. Do not copy path
data into page templates or maintain a feature-specific sprite.

| Context | Size | Treatment |
| --- | --- | --- |
| Compact list or selector | `24px` | entity symbol plus visible label |
| Standard list, card, or process row | `32–36px` | full entity symbol or stable data-provided monogram |
| Detail header or empty state | `40–48px` | full entity symbol; never hero scale |
| Inline runtime state | `16–20px` | status symbol plus text |
| Loading indicator | `16–24px` | only while activity is real |

Skill, plugin, and expert data may provide a stable glyph, short monogram, or
restrained entity color. The Renderer must preserve that identity and provide a
neutral fallback; replacing every entity with the same gray placeholder loses
meaning and is not a valid migration.

The product illustration palette is deliberately separate from semantic status
colors:

| Token | Light value | Use |
| --- | --- | --- |
| `--mw-symbol-line` | `#514B43` | illustration outline and facial detail |
| `--mw-symbol-shell` | `#C7D9E6` | robot and Team shell |
| `--mw-symbol-face` | `#F7F5EE` | face and paper surface |
| `--mw-symbol-accent` | `#B8C6AF` | primary identity detail |
| `--mw-symbol-accent-alt` | `#E7D29B` | secondary identity detail |

Dark and high-contrast themes remap these semantic symbol tokens. Feature CSS
cannot override them globally. A specific Agent identity may override the two
accent tokens on its own symbol root, but it must preserve line and surface
contrast. Canonical sprite declarations must reference semantic tokens without
embedded color fallbacks; missing token wiring is a build-time defect, not a
reason to silently restore a legacy color.

The Living Design System must display every currently supported product entity
and runtime-state symbol in light, dark, and high-contrast themes. During the
specification-only phase its inline symbols are proposals and migration
evidence, never a source to copy back into production. The first Renderer
migration slice must consolidate the approved symbols into the canonical
production sprite and make both production preview and Living HTML consume that
asset. After that point, when the sprite adds or removes a symbol, its visual
inventory and this table are updated in the same change.

The merged Wiki navigation icon and external-Agent navigation mark currently
exist as feature-local inline SVG. Their path data must be consolidated into the
canonical sprite during migration; the design system documents their semantic
role now and does not authorize another copied icon.

## 10. Shared component contracts

Production DOM behavior is owned by
`desktop/src/ui/components/controls.ts`; production presentation is owned by
`desktop/assets/styles/components.css`. New feature code composes these
controls and may position their root elements, but must not copy their markup
or redefine `.mw-button`, `.mw-field`, `.mw-tab`, `.mw-badge`, `.mw-status`, or
`.mw-list` visual states in feature CSS.

### 10.1 Buttons

Variants:

- primary;
- secondary;
- outline;
- ghost;
- danger;
- icon.

Sizes:

| Size | Height | Horizontal padding | Icon |
| --- | --- | --- | --- |
| small | `28px` | `10px` | `14–16px` |
| default | `32px` | `12px` | `16px` |
| large | `36px` | `16px` | `18px` |
| icon | `32px` square | `0` | `16–18px` |

State priority:

```text
disabled > loading > active > focus-visible > hover > default
```

Behavior:

- Hover changes fill or border only.
- Active may use `translateY(1px)`.
- Focus uses a visible `2px` ring.
- Loading preserves width and replaces the leading icon with a spinner.
- Disabled remains readable and cannot be activated.
- Danger requires confirmation when the action is not easily reversible.

### 10.2 Form controls

Text input, select, date/time input, and compact search use native controls
where they satisfy the requirement.

- Default height: `32px`.
- Large or composer-adjacent control: `36px`.
- Border: `1px solid var(--mw-border-default)`.
- Radius: `6px`.
- Labels are visible; placeholder text is not a label.
- Helper and error text occupy a stable area when layout shift would be
  disruptive.
- Focus is indicated by border and ring, not glow.
- Validation uses icon, message, and color.

Checkboxes, radios, and switches must preserve native keyboard semantics.

### 10.3 Tabs and segmented controls

- Tabs switch views or content regions.
- Segmented controls switch a mode among two to four options.
- Pills represent filters or tags, not navigation.
- Active state uses selected fill, primary text, and optional bottom indicator.
- Counts use a separate compact badge.
- Count badges use a fixed `20px` square or minimum-width box, center their
  tabular numeral in both axes, and reserve the same track for zero and
  non-zero values. Active color may change; geometry and baseline may not.
- Tab rows may scroll horizontally only inside their own container.
- Category and filter collections are not tab rows. They wrap at supported
  widths or collapse into a labelled menu/select when wrapping would consume
  excessive space; they never expose a clipped horizontal scrollbar across the
  page toolbar.

### 10.4 Badges and status

- Badge height: `20px` or `24px`.
- Short labels may use full radius.
- Status color never appears without text or icon.
- A pulsing dot is reserved for a currently running or connecting process.
- Success, warning, danger, and info are not category colors.
- User-provided tags use neutral fills by default.

### 10.5 Cards and panels

Use a panel only when content needs a stable visual or interaction boundary.

- Default radius: `8px`.
- Default border: `1px`.
- Default shadow: none.
- Default padding: `16px`.
- Large functional panel padding: `20px` or `24px`.
- Do not place a card inside another card to create spacing.
- Do not add hover elevation to non-interactive panels.

### 10.6 Tables

- The table owns its horizontal scroll container.
- Text aligns left; numbers align right; actions align right.
- Default row height: `48px`; compact: `40px`.
- Header stays sentence case and uses secondary text.
- Rows use horizontal dividers; avoid full cell grids.
- Hover uses subtle neutral fill.
- Selected state uses `--mw-bg-selected`.
- Long values truncate consistently and expose the full value.
- Empty, loading, error, and filtered-empty states render inside the table
  region without changing the page skeleton.

### 10.7 Dialogs, drawers, popovers, and toasts

Production lifecycle and focus behavior are owned by
`desktop/src/ui/components/overlays.ts`; presentation is owned by
`desktop/assets/styles/overlays.css`. New overlays must extend this owner rather
than attach feature-local `document` or `window` listeners. Closing or disposing
an overlay must remove every listener it registered.

- Modal: blocking decision or focused form.
- Drawer: detail that must remain adjacent to page context.
- Popover: short contextual choice.
- Toast: non-blocking result that does not require a decision.

Requirements:

- Every modal backdrop uses the theme-resolved `--mw-bg-overlay`. It is
  achromatic in every theme; brand, accent, status, and text colors must not be
  mixed into a backdrop.
- Nested settings forms use the same backdrop contract. Only the dialog
  content, entity icon, and semantic state inside the dialog may carry color.
- Preview or fixture styles must not redefine production overlay selectors.
- Focus enters and remains in a modal.
- Escape closes dismissible overlays.
- Closing returns focus to the trigger.
- Footer actions follow `cancel -> primary` order.
- Destructive confirmation names the affected object.
- Overlay motion is `160–220ms` and has a reduced-motion fallback.
- Toasts do not cover primary navigation or the composer.

### 10.8 Permission and follow-up decisions

Follow-up questions and security approvals share overlay foundations but remain
different components:

- a normal follow-up stays in conversation flow when it is part of the task;
- a permission request blocks the affected execution and uses a modal;
- permission content names the actor, requested operation, target, and scope;
- permission actions use `拒绝`, `仅本次允许`, and only policy-supported
  persistence choices;
- Team-originated approval shows Team identity; external-Agent approval shows
  the Agent identity;
- pending submission disables every competing action and keeps the decision
  visible;
- permission dialogs never offer an unrelated free-text business response.

Do not turn every question into a modal. Blocking presentation is reserved for
an action that cannot safely continue without a decision.

## 11. Content, Markdown, and chat

### 11.1 Markdown

- Body line height: `1.6`.
- Headings use typography and spacing, not decorative accent bars.
- Blockquotes use a subtle border and fill.
- Inline code uses a neutral tinted background.
- Code blocks may use a dark code surface inside the light application.
- A fenced code block owns its foreground/background pair. A light code
  container uses primary text; a dark code container uses code text. Nested
  `pre`/`code` rules must not mix the light container with the dark-surface
  foreground, and the resulting contrast is at least `4.5:1`.
- Tables remain horizontally scrollable within the message width.
- Links are visibly distinct and keyboard focusable.

### 11.2 Assistant turn structure

The rendering contract in `desktop/docs/assistant-segment-role.md` remains
authoritative:

- process narration, thinking, tool calls, status, and recoverable errors live
  in the process region;
- the final answer lives in the answer region;
- pure direct answers do not show an empty process disclosure;
- streaming must not cause avoidable movement between regions.

### 11.3 Process timeline

- One vertical timeline groups process items.
- Each process item reserves a `28px` icon column and a flexible content column;
  the icon is normally `20-21px`.
- Tool calls use semantic icons. A generic status dot must not replace tool
  identity:

| Process kind | Icon meaning |
| --- | --- |
| Thinking | light bulb |
| Write, edit, patch | pencil |
| Read | open book |
| Search and file discovery | magnifier |
| Web, browser, vision | globe |
| Todo and plan tracking | checklist |
| Agent, expert, and Team delegation | people |
| Memory | database |
| Skill | star |
| Cron and timed status | clock |
| Shell, terminal, and uncategorized MCP tool | terminal window |
| Recoverable error | exclamation in a circle |

- Icon shape identifies the capability; color and motion identify execution
  state. Never rely on color alone.
- Adjacent items use a restrained dashed connector behind the icon column.
- Process narration may intentionally use an empty icon slot, but remains
  aligned with tool items.
- Running items may animate; completed items are static.
- Tool request and response details use disclosure controls.
- Request/Response payloads use the code-surface foreground and background as
  one pair; normal-size payload text maintains at least `4.5:1` contrast in
  light, dark, and high-contrast themes.
- Security approval summaries use the surface text token rather than inheriting
  the global code-block foreground from their `pre` element.
- Thinking is expanded while streaming and collapsed after completion. Tool
  request/response details are collapsed by default after completion.
- The outer disclosure summarizes elapsed time and tool count, for example
  `已执行 12s，已调用 3 个工具`. Pure direct answers show no disclosure.
- Error items remain readable without turning the entire message red.
- Durations use tabular numerals.
- Use the trusted static SVG family already owned by the Desktop renderer.
  Pages must not substitute emoji, text initials, duplicated SVG definitions,
  or feature-local icon mappings.

### 11.4 Composer

- The composer is a functional floating panel, so `12px` radius and a restrained
  shadow are allowed.
- The text area is the dominant region.
- Secondary controls remain one visual level below Send/Stop.
- The primary toolbar order is stable: attachment and the consolidated
  Agent/Skill/MCP capability menu on the left, the conversation security mode
  immediately after them, and the model selector plus Send/Stop on the right.
  Voice input is not part of the Desktop Composer.
- Project context is shown as one compact chip on a subtle full-width backing
  strip immediately above the Composer while the conversation is still a
  draft. Once content exists the strip is removed instead of leaving disabled
  project chrome. The chip itself adds no second border or gray fill. It is not
  repeated as a toolbar control. Its popover uses a flat search row followed by
  `project list -> + create project / × work without a project`; the Renderer
  continues to use the existing Workspace state and access boundary underneath.
- The capability menu may expose Agent and Skill choices and the connection
  state of globally available MCP servers. It opens below its circular trigger;
  hovering or focusing a row opens that row's detail beside the parent without
  replacing it. Hovered, focused, and selected rows use the same quiet neutral
  state. It must not imply per-conversation MCP enable/disable behavior unless a
  real backend contract exists.
- The model selector opens a compact flat list below its trigger. Optional model
  descriptions belong to hover help rather than permanent nested cards.
- The security selector offers only the policy-supported Crew modes:
  `请求批准`, `替我审批`, and `完全访问权限`. It does not expose a
  free-form or custom policy editor.
- Busy, queued, offline, and permission-blocked states are explicit.
- Preview controls must never overlap the composer.

### 11.5 Context references

The office assistant composer uses `@` as the unified context entry point.

Supported result families are files and folders, Work items, Work
conversations, Agent conversation snapshots, personal knowledge, authorized
organization knowledge, and synchronized source records.

- Results are grouped by family and searchable from one popover.
- A selected result becomes a removable reference chip or compact card.
- Every reference exposes its source and access state.
- Agent conversation references are snapshots by default and offer an explicit
  `更新到最新内容` action.
- A text reference never grants access to files mentioned in that text.
- Long reference names truncate, but the full name remains available through
  tooltip or detail.

### 11.6 Images, attachments, and generated artifacts

- An image preview has one visible primary canvas and a stable action footer.
- Supported actions are named and icon-backed: preview, copy, reveal in folder,
  download, or attach.
- The full local path is secondary metadata and wraps safely; it is never used
  as the visible action label.
- Missing, revoked, or inaccessible files retain their message position and
  show a recoverable unavailable state.
- Team and Agent artifacts use the same file-row and image-viewer components as
  ordinary chat.
- Privileged file actions are rendered only from trusted application data;
  Markdown content cannot create executable file controls.

### 11.7 Turn output summaries

Execution outputs appear after the visible answer and before the message footer.
They summarize the current turn and link to the persistent Inspector; they do
not duplicate the complete Inspector content.

The file-change summary:

- appears only when the turn changed at least one non-Plan file;
- says `已编辑 N 个文件` and shows total additions and removals;
- previews at most three files, followed by `再显示 N 个文件`;
- shows path, filename, `新增` / `修改` / `删除`, and per-file `+` / `-`
  counts;
- opens the Inspector Files tab and expands the selected file when its header
  or a file row is activated;
- offers `在资源管理器中显示` only for files that still exist;
- keeps full diff rows out of the persisted chat message.

Additions and removals use success/danger color plus explicit `+` and `-`
signs. Status never relies on color alone. Long directories elide before the
filename; the filename remains visible.

A Plan created in the turn uses a compact entry card with title, status, and
`在看板中审阅` / `在看板中查看`. The chat card never embeds the full editable
Plan. Team and Agent output files use the same artifact and file-row contracts.

### 11.8 Inspector Files and diff

The Files tab is the session-level source for file changes. Its `本次会话改动`
label must not be confused with the turn-level chat summary.

- The header shows cumulative file count and addition/removal totals.
- File rows show path, status, counts, disclosure state, and a reveal action.
- Several file diffs may remain expanded at once.
- The selected path from a chat summary opens directly.
- Plan documents are owned by the Plan tab and excluded from Files.
- Empty state explains how file tracking is produced; it does not imply that
  the whole task failed.

Diff uses a compact unified view:

- remove `diff --git`, file headers, and hunk-header noise from the primary
  reading path;
- retain old/new line numbers, an explicit `+` / `-` sign column, and code;
- use a restrained success/danger background and a `2px` edge marker;
- preserve context lines and offer controls to reveal omitted regions;
- use language-aware syntax color only as secondary information;
- keep long lines horizontally scrollable inside the diff, never at body level;
- use a softer addition fill when an entire new file is shown.

### 11.9 Inspector views

The Inspector has six owned views. A view may be hidden when it does not apply,
but the same capability must not reappear as a page-local copy:

| View | Ownership |
| --- | --- |
| Context | session identity, model, context usage, token provenance, system prompt disclosure, and raw message inspection |
| Files | session file changes, unified Diff, file identity, open/reveal actions, and unavailable/revoked states |
| Plan | complete Plan lifecycle, document preview/editing, Todo progress, feedback, approval, rejection, cancellation, and read-only state |
| Kanban | session execution nodes and workflow state when a board is useful; it reuses task state rather than inventing a second status model |
| Team | Team identity, members, DAG dependencies, execution events, artifacts, cancellation, and Team-only states |
| Browser | embedded browser navigation, page state, safe handoff, permission boundaries, loading, offline, and failure states |

Context exposes provenance and debugging detail without becoming a settings
page. Raw messages and system prompts are read-only and must not render trusted
HTML. Browser retains the existing main-process security boundary; a URL or
page result never grants file or execution authority.

The Plan tab owns the complete Plan lifecycle:

- pending, editing, revising, approved/read-only, rejected, cancelled, and empty
  states;
- preview/edit segmented control while editable;
- Plan filename, Markdown preview or editor, Todo progress, approval, revision
  feedback, rejection, and cancellation;
- a read-only explanation after approval rather than disabled controls without
  context.

The Team collaboration tab appears only for Team sessions. It owns:

- Team identity and members;
- DAG strategy, dependencies, turns, stages, and task nodes;
- pending, running, blocked, approval, failed, cancelled, and completed states;
- per-node summary, execution events, tool count, artifacts, and cancellation;
- a session file list that reuses shared file identity and open/reveal actions.

Context, Files, Plan, Kanban, Team, and Browser are peer Inspector views, not
page-local popup variants. Chat summary cards navigate to the applicable view;
they must not create a second usage calculator, diff parser, Plan editor, task
state model, Team status model, browser host, or file-action implementation.

## 12. Page shell

### 12.1 Desktop geometry

- Default application window: `1280 x 800`.
- Supported minimum: `960 x 640`.
- Primary verification: `1440 x 1000`, `1280 x 800`, and `960 x 640`.
- The application rail is `128px` at normal desktop widths and uses a `56px`
  compact column below `1180px`. The compact mode hides primary labels after
  preserving their accessible names and title tooltips.
- Page content must not cause body-level horizontal scrolling.
- Tables, code, graphs, and boards may own local scrolling.
- Shell columns are route-owned. A chat history column exists only for a chat
  workspace; route changes must remove its track instead of leaving an empty or
  stale placeholder beside a feature page.
- The general-assistant chat topology follows the verified `master` baseline:
  navigation rail | history | conversation | optional Inspector. Work uses the
  office topology in section 12.5. Shared styling does not merge these layouts.
- The history collapse/restore and collapsed new-conversation commands use
  semantic panel-collapse and conversation-plus symbols rather than a generic
  arrow or bare plus. Workspace-group hover/focus fill belongs to the complete
  row, including its trailing actions; partial-row hover tails are invalid.

### 12.2 Page header

Every non-chat feature page starts with:

```text
Page title                         Primary/secondary actions
One-sentence description           Optional status or refresh
Optional tabs or scoped navigation
```

Rules:

- Title and description align to the content grid.
- Actions are right aligned and wrap below at constrained widths.
- Do not color one word in a page title.
- Do not place the header inside a decorative card.
- Do not repeat the page title in the first content panel.

### 12.3 Content widths

- Data, board, and split-detail pages use the available width.
- Reading content uses `max-width: 72ch`.
- Forms use `560–720px` unless a split layout is justified.
- Card grids use responsive grid tracks, not fixed card widths followed by empty
  space.

### 12.4 Sidebar brand and shell owner

Crew uses one full-height application rail. The brand sits at its top; the
title bar starts to the right and owns only the drag region and native window
commands. The rail is `128px` in the normal layout and `56px` in compact mode;
the compact brand keeps the Logo icon and hides the text label.

```text
[ Crew product mark ]
        Crew

  [icon]  对话
  [icon]  外援
  [icon]  技能
  [icon]  灵感
          ...
```

- The brand mark and `Crew` label use a vertical identity stack without an
  added tile, border, shadow, menu, or chevron.
- Navigation rows keep icon and label side by side. A fixed `22px` icon track,
  restrained gap, and bounded label track are centered as one unit so icons do
  not shift when labels have different lengths.
- Selected, hover, focus, disabled, account, update, and settings rows reuse the
  same centered geometry. Long account names truncate visibly instead of
  clipping raw characters.
- The rail and its brand span the title-bar and page rows. The title bar never
  paints a duplicate product mark.
- Switching does not convert the active record to the other mode.
- Restoring the assistant mode's active session must not navigate away from
  that mode's saved `lastPosition` when asynchronous session hydration finishes.
- At constrained width the rail keeps both icons and labels; the context track
  narrows or collapses first.
- `product-mode-store.ts` owns the `productMode` field and separate
  `lastPosition`, `navigationCollapsed`, and `historyFilter` snapshots for each
  product mode. It must not read or write Agent execution `mode`, session
  ownership, or Workspace identity.
- `desktop/src/ui/layouts/application-shell.ts` and
  `desktop/assets/styles/shell.css` own the shared title bar, product selector,
  primary navigation, context slot, page outlet, responsive shell geometry and
  command-entry semantics. Feature slices populate the slots; they do not add a
  second shell or override its columns.
- General-assistant history controls follow the verified `master` placement:
  the collapse/restore command sits beside the `对话 / 工作室` switch, and the
  compact `新建对话` command appears beside it only while history is collapsed.
  Collapsing removes the history track completely; it must not leave a `48px`
  full-height action rail. Both icon commands require tooltips and accessible
  names, and neither command belongs to primary navigation.

### 12.5 Office assistant shell

The office assistant uses three stable regions, following the product flow in
`Ace工作模式.pdf` and the visual composition in
`docs/frontend/work-mode-panel.html`:

```text
128px shared application rail | 260px context list | minmax(0, 1fr) work area
```

Work and General Assistant use the same application rail, row geometry, labels,
and footer commands. Below `1180px`, the rail becomes `56px` and switches to
icon-only rows while the context list narrows to `240px` and may be removed
entirely by its collapse command.

The Work navigation starts with three Work destinations: 工作, 计划, and 知识.
It then reuses the shared 专家, 技能, Wiki, 任务, 审计, and 系统 destinations
from the same navigation inventory and routes them to the same production
pages. 工作 owns the daily overview and ordinary Office conversations; 计划
owns dated Work items and item processing; 知识 owns personal knowledge.
Templates are quick-start actions on the workbench; Workspace scope and its
detailed configuration live in Settings, not in a duplicate workbench dialog.
Data sources, preferences, reminders, and permissions remain in Settings.

Switching to Office assistant always replaces the general-assistant page outlet
with the last valid Work location, even when the previous assistant location was
not Chat. The legacy assistant outlet must not occupy or cover the Work page
track. Every Work navigation command updates the visible Work page in place.

The Office context rail contains dated Work items followed by standalone Office
conversations, rendered with the same compact hierarchy as the 09 Office
Assistant example. A processing conversation is represented by a small linked
indicator on its item and never repeated as a top-level conversation. General
Agent sessions remain available through explicit cross-mode search/reference
flows; they are not ordinary Office conversations.

The context list changes with the destination. 工作 shows search, new-item,
collapsible Today/Week/Month/Earlier item groups, and standalone Office
conversations. 计划 owns the full work area and does not reserve a Work context
column. 知识 shows personal-knowledge search; general Work history is not
repeated there. The work area hosts the live brief, the shared
full conversation surface, item processing, or knowledge content. An optional
inspector is adjacent to the work area and never becomes a fourth permanently
visible column at the minimum window size.

The workbench follows the Living Design System 09 Office Assistant composition:
compact greeting and commands, one continuous daily brief, attention list,
template rail, and the shared Work Composer. The general-assistant top bar is
not rendered in Work mode. The greeting uses local time: `06:00–08:59` 早上好,
`09:00–11:59` 上午好, `12:00–17:59` 下午好, and `18:00–05:59` 晚上好; it has no
generic explanatory subtitle. Collapsing the Office context removes its
complete track and leaves one restore affordance centered on the application
navigation boundary, outside the page-title text flow. The command is not
inserted into primary navigation and never leaves a `48px` full-height column.

A Work item exists independently from chat. Opening it shows the item header,
description, metadata, related files, and activity first. If no processing
conversation exists, the only AI entry is the explicit `使用 AI 协助` command.
After linking, the item exposes `事项详情` and `AI 协作` views; the latter reuses
the existing production Conversation Surface and Composer. It does not
implement a second chat controller or a reduced textarea-only composer.

Inside an item processing conversation, title, business status, priority, and
due date appear in a compact context bar above the shared conversation. `事项详情`
opens the shared right-side Drawer over the conversation and reuses the
existing item editing, business actions, source/synchronization information,
activity history, and knowledge deposition owner. Closing the Drawer returns
focus to its trigger and must preserve the conversation, scroll position, and
Composer draft. At the 960 px compact viewport the Drawer overlays the work
area; it never compresses the conversation into another permanent column.

The office shell is functional only when each destination routes to a real
production owner and its commands reach the Work API/store. A visible rail,
fixture data, or an empty work area is not an implemented Work surface.

## 13. Page templates

New pages must choose the closest template before adding feature CSS.
Production slot structure is owned by
`desktop/src/ui/layouts/page-templates.ts`; column, fixed-row, and scroll-chain
behavior is owned by `desktop/assets/styles/layouts.css`. Feature CSS may lay
out content inside a slot, but must not redefine template columns, move a
toolbar or Composer into the scrolling row, or override compact Inspector
visibility.

### 13.1 List management

Use for skills, plugins, experts, agents, sessions, and similar collections.

```text
page header
tabs or scope switch
filter/search toolbar
list, table, or responsive item grid
pagination or continuation
```

Required states: loading, populated, empty, filtered-empty, error, unavailable.

### 13.2 Dashboard

Use for system, usage, audit summary, and operational health.

```text
page header
small metric strip
primary analysis region + secondary summary
detail list/table
```

Metrics support decisions; they do not become decorative equal-sized cards by
default.

Metric values use tabular figures and a stable alignment box. A count badge or
tab count is horizontally and vertically centered for one to four digits and
must not shift the surrounding label when its value changes.

Usage request tables use a fixed semantic column contract. Compact numeric and
status columns keep stable widths; model and session columns own the remaining
space, ellipsize inside their table cells, and never change a `td` away from
`display: table-cell`. Header and body centers remain aligned at every supported
desktop viewport.

### 13.3 Master-detail

Use for Wiki, audit event detail, settings navigation, and resource explorers.

```text
page header
master list | detail
```

- Master width is stable within a defined range.
- Detail owns its empty state.
- At narrow width, detail may replace master with a Back action.
- Both panes define independent overflow behavior.

### 13.4 Settings form

```text
settings navigation | grouped form
sticky or stable action footer when needed
```

- Group by user goal, not implementation module.
- Save behavior is explicit: immediate or submitted, never mixed silently.
- Advanced and dangerous settings are separated.
- A visible preference must have a verified runtime consumer. Persisting a
  value without applying it is not an implemented setting and must not appear.
- Compatibility mode is the current startup default while strict-security
  services are unavailable. Login does not expose a strict-security switch.
  Compatibility mode may relax legacy transport, supply-chain verification,
  and default approval behavior; it never authorizes a managed-to-host
  execution fallback.
- Gateway connection is an application invariant and remains automatic; General
  settings do not expose a start-connection switch.
- General typography exposes three user goals only: interface, reading/content,
  and code/terminal. Editable fields inherit the content scale instead of
  presenting a fourth editor-only slider.

### 13.5 Chat workspace

```text
navigation rail | history | conversation | optional inspector
```

The conversation remains dominant. Inspector and browser tools resize or
replace adjacent panes; they do not overlay essential content without an
explicit transient mode.

General-assistant history preserves the compact `master` hierarchy while using
the current semantic tokens:

- Workspace/project groups appear before the ordinary conversation group.
- A conversation row is one scanning line: identity icon, title, then
  time/runtime state at the trailing edge. Preview and repeated Workspace labels
  do not create permanent second and third lines.
- The title remains a neutral placeholder until the generated conversation
  summary arrives; the first user message is preview content, never the title.
  CSS may ellipsize the summary, but the title node and its hover tooltip retain
  the complete string.
- The history list owns its vertical scroll range. Wheel and trackpad scrolling
  work while the pointer is over any row; users never have to drag the scrollbar.
- Hover/focus uses the fast motion token for neutral row feedback and reveals
  the trailing action without shifting the title unpredictably. Group carets
  animate between collapsed and expanded states.
- `prefers-reduced-motion` disables these transitions without removing hover,
  focus, selected, busy, unread, or error states.

### 13.6 Board

Use for task state and workflow only.

- Columns share width constraints.
- Column and card controls are stable.
- Horizontal scrolling belongs to the board.
- Status color is not the only state indicator.

### 13.7 Office daily workbench

Use only for the office assistant default view.

```text
function rail | dated items and history | live daily brief
                                        | attention list
                                        | frequent templates
                                        | shared composer
```

- The default scope is `全部工作`.
- A Workspace selection filters the entire view, not one metric card.
- The brief displays a last-updated time and preserves existing content during
  background refresh.
- Source states returned with the brief are not discarded. `error`,
  `unavailable`, and active synchronization states appear in one compact status
  band with a source-specific refresh command; full configuration remains in
  office settings.
- Formal priority and AI attention are visually distinct.
- Metrics, attention list, and source counts resolve to the same fixture data.
- Office sources do not become four equal list columns. The source detail uses
  a two-by-two work layout: compact inbox and todo lists on the first row,
  schedule and meeting month calendars on the second row.
- Calendar cells show the first event label and an additional-event count.
  Selecting a date reveals that day's entries below the month grid; selecting
  an entry preserves the source's existing edit or open behavior.
- `查看全部` never expands a source card in place. It opens the shared dialog
  with domain-specific columns, instant search, result count, and pagination.
- The composer remains visible without turning the workbench into an empty
  chat landing page.
- The visual base is a pure white work surface in light mode, with neutral
  dividers and compact groups. It must not be reconstructed as a gray-blue card
  dashboard.
- The PDF's daily-flow intent survives: dated work/history provides context,
  the center presents the daily brief and actionable work, and source/Agent/MCP
  activity opens adjacent detail or Inspector without replacing the whole app.

### 13.8 Office item processing

```text
function rail | item and conversation list | item header
                                           | process + answer stream
                                           | composer
                                           | optional inspector
```

The stable item header contains title, formal priority, optional deadline,
source, `延期`, `标记完成`, and more actions. The inspector contains details,
steps, related files, outputs, source record, synchronization, and activity.

Business state, execution state, and synchronization state must not be combined
into a single badge. A completed item can still have a pending writeback; an
in-progress item can be waiting for user confirmation.

### 13.9 Office Workspace

The office view reuses the existing Workspace identity and root directory.

- One Workspace has one local root and many Work items.
- Show directory availability, lightweight index state, related items, recent
  files, and generated outputs.
- `重新关联` repairs a missing root without creating a duplicate Workspace.
- Directory states distinguish a confirmed missing/inaccessible directory from
  a failed availability probe. Probe failure is labelled `目录状态未知` and is
  accompanied by a short recovery hint that points to `重新关联`; it is never
  presented as proof that the directory does not exist.
- Full-content indexing is an explicit setting with scope, progress, stop, and
  delete actions.
- File generation defaults to a new version. Overwriting requires explicit
  user intent.

### 13.10 Office preferences and sources

Settings use the existing settings-form template.

Work preferences are grouped by documents, presentations, spreadsheets,
email, meetings, and general habits. A preference row shows its statement,
scope, origin, status, and last use. It supports edit, pause, delete, and
undo.

Data sources are organization-managed. A user may enable or disable each
available source. Turning a source off stops new synchronization but preserves
existing records; deleting synchronized local data is a separate confirmed
action.

The Office settings entry appears only when Settings is opened in Work mode.
Its page header aligns with every other settings pane, followed by one compact
segmented tab row: sources, preferences, automation, and reminders. Each tab
uses a bounded single-column content region no wider than `760px`, with a local
heading, short description, and consistent setting-row rhythm. Empty
source/record states collapse to one left-aligned message, binary controls keep
native switch geometry, time fields have visible start/end labels, and runtime
enums such as `granted`, `denied`, `default`, or `unavailable` are translated
before display.

Before a Work turn is sent, active global and matching Workspace preferences
appear as compact, removable-for-this-turn controls above the Composer. A
temporary cancellation is visually distinct, is frozen with a queued message,
and clears after that message is submitted. It never pauses or deletes the
stored preference. Automatically enabled preferences produce an in-app notice
and refresh the preference list. Account preferences may also guide the general
assistant, but Work product rules, source metadata, and Work history never do.

### 13.11 Agent Hub

Use the list-management family with two levels: browse/manage and create/edit.

```text
page header
my Agents / my Teams / create Agent / create Team
search or runtime filter
identity card grid or focused form
```

- The navigation entry may be hidden by a product feature flag; the disabled
  state is not shown as an empty broken page.
- Agent and Team cards use the shared identity system and visible availability
  labels.
- Runtime and model selection reuse the standard searchable option menu.
- `使用` creates or selects the destination conversation before assignment; it
  never silently replaces the active conversation.
- Smart formation progress, member conflicts, and policy blocks are explicit
  states inside the form region.

### 13.12 Wiki workspace

Wiki is a three-region master-detail-chat composition:

```text
source navigation | page or graph detail | Wiki Agent
```

- Source navigation supports timeline, file tree, type, and graph views without
  changing the detail contract.
- The file tree exposes the public `wiki/` hierarchy plus `Home.md` and
  `index.md`; runtime metadata and raw-source directories are not navigation
  content. Entity, topic, source, comparison, and synthesis labels keep their
  visual distinction through semantic `--mw-*` status tokens in list badges,
  graph nodes, and conversation result cards.
- Upload and compile progress stay near the affected source collection.
- The detail region owns Markdown reading, page metadata, and true empty state.
- The Wiki Agent is persistent for the selected knowledge base and opens cited
  pages in the detail region.
- At constrained widths, the Agent moves below the knowledge regions; it does
  not compress the document to an unreadable column.
- At desktop widths, navigation, sash, detail, and Agent own explicit grid
  tracks. Hiding the sash or entering graph mode must not shift detail into the
  sash track or remove the middle detail region.
- Graph zoom, pan, fit, `1:1`, source visibility, loading, empty, and layout
  failure states are visible and keyboard reachable.
- Knowledge-base selection, create/upload commands, refresh, and view switching
  form one stable toolbar. Selects use content-sized bounded tracks, segmented
  views have a visible container and selected state, and command labels never
  wrap at supported widths. Icon-only refresh is permitted only with a tooltip
  and accessible name.
- The embedded Wiki Composer reuses the shared model selector, attachment, and
  send controls; no browser-default button or oversized select may appear in
  this surface.

### 13.13 Team collaboration inspector

The collaboration board is an inspector view for Team sessions, not a global
project-management page.

- Show Team identity, members, Leader, concurrency, dependency groups, node
  status, current activity, outputs, and errors in that order.
- Polling or live refresh must not rebuild unchanged DOM or reset scroll.
- Artifacts open through shared file and image patterns.
- Cancel is available only for cancellable nodes and always names its target.
- The collaboration tab is absent for ordinary Crew and external single
  Agent sessions.
- Dense DAG content may scroll locally; the inspector must not cause page-level
  horizontal overflow.

### 13.14 Skills and plugins Hub

Skills, plugins, and experts share one Hub shell, not three unrelated card
systems.

- Primary tabs select the entity family; secondary controls select installed,
  market, category, or organization scope.
- Search updates results without rebuilding the focused search field.
- Install, uninstall, built-in, enabled, policy-disabled, pending, error, and
  unavailable states have stable action geometry.
- Account-level plugin switches use the shared Switch contract and show the
  reason when organization or role policy prevents changes.
- Skill and plugin symbols may carry restrained identity color; selected tabs
  and card actions continue to use semantic UI tokens.
- Search uses the shared field with an inline `16–20px` search icon. An icon
  cannot escape the input layout, scale independently, or overlap the results.
- Entity cards retain their data-provided glyph/monogram and restrained identity
  color. Missing data uses one documented fallback, not a blank or repeated gray
  placeholder.
- Hub cards use responsive tracks with a practical `300–320px` minimum and a
  compact horizontal proportion. Identity and name lead, real usage/ownership
  metadata follows the name, description occupies at most two lines, and tags
  form the final row. Do not invent usage data for entities that do not provide
  it.
- Expert, team, skill, and plugin category tags use the neutral Badge by
  default. Color is reserved for a real operational state such as warning,
  failure, enabled, or unavailable; decorative multi-color tag rows are
  prohibited. Recommendation and newness are ranking/catalog metadata, so
  `推荐` and `新` are not repeated as card badges.
- Expert and Skill descriptions use the caption scale and muted text so names
  remain the first reading level. Skill cards keep a comfortable compact
  minimum block size and token spacing instead of compressing title,
  description, and footer into one line cluster.
- Skill card copy uses a three-row name / description / metadata grid. The
  metadata row stays bottom-aligned across peers even when a description uses
  fewer lines.
- Primary buttons override monochrome symbol ink with the primary-button text
  token; a dark primary surface must never contain a dark leading icon.
- Assistant history uses one continuous Surface background. Workspace groups
  are visibly indented beneath the Project section while their hover/focus
  target still covers the complete available child row.
- Hub cards grow with their content and must not use a tall fixed height that
  creates an empty footer. A visually quiet action may become prominent on
  `hover` or `focus-within`, but while hidden it must not reserve a permanent
  content column; keyboard focus and an already-active state keep it visible.
- Hub details use the shared neutral modal overlay and lifecycle. Header,
  summary, metadata/tags, optional members or tools, and the action area form
  distinct regions; feature-local blue or identity-tinted page masks are not
  permitted.
- Expert category filters follow the category overflow rule in section 10.3;
  they wrap or collapse to a menu instead of creating a clipped horizontal rail.

### 13.15 Usage provenance

Usage views distinguish provider-reported values from estimates.

- A Settings pane owns its page title and concise description exactly once.
  Embedded Usage content begins with the dashboard itself and must not repeat
  the same title or expose implementation-oriented refresh/source prose.
- Successful initial refresh is silent. Security and System pages show a
  status region only while loading, partially available, offline, or failed;
  stable page content and its own timestamps carry the ready state.
- Use visible text such as `Provider 实际值` and `估算`, not color alone.
- Provenance stays adjacent to the affected number or row.
- Mixed-source totals disclose their composition.
- Numeric columns use tabular figures and one unit convention.

### 13.16 Feature-gated and unavailable surfaces

A capability can be hidden, unavailable, disabled by policy, or temporarily
failed. These are not interchangeable:

| State | Presentation |
| --- | --- |
| Product feature disabled | omit navigation and entry actions |
| User lacks permission | preserve context and explain the missing permission |
| Runtime unavailable | show the configured entity and a recovery action |
| Temporary load failure | preserve stale data where available and offer retry |
| Policy disables a control | keep the control visible, disabled, with reason |

### 13.17 Welcome and scenario entry

The welcome view is the general assistant's quiet start state, not a marketing
hero and not a Work dashboard.

- Brand identity, the daily Crew greeting, the shared Composer, and
  scenario commands form one centered vertical reading order. The Composer is
  the dominant action and scenario commands remain secondary.
- Welcome copy rotates through the six approved colloquial title/subtitle pairs
  by local calendar date. One day keeps one stable pair, including across
  rerenders; invalid dates fall back to the first pair. The greeting is the
  centered, large, bold primary line; its prompt is a smaller regular-weight
  secondary line. This hierarchy remains stable for all six copy pairs.
- The transparent Crew product mark is enlarged above the Composer without the
  old circular tile, border, or raised shadow. Two large rounded doodle paws
  use a bold `4px` rounded outline and three matching toe marks. They belong
  to the primary Composer project-context strip, straddle its upper edge, and
  sit wider than the mascot body. Mascot, paws, and Composer share one
  responsive center axis; paws are decorative and never receive pointer events.
- The mascot may breathe by at most `2px`. Reduced-motion disables this motion,
  and narrow windows scale the complete mascot-hand unit together. At viewport
  heights of `680px` or below, the mascot scales to `140px` and the Header keeps
  a `16px` bottom safety gap: the body may overlap only the upper half of the
  project-context strip while the paws remain anchored to its top edge.
- The welcome state reuses the production Composer in the centered composition;
  it does not create a second textarea or event path. Once conversation content
  exists, the same Composer returns to the chat workspace's stable bottom
  region.
- Completion canvases opened from the Composer are always above the Welcome
  mascot and paws. While a mention canvas is present, the Composer root and its
  shell do not create trapping stacking contexts; only the input panel is raised
  above the Welcome Header. The project-context strip remains below the mascot.
- Scenario categories use compact icon-and-label commands below the Composer
  instead of three descriptive cards. Expanding a category reveals its commands
  beneath that category row without moving the brand, greeting, or Composer.
  Refresh, selection, and clicking outside the scenario area collapse the
  expanded commands; loading reserves the same vertical track so the Composer
  does not jump.
- Scenario rows use standard entity/action icons with the optical sizing rules
  in section 9; they do not use miniature outlined placeholders.
- The Composer keeps all model, Agent, Skill, MCP status, approval, project,
  attachment, and send behavior in both positions.
- The project selector and input panel live in one shared Composer input shell.
  General Assistant, Work, and Studio reuse its overlap geometry; themes may
  change tokens but do not redefine the project-selector layout.
- The welcome state does not render an AI-generated-content disclaimer.
- Loading, no scenarios, unavailable scenarios, and long labels preserve the
  same skeleton and command position.

### 13.18 Audit dashboard

Audit uses the dashboard plus master-detail templates without nesting page
sections in decorative cards.

- Behavior/session/permission tabs keep labels and applicable count badges centered in stable tracks.
- Permission audit reuses the Security Center mode, capability, rule and audit
  surfaces inside Audit; it is not a separate application-rail destination.
- Summary metrics lead to the record list or current filter; they are not
  decorative numbers without commands.
- Filters, refresh, export, record detail, stale/partial data, and permission
  state keep their current behavior and use shared controls/overlays.
- Tables and tab strips own any necessary local overflow; the page body and
  Inspector tab row do not expose an accidental horizontal scrollbar.
- A wide trend range must not stretch a few bars across the full canvas. Use a
  bounded plot, stable bar width, or separate allowed/blocked summaries while
  keeping one data source and legend contract.
- Pagination is a stable command row: result range, page-size selector, and page
  controls may wrap as groups at constrained width, but no label, number, or
  control breaks into individual characters or lines.

### 13.19 Security dashboard

Security is a first-class operational surface, not an unmigrated legacy page.

```text
page header + refresh/status
summary and policy/runtime readiness
time-series or distribution analysis
filter toolbar
audit/security records + detail
```

- Charts use a shared chart palette (`--mw-chart-*`), typography, grid, tooltip,
  empty/error, and reduced-motion contract. Chart configuration contains no raw
  colors.
- Values, axes, labels, legends, and data labels remain inside their plot area
  at all supported widths; charts do not use gradients, glow, or oversized
  rounded bars.
- Summary values and record totals resolve to the same data. Loading does not
  fabricate zeros, and background refresh preserves labelled stale data.
- Policy, approval, diagnostics, permission, unavailable, partial, and
  fail-closed states remain distinct and do not weaken security behavior.
- The dashboard canvas consumes the available page width. Do not apply a page-
  level reading-width cap to operational cards, grids, charts, or records;
  constrain only prose paragraphs to `72ch`.

### 13.20 Existing surface migration inventory

This inventory prevents a visual rewrite from silently dropping existing
Desktop behavior. It describes the July 2026 baseline, not the target source
file structure. Before deleting a legacy surface, verify its current code,
tests, IPC/API contracts, security boundary, and every state listed here.

Primary product surfaces:

| Existing surface | Target pattern | Capability and state that must survive |
| --- | --- | --- |
| Application shell and title bar | shared shell | window controls, sidebar resizing, collapsed navigation, mode identity, update indicator, account state |
| Welcome and scenario entry | quiet start view plus command list | new-session entry, scenario selection, recent context, loading and unavailable scenarios |
| Conversation and history | chat workspace plus list management | create, search, group, pin, rename, delete, Workspace/channel identity, external Agent and Team identity, busy and unread state |
| Expert picker | entity selector plus Hub detail | discovery, availability, assignment, identity symbols, model/runtime metadata |
| Agent Hub | Hub | runtime discovery, external Agent and Team creation, assignment, model selection, unavailable and feature-gated state |
| Skills and plugins | shared Hub | installed/market tabs, search, categories, install/uninstall, account plugin toggle, policy disabled and install failure |
| Wiki | three-region master-detail workspace | source navigation, Markdown detail, upload/compile progress, graph controls, persistent Wiki Agent, empty/error/offline states |
| Scheduled tasks | list management plus detail/drawer | create, edit, enable/disable, run, delete, schedule validation, run history and failure recovery |
| Audit | dashboard plus master-detail | filters, records, detail, export, permission state, stale and partial data |
| Security center | dashboard plus settings form | current mode, runtime readiness, approvals, policy explanation, diagnostics and fail-closed errors |
| System | dashboard plus master-detail | runtime/services/filesystem status, logs and recovery actions without exposing privileged execution |
| Studio | scoped skin on chat workspace | Studio workflow and reduced chat chrome without global theme leakage |
| Feedback | dialog plus history | form validation, attachments, submission, retry, history, success and failure |
| Login and account | blocking dialog plus account settings | login, verification, agreement, cancellation/quit, expired identity and logout |
| Application update | progress surface plus confirmation | available, downloading, paused/failed, ready-to-install and restart states |

Inspector views:

| Existing view | Target owner | Capability and state that must survive |
| --- | --- | --- |
| Context | Inspector Context | session/model identity, provider-reported versus estimated usage, context composition, system prompt and raw messages |
| Files | Inspector Files | cumulative file list, file actions, unified Diff, unavailable/revoked/superseded files |
| Plan | Inspector Plan | preview/edit, Todo progress, feedback, approval, rejection, cancellation and approved read-only state |
| Kanban | Inspector Kanban | task nodes, dependencies, progress, blocked/failed/cancelled/completed states |
| Collaboration | Inspector Team | members, DAG, events, tools, artifacts, approval, cancellation and Team-only visibility |
| Browser | Inspector Browser | navigation, loading, offline/error, permission boundary, safe handoff and host-owned browser lifecycle |

Settings panes:

| Existing pane | Target pattern | Capability and state that must survive |
| --- | --- | --- |
| Account | settings form | identity, account state, login/logout and unavailable account service |
| General | settings form | theme, interaction preferences and persisted validation |
| Model | settings form plus list management | provider/model profiles, defaults, validation, connection failure and secret-safe display |
| Channel | list management plus detail | channel binding, availability, enable/disable, connection and recovery |
| MCP | list management plus detail | server configuration, capability state, validation, connection and policy restrictions |
| System logs | data table plus detail | filtering, local scrolling, copy/export and partial/error states |
| Usage | dashboard plus data table | provider-reported/estimated provenance, filtering, units, editing where supported and empty/partial states |
| Library | accordion/list management | Workspaces/projects and sessions, counts, loading, empty and unavailable records |
| Data | settings form | retention/export/clear controls, confirmation, progress and failure without weakening ownership boundaries |
| About | compact detail | version, update state, legal/support links and build provenance |

Settings list rules:

- Channel rows show channel identity, compact connection state, and the direct
  connect/disconnect action. Transport, SDK, authentication, upload, and policy
  implementation notes belong in the configuration detail, not the list row.
- MCP connection errors use a dedicated wrapping error line inside the row;
- interactive integration rows own hover and focus across the complete card, including
  the trailing action area; a nested select button must not paint a partial-card hover;
- host-mutating installation approval remains main-process-owned, but its visible modal
  uses the same neutral overlay, surface, typography, focus and keyboard contracts as
  product dialogs;
- model cards use a compact two-column responsive grid where space permits; short
  default/built-in states remain content-width metadata rather than full-width bands;

Work settings are visible only in Work mode and use four local tabs: `数据来源`,
`工作偏好`, `计划自动化`, and `办公提醒`. Users can manually add a preference
as well as edit, pause, resume and delete it. Workspace/file permission guidance
belongs with data sources, automatic item progression belongs with automation,
while desktop notification, DND and source-notification controls belong to
office reminders.

Legal documents have a bounded dialog shell and one scroll owner in the document body.
At short viewport heights the final section must remain reachable without scrolling the page.

The General Assistant welcome topology uses a centered daily greeting followed
by a `120px` Crew mascot unit whose two doodle hands overlap the shared Composer
top edge. Short windows reduce the unit to `88px`; narrow windows use `96px`.
A compact horizontal scenario-command row remains beneath the Composer. Current
theme tokens and responsive stacking remain authoritative.

Session selection updates active rows in place. Store refreshes must not detach and reinsert
unchanged rows in a way that replays hover/focus motion after a click.
- Backend text is never forced into a single-line status pill or allowed to
  collide with actions.
- Model rows use a bounded responsive grid. `Default model`, `Built in`, and
  other short states are content-sized badges, never full-width colored bars.
- These presentation rules do not change selection, edit, default, reconnect,
  delete, install, or channel connect/disconnect behavior.

The application title bar begins after the full-height Sidebar brand and
contains only the drag region and native window commands. Approval, sandbox,
and network state stay in the Composer and Security
surfaces; a permanent three-badge status strip is not shown in either mode.

Office assistant adds Workbench, Work items, Workspace, Knowledge, Templates,
Sources, Preferences, and Notifications. Its Knowledge and Templates views use
the same master-detail, list-management, file, permission, and state contracts;
they are not placeholder navigation entries.

When code added by a later merge introduces another visible surface, update
this inventory before that surface enters the rewrite plan.

## 14. Responsive and overflow rules

At or above `1180px`:

- full page actions remain in the header;
- multi-column grids may use three or four tracks;
- master-detail panes remain side by side.

Between `960px` and `1179px`:

- the `56px` navigation rail keeps centered icon rows, accessible names, and the
  compact Crew Logo;
- header actions may wrap;
- grids reduce columns;
- side summaries move below primary content;
- wide tables scroll locally.
- category and filter groups wrap or collapse to a menu/select; only true tab
  strips and intrinsically wide data views may own horizontal scrolling.

Office assistant behavior at the minimum window:

- the function rail remains labeled, with tooltips as supplemental help;
- the context list can collapse and is restored without losing selection;
- the collapsed context action rail keeps restore and new-conversation commands
  reachable without moving them into primary navigation;
- the item inspector opens as a drawer over the work area;
- the item header wraps metadata below the title, but keeps required commands
  reachable;
- the composer never falls below the visible work area or overlaps navigation.

Below `960px` is not a supported application window. Dialogs and detached
windows must still avoid clipped actions.

Text rules:

- Use `min-width: 0` for flexible grid and flex children.
- Use `overflow-wrap: anywhere` for untrusted identifiers and paths. The
  process timeline summary title is a component-level exception: it is a
  single-line scan label and may use `overflow: hidden`, `white-space: nowrap`,
  and `text-overflow: ellipsis`. Expanded Request/Response details remain the
  source of truth for full arguments, paths, and results.
- Do not hide overflow to conceal layout defects.
- A button label must not wrap at supported widths.

## 15. State patterns

Every data-bearing page or region defines:

1. initial loading;
2. background refresh;
3. populated;
4. true empty;
5. filtered empty;
6. partial data;
7. recoverable error;
8. unavailable/offline;
9. permission blocked;
10. destructive action pending;
11. success feedback.

Guidance:

- Use skeletons when the final geometry is known.
- Use a compact spinner for actions and indeterminate local work.
- Do not show fabricated zero metrics while the source is loading.
- Preserve stale data during background refresh and label it if necessary.
- Empty-state actions must be relevant to why the region is empty.

Office items additionally use independent state axes:

| Axis | Values |
| --- | --- |
| Business | 待确认, 待处理, 进行中, 已完成 |
| Execution | 未执行, 运行中, 等待确认, 执行失败, 执行完成 |
| Synchronization | 不适用, 已同步, 同步中, 待回写, 同步失败, 存在冲突, 来源不可用 |

`延期` is a date operation. `归档`, `取消`, and `停止跟踪` are record
dispositions. They do not become extra values in all three axes.

Status presentation:

- one leading label states business state;
- execution appears near the process region;
- synchronization appears next to source metadata;
- attention reason uses neutral explanatory text;
- animation is limited to actual running or synchronizing work.

### 15.1 Office planning, synchronization, and workload

The Office Plan has list, calendar, and board views over the same WorkItem
collection. View changes never create a second client-side task model.

Board behavior:

- columns represent the business axis only: 待确认, 待处理, 进行中, 已完成;
  已归档 is a read-only disposition lane;
- native drag and drop may move an active item only to a domain-valid target;
  the card remains in its source lane until the server accepts the optimistic
  version;
- the active drag card uses the existing pressed/selected surface tokens and
  the target lane uses the existing focus/selection border tokens;
- every draggable card also exposes a compact native status control for
  keyboard and assistive-technology users;
- an error restores the server snapshot and appears in the page-level live
  region. Color is never the only drop or error signal.

Category is optional user metadata. It appears as one neutral compact label,
uses the existing Badge typography and surface tokens, and never assigns a
rainbow palette per category. Category filtering uses a native select populated
from the current result set.

The system-update dialog is a shared overlay, not a page-sized blue panel. It
lists approved sources with name, enabled state, synchronization state, last
successful update, and the existing refresh/toggle commands. Credentials and
adapter implementation details never render. Source links continue to open
through the existing Browser Use / external navigation boundary.

Automatic business-state transition is an account setting and defaults off.
When enabled, the first successful Agent response in a linked item processing
Session may move only `待处理` to `进行中`. It never confirms an untrusted item,
marks work complete, archives a record, or hides synchronization failures.

Workload reports use a segmented day/week/month control and a date anchor. The
summary band shows created, completed, in-progress, overdue, and completion
rate. A compact status distribution follows; charts supplement exact values
instead of replacing them. The archive action freezes one owner/Workspace/
period snapshot. Archived reports display the frozen-at time and omit refresh
or overwrite actions.

Agent, Wiki, and Team surfaces additionally cover:

- feature disabled versus permission denied;
- runtime discovered, ready, busy, unavailable, and stale;
- Wiki upload queued, uploading, ingesting, complete, failed, and cancelled;
- graph loading, empty, filtered, layout failure, and ready;
- Team planning, dependency blocked, running, waiting for approval, failed,
  cancelled, and complete;
- artifact available, unavailable, revoked, and superseded.

## 16. Mock and preview contract

The preview mode uses the same HTML, TypeScript, and CSS as production. Only the
external adapter and data fixtures vary.

Preview files are not allowed to:

- patch production selectors;
- inject application UI through inline styles;
- hide authentication or loading classes on an interval;
- maintain a copied `renderer.js`;
- replace production components with demonstration-only markup.

Fixture scenarios:

- normal populated;
- long Chinese and English labels;
- loading;
- empty;
- filtered empty;
- partial data;
- error;
- offline;
- permission blocked;
- running/streaming;
- dense maximum-reasonable data.

Office assistant fixtures also include:

- all-work and single-Workspace scopes;
- structured source auto-created item and email-derived pending confirmation;
- Work and read-only Agent history in one dated list;
- Agent conversation `@` snapshot and stale-snapshot update;
- no deadline, overdue, completed with pending writeback, sync conflict, and
  unavailable source;
- missing local root with `重新关联`;
- learned preference auto-enabled and undoable;
- full-content indexing off, indexing, complete, and failed;
- item execution waiting for approval;
- live daily brief refresh and archived daily snapshot;
- multiple user categories, a board drag target, automatic-status setting off
  and on, source update states, and populated day/week/month workload reports;
- one immutable archived period report whose values do not follow later fixture
  mutations.

Merged Desktop surface fixtures also include:

- Agent Hub with no runtime, ready runtime, unavailable Agent, populated Team,
  smart-formation progress, member conflict, and feature-disabled navigation;
- Wiki with no knowledge base, populated timeline, upload and ingest progress,
  graph with and without source nodes, graph-layout failure, missing page, and
  persistent Agent response containing page citations;
- Team collaboration with dependency groups, parallel nodes, approval wait,
  node failure, cancellation, artifacts, and unchanged live refresh;
- ordinary follow-up, external-Agent permission request, Team permission
  request, submitting decision, and rejected decision;
- image available, missing local image, copy success, reveal unavailable, and
  long attachment path;
- skills/plugins with installed, market, search-empty, policy-disabled toggle,
  pending toggle, and install failure;
- usage rows containing provider-reported, estimated, and mixed-source values.

Fixture invariants:

- summary totals equal the underlying records;
- chart totals and list totals agree;
- timestamps use a fixed scenario clock;
- durations and next-run calculations are plausible;
- status labels use product language;
- related ids resolve;
- no impossible percentages, dates, counts, or units;
- mock actions update the fixture state or clearly report that they are
  read-only.

The preview scenario switcher lives outside the application canvas. At
`960 x 640` it must not cover navigation, dialogs, or the composer.

## 17. Accessibility

Target: WCAG 2.1 AA.

- Normal text contrast: at least `4.5:1`.
- Large text and UI boundaries: at least `3:1`.
- All commands use native interactive elements where possible.
- Keyboard order follows visual order.
- Focus is always visible.
- Icon-only buttons have accessible names.
- Dynamic status uses appropriate live regions without excessive
  announcements.
- Modals manage focus and restore it.
- Color is never the only state signal.
- Controls remain usable at `200%` text zoom where the Electron shell permits.
- Reduced motion and increased contrast preferences are respected.

## 18. Content rules

- Use concise Simplified Chinese for primary UI text.
- Use product terminology consistently across pages.
- Do not mix `success`, `done`, `已完成`, and `正常` for the same state.
- Button labels use a verb and object when the action is not obvious.
- Destructive labels name the action: `删除会话`, not `确定`.
- Empty states explain what is absent and what the user can do next.
- Avoid feature-tour text inside operational pages.
- Use realistic long labels in preview fixtures.
- Use `事项` for user work, `会话` for conversational history, and `执行` or
  `任务` for Agent runtime work.
- Use the full product labels `Crew 通用助手` and
  `Crew 办公助手` in the mode selector.
- `优先级` is user or source-owned. AI ordering is described as `当前关注` and
  includes a reason.
- Agent history in the office assistant always carries a visible `Agent` label
  and read-only explanation.
- General-assistant history does not show Work item state, source, snapshot,
  writeback, or synchronization metadata.
- The AI-generated-content disclaimer is supporting window-level copy and stays
  in the active content safe area, never underneath navigation or Composer.

## 19. CSS ownership and architecture

Target cascade:

```css
@layer reset, tokens, base, components, shell, features, skins, utilities;
```

`desktop/assets/styles/tokens.css` is the only Renderer token owner.
`variables.css` and all pre-reconstruction aliases have been removed; feature
CSS consumes only `--mw-*` semantic/component tokens and never recreates an
alias compatibility layer.

Production has one `#renderer-root`. Unmigrated static UI is contained by the
single `#renderer-legacy-outlet`; new shared overlays mount in
`#renderer-overlay-host`. A migration slice moves its surface out of the legacy
outlet and deletes the old owner—it must not create a second application root
or another catch-all compatibility outlet.

Target ownership:

```text
styles/
  index.css
  tokens.css
  foundations.css
  components/
    controls.css
    data-display.css
    overlays.css
    content.css
  shell/
    app-shell.css
    chat-workspace.css
    inspector.css
  features/
    experts-skills.css
    agents-teams.css
    team-collaboration.css
    wiki.css
    cron.css
    audit.css
    system.css
    settings.css
    browser.css
    work/
      shell.css
      workbench.css
      items.css
      workspace.css
      knowledge-templates.css
      preferences-sources.css
  skins/
    studio.css
```

This is an ownership model, not a requirement to create empty files. A file is
created only when it owns real rules.

The `work/` directory is created incrementally. Shared controls, composer,
history primitives, Workspace behavior, and security presentation remain with
their existing owners; Work files own only office-specific layout and business
presentation.

`agents-teams.css` owns Agent Hub and shared identity presentation.
`team-collaboration.css` owns only the inspector board. `wiki.css` owns the Wiki
shell and graph layout, while shared Markdown, attachments, image viewer,
dialogs, and composer rules remain in component or shell owners.

Rules:

1. A selector has one owning file.
2. File names describe stable responsibility, not chronology (`v2`, `premium`,
   `new`, and `override` are prohibited).
3. Feature CSS is scoped under one feature root.
4. Components do not know page ids.
5. Theme selectors only map tokens.
6. TypeScript does not inject arbitrary visual inline declarations. Measured
   runtime geometry and user-selected typography may cross the DOM boundary
   only through the shared allowlisted runtime-style bridge; it publishes
   `--mw-runtime-*` variables and `runtime.css` remains the CSS owner. Direct
   feature-level `.style.*` and literal `style="..."` remain forbidden.
7. `!important` requires a documented integration reason and an allowlist.
8. Raw colors outside `tokens.css` primitive declarations fail verification;
   semantic theme mappings, chart configuration, SVG consumers, and Studio skin
   all reference primitives.
9. Undefined token references fail verification.
10. Cross-file conflicting selectors fail verification.
11. Deleting legacy CSS is part of each migration slice; do not layer new rules
    above it.
12. The production import graph contains no `legacy`, `compat`, `premium`, `v2`,
    chronological override, or old theme owner when reconstruction completes.
13. Every referenced custom property resolves in each supported theme, and dead
    variables are removed rather than retained as aliases.
14. The raw-color, undefined-token, duplicate-selector, dead-variable,
    `!important`, and forbidden-owner audits are blocking gates with zero
    unapproved findings. They are not report-only checks.

The bundled pet page under `desktop/assets/pet-assets/` is an upstream,
self-contained pixel skin loaded in a separate sandboxed BrowserWindow. It is
not part of the Renderer CSS import graph and does not inherit `--mw-*` tokens.
Its source palette and required bridge overrides are therefore audited as an
isolated integration asset, not rewritten as Renderer feature CSS. Renderer
Studio styles remain inside the normal token and owner rules above.

## 20. New-page workflow

Before implementation:

1. State the user goal and primary repeated task.
2. Select the product mode and confirm whether the page is shared or
   mode-specific.
3. Select a page template from section 13.
4. List the data states from section 15.
5. Identify shared components already available.
6. Define only feature-specific layout or visualization needs.
7. Check behavior specifications and permissions.
8. For an existing general-assistant surface, compare the same route, state,
   viewport, and overlay with `master`; record region, scroll, and anchor
   differences before changing production layout.
9. When a shared visual contract changes, amend this document first and update
   the corresponding Living HTML before production code.

During implementation:

1. Use semantic and component tokens.
2. Keep feature selectors under one root.
3. Implement `1280 x 800` and `960 x 640` together.
4. Use real-length fixture content.
5. Preserve keyboard and focus behavior.

Before merge:

- typecheck, lint, and unit tests pass;
- style lint passes;
- no cross-file selector conflicts;
- no undefined tokens;
- no raw component colors;
- no unsupported `!important`;
- no embedded SVG color literals or token fallbacks;
- no legacy/compat/premium/v2/theme-override imports or unresolved/dead tokens;
- screenshots pass at all three target viewports;
- populated, loading, empty, and error scenarios are visible in preview;
- the Living Design System is updated if a shared contract changed;
- `docs/frontend/desktop-frontend.html` records functional changes.

## 21. Migration order

1. Establish tokens, base typography, focus, and theme application.
2. Establish shared controls and state patterns.
3. Stabilize app shell, navigation, history, composer, and inspector.
4. Add product-mode ownership without overloading the existing Agent execution
   mode.
5. Migrate chat content, process timeline, follow-up and permission decisions,
   attachments, image viewer, and artifact actions as one interaction family.
6. Migrate the Hub family: experts, skills, plugins, Agent Hub, runtime/model
   pickers, and shared identity presentation.
7. Migrate Wiki navigation, detail, graph, ingest progress, and embedded Wiki
   Agent as one responsive workspace.
8. Migrate the Team collaboration inspector after shared board, status, and
   artifact components are stable.
9. Migrate Cron, Audit, Usage provenance, and System data views.
10. Implement the office assistant shell using the stabilized shared
   foundations and existing Workspace/security behavior.
11. Migrate settings, integrations, browser, and remaining overlays.
12. Scope Studio skin and remove global theme override.
13. Remove legacy compatibility and obsolete audit scripts.

Each slice replaces old ownership. It does not add a new patch layer.

## 22. Acceptance criteria

The refactor is complete when:

- all supported pages use one header and spacing system;
- component appearance is consistent across page families;
- page-specific layouts remain appropriate to their workflows;
- light, dark, and high-contrast themes are token mappings;
- Studio is a local skin;
- `960 x 640` has no clipped required action or body-level horizontal overflow;
- preview data is internally consistent;
- visual regression covers all primary pages and states;
- the CSS cascade no longer depends on import order between competing selectors;
- a new page can be built by selecting a documented template and existing
  components without inventing a new visual vocabulary.
- both assistant modes use one visual system while preserving separate
  navigation and history semantics;
- the office assistant workbench, item processing, Workspace, knowledge,
  template, preferences, and source patterns are represented in the Living
  Design System;
- Work item business, execution, and synchronization states remain visually
  independent;
- Agent history is recognizably read-only in the office assistant and can be
  referenced through the shared `@` pattern.
- Agent Hub, Wiki workspace, Team collaboration, permission decisions, media
  actions, Skills/Plugins Hub, and Usage provenance retain their merged
  capabilities while using shared tokens and component contracts;
- Context, Files/Diff, Plan, Kanban, Team, and Browser are represented as six
  navigable peer Inspector views with one owner per capability;
- every primary surface, Inspector view, and settings pane in the migration
  inventory is mapped to a target pattern before its legacy implementation is
  removed;
- feature-disabled, permission-denied, runtime-unavailable, and load-failed
  states remain semantically distinct;
- the canonical Markdown specification and Living HTML contain matching page
  patterns and links.
- light app/canvas/surface resolves to pure white and dark app/canvas resolves
  to pure black; routine chrome has no gray-blue cast;
- all general-assistant page/popup region ownership and placement match the
  verified `master` baseline or carry an approved intentional-change record;
- route changes never leave a chat-history placeholder on non-chat pages, and
  context collapse is owned by the active chat/Inspector surface;
- Skills/Plugins and Expert retain entity identity, use contained search icons,
  and handle category overflow without a page-width horizontal scroller;
- Audit counts are centered, Security has a complete shared-token dashboard and
  chart implementation, and Welcome keeps the disclaimer in the content safe
  area;
- Inspector counts retain one fixed centered box; Hub cards preserve compact
  information order and keyboard-reachable actions; Wiki toolbars, Audit
  pagination, and the full-width Security canvas remain stable at every
  supported viewport;
- every Work navigation entry and applicable command is functional against the
  production Work API/store; fixture visibility is not completion evidence;
- Work's full specification has current Renderer, API, store, persistence, and
  interaction evidence, including unavailable external-source behavior.

## 23. Explicit non-goals

- Rewriting the desktop UI in React or another framework.
- Adding a component-library dependency.
- Making all pages share the same information architecture.
- Creating a second Workspace, security, file-access, or Agent runtime stack for
  the office assistant.
- Rebuilding human task assignment, project management, or enterprise approval
  workflows inside Crew.
- Reproducing the Soft Pixel concept image layout exactly.
- Preserving obsolete theme options or arbitrary accents.
- Keeping the current `gemini_v3` copied renderer and CSS as a second frontend.
- Migrating Hermes UI code.

## 24. Governance and synchronization contract

### 24.1 Artifact roles

The Desktop design system is maintained as a connected set of artifacts:

| Artifact | Role | May redefine shared visual rules? |
| --- | --- | --- |
| Approved feature specification | behavior, permissions, data and workflow contract | only when it explicitly changes product behavior |
| `docs/specs/desktop-ui-reconstruction.md` | Renderer replacement scope, boundaries and completion criteria | no |
| `design/desktop-design-system.md` | single normative UI standard | yes |
| `docs/frontend/desktop-ui-parity-acceptance-manual.md` | legacy capability, interaction, IPC and security parity baseline | no |
| `design/ace-ui-design-system.html` | executable visual reference and acceptance surface | no |
| `desktop` production source | implementation | no |
| preview fixtures and visual snapshots | state coverage and regression evidence | no |

The Markdown standard and Living HTML are complementary, not two independent
design drafts. This document defines the rule; the HTML shows what the rule
looks and behaves like. A screenshot, current production page, mock, or copied
prototype never becomes a standard by itself.

### 24.2 Change classification

Classify every Desktop UI change before implementation:

| Change type | Required synchronization in the same change |
| --- | --- |
| Reuses existing tokens, components, templates, and states without changing a shared contract | production code plus relevant fixture and test |
| Fixes behavior without changing a visual contract | production code plus regression test; no design-document churn |
| Changes a shared token, component, shell, layout, interaction, state, theme, responsive, accessibility, or content contract | amend this standard first, then update the Living HTML, production code, fixtures, visual snapshots, and affected tests |
| Adds a page, page template, entity family, or state family | approved feature specification, this standard, Living HTML example, production implementation, fixtures, and visual tests |
| Migrates legacy CSS | establish the documented owner, move or rewrite the rules, and delete superseded/conflicting selectors in the same slice |

A change must not be split so that production temporarily invents an
undocumented shared rule or the Living HTML advertises a rule that production
cannot implement.

### 24.3 Review rules

Every page change must answer:

1. Which documented page template and component contracts does it use?
2. Which file owns each new selector?
3. Does it introduce a reusable rule? If yes, where is that rule documented and
   demonstrated?
4. Which populated, loading, empty, partial, error, offline, permission, and
   long-content states apply?
5. What happens at `1280 x 800` and `960 x 640`?
6. Which keyboard, focus, contrast, and reduced-motion behavior applies?
7. Which legacy selector or local visual invention becomes unnecessary?

Reviewers must reject changes that:

- copy an existing control, icon, shell, or state pattern instead of reusing its
  owner;
- introduce raw component colors, global feature selectors, visual inline
  styles, undocumented `!important`, or chronology-based patch files;
- rely on import order between competing selectors;
- update only the happy path or only the current viewport;
- change a shared contract without updating this document and its Living HTML
  example;
- migrate a page by layering new CSS while leaving replaced rules active.

### 24.4 Verification gates

From `Ace/desktop`, all UI changes run:

```bash
npm run check
npm run refactor:audit-leaks -- --strict
npm run refactor:audit-font-sizes -- --strict
npm run refactor:audit-dead-vars -- --strict
npm run refactor:audit-design-system -- --strict
```

During migration, an approved baseline may identify untouched legacy findings,
but Phase G completion requires the full production import graph to reach zero
unapproved raw colors, undefined/dead tokens, duplicate owners, forbidden
legacy files, and undocumented `!important`. Changes to a shared visual
contract, page template, theme, responsive layout, or documented state also run:

```bash
npm run test:visual
```

Visual snapshots are evidence, not authority. Update them only after inspecting
the rendered result against this standard and the Living HTML. The current
Stylelint and audit rules should be strengthened incrementally as the legacy
baseline is removed; weakening a gate to admit a patch is prohibited.

Repository-wide enforcement for this workflow lives in `Ace/开发约束.md`.
Human review and CI remain necessary because written instructions alone cannot
enforce repository policy.

## 25. Decision log

| Date | Decision |
| --- | --- |
| 2026-07-26 | Light white/minimal is the brand baseline. |
| 2026-07-26 | Supported themes are system, light, dark, and high-contrast. |
| 2026-07-26 | Sepia, sidebar-gray, and accent selection are removed. |
| 2026-07-26 | Studio dark styling is a scoped skin, not a global theme change. |
| 2026-07-26 | Preview mode must use the same production UI source with a mock adapter. |
| 2026-07-26 | The Living Design System includes reusable page templates and state scenarios. |
| 2026-07-26 | Product modes are `Crew 通用助手` and `Crew 办公助手`; the selector is global and top-left. |
| 2026-07-26 | Both modes share Workspace and security foundations but retain separate navigation and conversation ownership. |
| 2026-07-26 | The office assistant uses a function rail, context list, and dominant work area. |
| 2026-07-26 | Agent conversations are read-only in the office assistant and enter Work context as explicit snapshots. |
| 2026-07-26 | Work item business, execution, and synchronization states are independent. |
| 2026-07-26 | `design/desktop-design-system.md` is the single normative Desktop UI standard; `design/ace-ui-design-system.html` is its executable visual reference. |
| 2026-07-26 | The master-merge baseline adds Agent Hub, Wiki, Team collaboration, chat approval/media patterns, revised Skills/Plugins, history identity, and Usage provenance to the migration inventory. |
| 2026-07-26 | Desktop UI governance uses the normative standard, Living HTML, repository-level `开发约束.md`, automated checks, and human review as complementary enforcement layers. |
| 2026-07-26 | Process timelines preserve semantic icons for thinking, tool families, delegation, status, and errors; generic dots are not an acceptable replacement. |
| 2026-07-26 | Turn output summaries stay lightweight; Context, Files/Diff, Plan, Kanban, Team, and Browser use peer Inspector views as their complete interaction surfaces. |
| 2026-07-26 | The Desktop migration inventory includes every current primary surface, all six Inspector views, and every settings pane before legacy deletion is allowed. |
| 2026-07-26 | Muted light-theme text uses `#6A7276` so 10–12px explanatory text meets WCAG AA on white and warm-white surfaces. |
| 2026-07-26 | A full Renderer replacement must also satisfy `desktop-ui-parity-acceptance-manual.md`; the design system does not by itself prove behavior, IPC, or security parity. |
| 2026-07-26 | Renderer foundations own reset, typography, focus, scrolling and reduced-motion; focus rings use solid semantic colors instead of low-opacity accents. |
| 2026-07-26 | The canonical product sprite is injected at build time because Electron `file://` does not render external SVG `<use>` references; consumers still use semantic ids and never own path data. |
| 2026-07-26 | Shared control DOM and state behavior are centralized in `controls.ts`; `components.css` is their sole visual owner and feature CSS may only position their roots. |
| 2026-07-26 | Menu, popover, dialog, drawer, confirmation and toast focus/close/dispose behavior is centralized in `overlays.ts`; feature code may not attach a parallel global overlay lifecycle. |
| 2026-07-26 | Shell, master-detail, settings, board, chat and Inspector slot/scroll behavior is centralized in `page-templates.ts` and `layouts.css`; feature CSS owns only slot content. |
| 2026-07-26 | Production mounts one `#renderer-root`, one restricted legacy outlet and one shared overlay host; remounting reuses that structure and each migrated slice leaves the legacy outlet permanently. |
| 2026-07-30 | Strict security is one host-owned global preference exposed consistently in Login, Security Center, and General Settings; disabling it requires main-process confirmation and never disables managed isolation. |
| 2026-07-30 | Security Center moved into Audit as the `权限审计` tab; the application rail no longer exposes a separate Security destination, while the existing security controller and IPC paths remain the sole behavior owner. Successful Audit/System refreshes stay quiet; partial, offline and error states remain explicit. |
| 2026-07-30 | The compact and expanded application rail use 22px icons with scoped 2px/1.75px strokes; navigation outlines follow row state color while the shared sprite keeps its default geometry elsewhere. |
| 2026-07-30 | Office settings appear only in Work mode and use the standard settings-pane inset, an aligned page header, four segmented tabs, and a bounded single-column content rhythm. |
| 2026-07-30 | Work and General Assistant now share the same 188px desktop application navigation, typography, row states and footer commands; both collapse to the same 56px responsive rail below 1180px. |
| 2026-07-31 | The shared expanded application navigation narrows from 188px to 172px; Work and General Assistant retain identical geometry and the compact rail remains 56px. |
| 2026-07-31 | Work greetings use four explicit local-time ranges and no generic subtitle; the collapsed-context restore button is centered on the navigation boundary so it cannot cover page titles. |
| 2026-07-31 | Login removes the strict-security switch and Desktop defaults unset or invalid preferences to compatibility mode until strict-security services are available; managed isolation remains enabled. Hub page titles now use the shared page-title typography. Live chat turn patches preserve the spinner node, while the fixed-geometry ring animates its conic stroke instead of transforming the whole element, preventing chunk restarts and small-size lateral jitter. |
| 2026-07-31 | The workbench removes the duplicate “全部工作空间” settings entry and its “系统更新与办公设置” dialog; Office configuration has one owner in the Settings page. |
| 2026-07-31 | Work follows the Living Design System 09 three-region composition directly: a permanent 56px function rail, 260px collapsible work context, and content-first work area. Work items stay on the Work destination, Plan owns the full content width, context items use Today/Week/Month/Earlier disclosure groups, and standalone conversations remain below them. |
| 2026-07-31 | Work and General Assistant again share the 172px desktop application rail and 56px responsive collapse. Work prepends 工作 / 计划 / 知识 and reuses the shared 专家 / 技能 / Wiki / 任务 / 审计 / 系统 inventory and production routes. General Assistant, Work, and Studio also share one Composer input shell for the project selector overlap. |
| 2026-07-31 | Office links open in the session-scoped built-in Browser. Mail, schedule, meeting, and todo use domain-specific rows and editors; archived items expose summary metrics, archive date, outcome, and activity entry. Desktop knowledge presents personal knowledge only. |
| 2026-07-31 | Plan uses a vertical agenda, a metadata-dense task list, and four operational board columns; archive is a separate record view. Item editing replaces the detail body while active and uses visible labels. Mail and schedule dialogs use a bounded 720px office-document surface with one scroll owner at compact heights. |
| 2026-07-31 | Security audit and feedback history use the shared server pagination with page-size selection, first/last controls, numbered pages, and numeric jump. Feedback details use the shared modal hierarchy and standard buttons; successful Audit refreshes stay quiet. |
| 2026-07-31 | System Logs uses shared field, switch, and icon-button controls. Normal update confirmation is dismissible; only a server-issued forced update is non-dismissible, and its blocking dialog waits until all running sessions finish. |
| 2026-07-30 | The product-mode trigger uses the compact application icon; its compact menu uses Agent/Task symbols and a standard selected-row check instead of an unstyled text glyph. |
| 2026-07-30 | Returning from a Work session restores the General Assistant's saved page; asynchronous session hydration cannot force the shell back to Chat. |
| 2026-07-30 | Conversation Request/Response payloads and security approval summaries explicitly pair semantic foreground and background tokens; browser regression checks enforce at least 4.5:1 contrast in every supported theme. |
| 2026-07-30 | Conversation history rows keep a neutral placeholder until the generated summary arrives; the first user message never becomes the title, visual ellipsis never truncates the DOM value or hover tooltip, and the list itself supports wheel/trackpad scrolling. |
| 2026-07-26 | Product mode persistence uses the dedicated `productModeStore`; assistant/work view snapshots are independent from Agent execution mode and session ownership. |
| 2026-07-26 | The shared title bar, product selector, primary navigation, context slot, page outlet and shell commands are owned by `application-shell.ts` and `shell.css`; feature slices only populate its outlets. |
| 2026-07-28 | Light app/canvas/surface uses a pure-white base; dark app/canvas uses a pure-black base; routine product chrome uses neutral grays and black/white actions without a gray-blue cast. |
| 2026-07-28 | Raw colors exist only in primitive token declarations; semantic themes, components, feature CSS, charts, SVG consumers, and skins reference tokens, and Phase G CSS audits are blocking. |
| 2026-07-28 | G-R15–G-R19 add main-owned styled host approval, whole-card integration hover, manual Work preferences and office-reminder IA, legal scrolling, compact model cards, master welcome topology and non-replaying session selection motion. |
| 2026-07-28 | G-R20–G-R22 define coherent Markdown code contrast, product-styled full-access confirmation, semantic history commands with whole-row project hover, and calmer Expert/Skill typography without decorative recommendation/newness badges. |
| 2026-07-28 | G-R23–G-R25 remove duplicated Usage copy and redundant ready notices, align variable-length Skill metadata, restore primary-icon contrast and clear history nesting, and keep the Welcome composition stationary while scenario commands expand below it. |
| 2026-07-28 | `master` is the layout-parity baseline for existing general-assistant pages and overlays, while this specification remains the visual authority. |
| 2026-07-28 | Chat history and context-collapse controls are route-owned; non-chat pages do not reserve history, and Work-only metadata never enters general-assistant history. |
| 2026-07-28 | Navigation and feature icons use normalized optical sizing and consistent strokes; entity identity colors remain local, while amber is reserved for warning or documented illustration semantics. |
| 2026-07-28 | Welcome, Audit, and Security have explicit page contracts; Skills/Plugins and Expert preserve entity identity and use wrap/menu category overflow. |
| 2026-07-28 | A Work surface is complete only when its real Renderer commands and production API/store path work end to end; fixtures and prior checkpoint records are insufficient evidence. |
| 2026-07-28 | General-assistant history collapse removes the history track and keeps restore/new-conversation commands beside the chat sub-navigation, matching `master`; compact history rows use current semantic tokens and fast/reduced-motion-aware hover behavior. Inspector counts, Hub cards, Wiki toolbars, Audit pagination, and full-width Security geometry use stable shared-component tracks. |
| 2026-07-28 | Expert, team, skill, and plugin cards use content-sized compact geometry and neutral category tags; only real semantic states retain color. Their details use the shared neutral modal mask, lifecycle, information sections, and action region. |
| 2026-07-28 | General settings expose only applied preferences, keep Gateway connection automatic, and use three typography scales. Usage columns retain table semantics; Workspace probe errors are distinct from missing directories; Office settings translate runtime enums; process Request/Response code keeps at least 4.5:1 text contrast. |
| 2026-07-28 | Office switching hides the legacy assistant outlet and restores a visible Work location from every assistant page. Office history contains compact Work conversations/items and opens Work sessions without changing mode. The permanent title-bar security badge strip is removed. |
| 2026-07-28 | Work v2 reduces the Office rail to 工作 and 知识. Items are structured conversations, templates are workbench quick starts, Workspace is a scope, and both ordinary Office work and item processing reuse the full production conversation surface. |
| 2026-07-28 | Item processing keeps the shared conversation visible while existing item editing, actions, source/sync, activity, and knowledge controls open in the shared right-side Drawer; compact viewports use an overlay rather than a permanent column. |
| 2026-07-28 | Work dashboard content owns vertical scrolling without moving the shared Composer. Archived briefs expose archive state, not an invalid refresh action. Item sessions suppress Welcome and stay in the full Conversation Surface. “Manage items” is a dashboard action with active/completed/archived scopes. Organization knowledge distinguishes provider-unavailable from an empty connected library. |
| 2026-07-28 | A Work route owns exactly one main surface: Workbench shows Dashboard, a Work session shows Conversation, and neither may coexist with Welcome. Knowledge uses a bounded master list plus flexible detail; create/edit forms are progressive states triggered by explicit actions, never permanent empty-page furniture. |
| 2026-07-28 | G-W19 Work planning uses one WorkItem source for the dashboard, date-grouped item history, office conversations, calendar/list/board views, and lifecycle actions. The create dialog uses visible labels and native date controls. New Work CSS may use only semantic tokens; preview data stays fixture-only. |
| 2026-07-30 | Workbench presentation now follows the representative daily-work layout: a compact greeting header, one continuous four-metric brief, a primary attention list with a secondary template rail, and office-source detail below the first-viewport work summary. Item conversations expose an explicit keyboard-accessible return to Workbench in the stable context header. Existing Work API, template, item, archive, and shared Composer ownership remain unchanged. |
| 2026-08-09 | macOS menu-bar default/rest use transparent black-line Template Images; working, notification, and done remain supplied color states. All five macOS states use a 44px @2x representation for a crisp 22pt status item. |
| 2026-08-10 | New Crew drafts reset external runtime models to the configured Crew default. Running-intro and Agent avatars share one responsive content axis without fixed offsets; the approved yellow assistant keeps its small white-face artwork. Menu-bar color states use restrained optical normalization, with `done` enlarged 1.16x before the final 44px crop. |
| 2026-08-10 | The static Crew brand moves from the title bar into the responsive full-height Sidebar: 128px in regular windows and a 56px icon-only rail below 1180px. Its expanded-state top spacing matches the 40px title-bar track so the brand aligns optically with the adjacent context heading. Welcome restores one centered reading order: a large bold greeting, smaller regular prompt, and enlarged transparent Crew mark. Two larger three-toe doodle paws are owned by the primary Composer and straddle the project-context strip's upper edge at a wider stance. Reduced-motion support and six approved daily greetings remain intact. |
| 2026-08-11 | The shared application rail adopts `--mw-app-rail-width` (`128px`) and `--mw-app-rail-width-compact` (`56px`); below 1180px the rail, title-bar offset, no-context pages, restore control, navigation labels, account details, and Crew brand label switch together to compact icon-only presentation. |
| 2026-07-31 | Workbench office detail uses compact inbox/todo lists above paired schedule/meeting month calendars. Calendar dates expose event labels and selected-day detail; every source keeps a non-expanding `查看全部` search and pagination dialog. |
| 2026-07-30 | WorkItem and chat are separate: item creation never creates a conversation, item routes open details first, and one processing conversation can be created explicitly. Linked conversations are item-owned navigation, receive trusted owner-scoped item context, and do not duplicate in top-level history. The Work shell now follows example 09 directly and removes both the legacy top row and collapsed context placeholder track. |
| 2026-07-28 | G-W19 completion keeps date items and linked processing conversations as separate projections of the same WorkItem. Plan position restores across product-mode switches; item-aware sessions retain their context bar. Work text sizes use typography tokens, and both approved viewports pass the production interaction and overflow matrix. |
| 2026-07-29 | Production CSS audits include named colors; renderer text sizes use the current `--mw-type-*` scale or exact `--mw-font-ui-size` calculations, while fixed-geometry glyphs retain explicit sizes. The system font keeps the token default and optional font choices override `--mw-font-sans`. |
| 2026-07-29 | G-W19 full-target extension defines neutral item categories, dependency-free board drag/drop with a keyboard status control, opt-in automatic pending-to-in-progress transition, a shared source-update dialog, and immutable day/week/month workload reports. |
| 2026-07-29 | The General Assistant welcome state centers the shared Composer below the existing `Hi, 我是 Crew` / `今天想从哪里开始？` greeting; project context uses a compact chip on a subtle top backing strip, Agent/Skill/MCP entry points share one capability menu, security remains a three-mode Crew selector, and model plus context occupancy plus Send/Stop own the right side. |
| 2026-07-29 | Composer popovers now open below their triggers; capability details open beside the persistent parent on hover/focus, the model list is flat, scenario expansion is stationary and dismissible, the welcome disclaimer is removed, and draft-only project context disappears after conversation content exists. |
| 2026-07-30 | Login and Security Center expose one accessible strict-security switch, enabled by default; disabling requires confirmation and is persisted without overwriting other desktop preferences. Legacy settings colors now use `--mw-*` semantic tokens, including inverse text and shadow-none mappings. Audit rows expose tool/request provenance and use incremental pagination. |
| 2026-07-30 | Merge follow-up keeps the current fixed Inspector tabs and shared file “open with” menu, updates live tool durations in place, and loads help Markdown consistently in production and tests. File/application icons and help/PPT content now use native DOM construction after sanitization; visual builds share the production Markdown/WASM loaders. |
