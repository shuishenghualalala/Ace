# 9 Moon Note Fullstack Template

> **⚠ FULLSTACK — READ FIRST:** You MUST use the `backend-building` skill to make this website fullstack. You need to consider the user's query's intent and change the data accordingly.

A fullstack personal notes / knowledge-base app with wiki-style `[[links]]`, Markdown rendering, a D3-powered knowledge graph view, a real-time moon-phase widget, and four cinematic backgrounds (moonlit ripple, silk flow field, rain on glass, solid color). Per-user note persistence with automatic starter-notes seeding on first sign-in.

This template is well suited for:

- Personal knowledge bases / second-brains / Zettelkasten
- Subject-study / reading-notes apps (e.g. a language, a book, a research topic)
- Design / product thinking journals
- Wiki-style documentation sites for a small team or single author
- "Reflect-style" private notebooks

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## How To Fill This Template

All editable content lives in **two** places:

1. **`src/config.ts`** — every UI string (site title, header labels, sidebar copy, editor placeholders, graph labels, moon phase labels, background option labels + solid-color swatches), and the `starterNotes` array (shown to unauthenticated visitors and used as the localStorage fallback).
2. **`api/notes-router.ts`** — the **server-side** `STARTER_NOTES` array at the top of the file, auto-seeded into MySQL for every new user on their first sign-in.

You MUST keep the two starter-note arrays in sync. If the user is signed-in, they only ever see the MySQL-seeded `STARTER_NOTES` from `api/notes-router.ts`; unauthenticated visitors see `starterNotes` from `src/config.ts`. If the two drift, the template will feel inconsistent.

Do NOT modify component logic, Tailwind classes, or the shader / canvas backgrounds unless fixing a real bug. The liquid-glass UI, the four procedural backgrounds, the moon widget, and the D3 graph are all load-bearing visuals.

## Config Objects

### `siteConfig`

```ts
export const siteConfig = {
  title: "",        // Browser tab title and OG title
  description: "",  // Meta description
  language: "",     // BCP-47 tag, e.g. "en", "zh-CN", "ja"
}
```

Constraints:
- `title`: keep under ~40 characters
- `description`: keep under ~160 characters
- `language`: leave empty unless the user explicitly requests a fixed language

### `headerConfig`

```ts
export const headerConfig = {
  brandMark: "",             // Short brand / wordmark shown top-left
  noteCountSuffix: "",       // Suffix after the note count, e.g. " notes" / "条笔记"
  editorViewLabel: "",       // Tab label for the editor view
  graphViewLabel: "",        // Tab label for the knowledge graph view
  backgroundButtonTitle: "", // Tooltip on the background toggle button
  importButtonLabel: "",     // Label for the import button (leave "" to hide)
}
```

Constraints:
- `brandMark`: keep under ~10 characters. CJK or short English works equally well.
- `noteCountSuffix`: keep under ~10 characters. Include a leading space in English (e.g. `" notes"`).
- View labels: keep under ~6 characters each.

### `backgroundConfig`

```ts
export const backgroundConfig = {
  defaultMode: 'moonlit',    // one of: 'moonlit' | 'silk' | 'rain' | 'solid'
  defaultSolidColor: '',     // Hex color, used when mode is 'solid'
  options: [
    // { id: 'moonlit', label: "" },
    // { id: 'silk',    label: "" },
    // { id: 'rain',    label: "" },
    // { id: 'solid',   label: "" },
  ],
  solidColors: [
    // { color: '#000000', label: "" },
    // { color: '#1a1a2e', label: "" },
  ],
}
```

Constraints:
- Keep all 4 `options` and give each a short localised label (≤ 6 chars ideal). Removing an option changes the UI layout.
- `solidColors`: 3–4 items is the sweet spot. Each swatch label ≤ 4 chars.
- Background ids (`moonlit`, `silk`, `rain`, `solid`) MUST NOT be renamed — they map to actual WebGL / Canvas components.

### `sidebarConfig` / `editorConfig` / `graphConfig` / `moonConfig` / `appConfig`

Collectively these hold every other UI string. Each field is self-describing. Constraints:

- Every button / badge label: keep under ~8 characters (or ~4 Chinese characters). The sidebar column is ~260px wide; long labels wrap.
- `editorConfig.titlePlaceholder` / `contentPlaceholder`: can be one full sentence each.
- `editorConfig.contentPlaceholder` SHOULD explain the `[[wiki-link]]` syntax (e.g. `"Use [[Title]] to create a wiki link. Markdown is supported."`). If you omit this hint users won't discover the feature.
- `moonConfig.phaseLabels`: EXACTLY 8 strings, ordered `[New, Waxing Crescent, First Quarter, Waxing Gibbous, Full, Waning Gibbous, Last Quarter, Waning Crescent]`. Keep each under ~5 characters.
- `appConfig.emptyStateLabel`: one short sentence.

### `storageConfig`

```ts
export const storageConfig = {
  notesKey: "",  // localStorage key prefix for the fallback store
}
```

- `notesKey`: a unique slug per deployment, e.g. `"llm-notes-v1"`, `"cooking-journal-v1"`. Keep under ~30 chars.

