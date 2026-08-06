# 3D Todo Fullstack Template

> **⚠ FULLSTACK — READ FIRST:** You MUST use the `backend-building` skill to make this website fullstack. You need to consider the user's query's intent and change the data accordingly.

A fullstack todo / timer app named **CHRONOS** with a full-viewport WebGL visual — a GPU-driven 3D sphere (or torus-knot particle sculpture) that reacts in real time to the running timer. Supports three timer modes (timer, stopwatch, pomodoro), four visual styles (blob, lattice, metamorph, particles), and four color themes (blue `#3A7CA5`, green `#718355`, red `#6A040F`, yellow `#E6AF2E`).

**You MUST use the `webapp-building` skill with this template info, then use the `backend-building` skill with the `--template` flag, to finish the initialization and change the project adapted to the user's requirement. Then you must deploy the website.**

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Where The Editable Content Lives

The current codebase uses English UI strings inline (e.g. "STANDBY", "ADD NEW TASK"). Replace them directly in the relevant components; do not invent new config files unless necessary.

- **`src/App.tsx`** — timer state labels, `VISUAL MODE` chrome, main layout shell, `SYS // 60 FPS` footer
- **`src/sections/ControlPanel.tsx`** — brand name `CHRONOS`, subtitle `TIME & TASK MANAGEMENT`, mode labels `TIMER / STOPWATCH / POMODORO`, state pill `STANDBY / ACTIVE / COMPLETE`, sliders `FLUID DENSITY / TURBULENCE / HUE SHIFT / BREATH RATE`, presets `05:00 / 15:00 / 25:00 / 45:00` and pomodoro `FOCUS 25 / SHORT 5 / LONG 15`, `START` / `PAUSE` / `RESET` button text, login / logout affordance
- **`src/sections/TodoList.tsx`** — `TODO LIST`, pending / done pluralization, `COMPLETED (N)`, empty state `No tasks yet` / `Add one below`, `ADD NEW TASK`, `Task name...`, `DURATION`, `min`, `ADD TASK`
- **`src/sections/MorphCanvas.tsx`** — shader mode labels only come from `App.tsx`; this file is pure logic, do not modify
- **`src/types/theme.ts`** — the four named themes (blue / green / red / yellow, primaries `#3A7CA5 / #718355 / #6A040F / #E6AF2E`) with panel colors; rename or recolor if the brand requires a different palette
- **`api/todo-router.ts`** — starter todos auto-created for a new authenticated user can be added here if the product wants onboarding content; otherwise leave empty so first sign-in yields an empty list

## Layout Constraints

- **Brand name** (`CHRONOS`): max ~10 characters; rendered as a 3xl black-weight title in the top-left
- **Subtitle** (`TIME & TASK MANAGEMENT`): max ~28 characters, displayed tracked uppercase at 10px
- **Mode labels** (timer / stopwatch / pomodoro): keep under ~10 characters each — sits in a 3-button strip that cannot overflow
- **Preset durations**: exactly 4 entries for the `timer` mode, 3 entries for `pomodoro`. Labels display as `MM:SS` or short text (`FOCUS 25`); max ~10 characters each
- **Visual mode labels** (BLOB / LATTICE / METAMORPH / PARTICLES): keep 4-10 characters each; the strip is center-aligned and will squeeze if longer
- **State labels** (STANDBY / ACTIVE / COMPLETE): max ~10 characters
- **Slider labels** (FLUID DENSITY, TURBULENCE, HUE SHIFT, BREATH RATE): max ~16 characters; two-line label wraps will misalign the slider row
- **Theme swatch count**: exactly 4; adding more breaks the row layout
- **Todo item title**: long titles truncate with ellipsis — no hard limit but ~40 characters reads best

## Theme System

The app ships four themes. Each theme is defined by:

```typescript
{
  name: string,       // internal id
  label: string,      // 2-letter badge (unused in current UI)
  color1: string,     // primary saturated; drives the theme swatch + CSS --accent
  color2: string,     // softer / brighter variant; drives --accent-soft and shader color mix
  color3: string,     // usually #FFFFFF — highlight / urgency target in the shaders
  panelDark: string,  // app background + active button bg
  panelMid: string,   // borders, hover bg, dividers
  panelLight: string, // label text, muted labels
  textMuted: string,  // de-saturated color for line-through / completed text
}
```

When the theme changes, `App.tsx` writes `--panel-dark / --panel-mid / --panel-light / --accent / --accent-soft / --text-muted` to `document.documentElement` — every UI element reads from these CSS variables, so renaming or recoloring a theme only requires editing `src/types/theme.ts`.

Shader visuals also consume `color1 / color2 / color3` directly via three.js uniforms; a brightness floor formula `c1 * 0.5 + vec3(...)` keeps the dark regions tinted by the theme primary instead of a fixed blue. Do not remove this — it is what makes the yellow / red / green themes look correct.

## Required Images

No images required. Every visual is procedural (custom GLSL shaders + three.js geometry).

## Database Schema

The template uses two tables (see `db/schema.ts`):

- **`users`** — managed by Kimi OAuth (id, unionId, name, email, avatar, role)
- **`todos`** — user-owned todos (id, userId, title, durationMs, completedAt, createdAt)

Seed file at `db/seed.ts` can be used to bootstrap todos for a given user id.

## Design Reference

- Base background: from `panelDark` (varies per theme)
- Text: white / `panelLight` / `accent` / `accent-soft` / `text-muted`
- Fonts: Inter body, ultra-black weight for big time display, mono tabular-nums for numbers
- UI effects:
  - WebGL vortex-grade shaders (blob FBM noise, lattice crystal field, metamorph Perlin + wireframe cage, particles torus-knot point sculpture with mouse-driven rotation)
  - Center shader geometry scales and rotates based on `isRunning / isCompleted / timeRatio`
  - CSS variable-driven theme system so panels and visuals recolor together

## Tech Stack

- React 19 + TypeScript + Vite
- Tailwind CSS v3 + shadcn/ui component kit (mostly unused by default)
- Three.js + @react-three/fiber + @react-three/drei for the WebGL sphere / particles
- Custom GLSL vertex + fragment shaders in `src/shaders/`
- tRPC 11 + Hono + Drizzle ORM + MySQL backend
- Kimi OAuth 2.0 authentication
- React Router v7

## Important Notes

- **Fullstack**: `api/`, `db/`, `contracts/`, `Dockerfile`, `tsconfig.server.json`, `vitest.config.ts`, `drizzle.config.ts`, `.backend-features.json` and `.env.example` are all part of this template
- `.backend-features.json` declares `["auth", "db"]`; `backend-building --template` will pick this up
- `.env.example` documents every required environment variable — the app will not start without `DATABASE_URL` etc.
- Do not remove `api/kimi/` — it handles OAuth
- The frontend uses local state for todos by default, but when authenticated it switches to the tRPC-backed `api/todo-router.ts` via the `useTodos` hook
- Shader files (`src/shaders/*.vert`, `*.frag`) are hand-tuned. Do not modify unless you know GLSL