### `starterNotes` (client) + `STARTER_NOTES` (server)

The two arrays MUST contain the same notes in the same order, with the same fields.

`src/config.ts`:

```ts
export const starterNotes: StarterNote[] = [
  {
    title: "",         // Short note title — used as the display heading AND as the wiki-link target
    content: "",       // Full Markdown body (GFM supported: tables, code blocks, task lists, headings)
    tags: [],          // Array of short tag strings
    source: "",        // Optional citation line shown below the note (leave "" if none)
  },
  // ... more notes
]
```

`api/notes-router.ts`:

```ts
const STARTER_NOTES = [
  { title: "", content: "", tags: [] /*, source omitted on server */ },
  // SAME entries as above, minus the `source` field (server schema stores it separately if needed)
]
```

Constraints:
- **6–12 notes** is the sweet spot for the graph view to look meaningful. Fewer → graph looks empty. More → MySQL seeding is slow on first login.
- `title`: keep under ~25 characters. Wiki-link target matching is case-insensitive but whitespace-sensitive, so match titles exactly when you reference them in `content`.
- `content`: every note SHOULD reference at least one other note via `[[Other Title]]` syntax (this is the whole point of the wiki graph). Aim for ~3–6 outgoing links per note.
- `tags`: 2–5 tags per note. Keep each tag short (≤ 8 chars).
- Content Markdown supports: headings, bold/italic, tables, code fences, task lists, blockquotes, links, inline code. Don't use images (no asset pipeline for seeded notes).

## Database Schema

Two tables defined in `db/schema.ts`:

- **`users`** — Kimi OAuth managed (`id`, `unionId`, `name`, `email`, `avatar`, `role`, `createdAt`, `updatedAt`, `lastSignInAt`)
- **`notes`** — per-user notes (`id`, `userId`, `title`, `content`, `tags` json, `source` text, `createdAt`, `updatedAt`)

Starter notes auto-seed on first `notesRouter.list()` call for a user whose note count is 0. Manual seed for a specific user: `npx tsx db/seed.ts <userId>`.

## Required Assets

**None.** Every background is procedural (WebGL shaders + Canvas 2D). The moon widget is canvas-rendered. Notes are plain Markdown — no image pipeline needed. If a user explicitly asks for photos inside notes, use `generate_image` and drop the file into `public/images/` — but by default this template ships without any binary assets.

## Auth

- **Kimi OAuth** is the only auth flow. The login button is in the header; after Kimi redirects back, `api/auth-router.ts` upserts the user row keyed by `unionId` and returns a session.
- Unauthenticated visitors can still use the app — notes are stored in `localStorage` (using the key from `storageConfig.notesKey`) and show the `starterNotes` sample set.
- On successful sign-in the frontend calls `notesRouter.list`, which triggers starter-note seeding if the user's server-side note count is 0.

Do not remove `api/kimi/`, `api/auth-router.ts`, or `api/middleware.ts` — the whole auth chain depends on them.

## Design Reference

**Colors:**
- Base foreground: `#e0e0e0` on black
- Accent / button: `#c8956c` (warm amber)
- Wiki-link color: `#d4a574`
- Background: user-selectable between 4 procedural modes + a solid-color picker

**Fonts:** (loaded in `index.html`)
- System stack for body; small-caps mono for labels in the moon widget

**Animations:**
- 4 full-viewport backgrounds (all live, all GPU/canvas):
  - `moonlit` — rippling pool with a single moon reflection
  - `silk` — silk flow-field (noise-driven vector field)
  - `rain` — rain on glass with droplet trails
  - `solid` — static color chosen from `solidColors`
- Moon widget is a real-time canvas rendering of the current moon phase
- D3 force-directed knowledge graph with drag + zoom

**Liquid-glass UI:** all floating panels use `backdrop-filter: blur(...)` over a semi-transparent background. If the user asks for a "flat" design, do NOT remove the blur — it's the template's signature look. Just tone down the colour palette.

## Tech Stack

- React 19 + TypeScript + Vite
- Tailwind CSS v3 + shadcn/ui
- tRPC 11 + Hono + Drizzle ORM + MySQL
- Kimi OAuth 2.0
- D3.js (knowledge graph)
- `react-markdown` + `remark-gfm`
- React Router v7

## Important Notes

- **Fullstack**: `api/`, `db/`, `contracts/`, `Dockerfile`, `tsconfig.server.json`, `vitest.config.ts`, `drizzle.config.ts`, `.backend-features.json` and `.env.example` are all part of this template.
- `.backend-features.json` declares `["auth", "db"]` — `backend-building --template` will pick this up.
- `.env.example` documents every required environment variable — the app will not start without `DATABASE_URL` etc.
- Content changes go through `src/config.ts` and `api/notes-router.ts`. Do NOT hard-code strings into components.
- The two starter-note arrays (client `starterNotes` vs server `STARTER_NOTES`) MUST be kept in sync.
- Wiki-link target titles must match a note's `title` (case-insensitive) for resolution to work. Every `[[link]]` you write in content should point to a note that exists in the same starter set.
