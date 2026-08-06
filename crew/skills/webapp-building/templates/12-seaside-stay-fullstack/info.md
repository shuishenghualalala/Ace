# Video Hotel Fullstack Template

> **⚠ FULLSTACK — READ FIRST:** You MUST use the `backend-building` skill to make this website fullstack. You need to consider the user's query's intent and change the data accordingly.

A fullstack landing page for a luxury seaside hotel named **LUNAMARE**. Full-viewport video hero, a rooms & residences gallery with per-room detail pages, a services section on a second video, and a split shader / booking-form finale. Fullstack-wired reservation flow, Kimi auth, and a relational database.

**You MUST use the `webapp-building` skill with this template info, then use the `backend-building` skill with the `--template` flag, to finish the initialization and change the project adapted to the user's requirement. Then you must deploy the website.**

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Where The Editable Content Lives

The template uses inline strings and a single static data file for display content — only reservation submissions are persisted to the database. To re-skin for a different hotel, edit these files directly:

- **`src/sections/Spatial.tsx`** — top hero: eyebrow (`Luxury Seaside Retreat · Est. 1998`), big title (`Where the Sea / Meets Stillness`), subtitle paragraph, primary button (`Reserve Your Stay`), secondary link (`Explore Rooms →`). Background video is `/videos/sea-hotel.mp4`.
- **`src/sections/Header.tsx`** — brand wordmark (`LUNAMARE`) and nav items (`Rooms`, `Experiences`, `Contact`)
- **`src/sections/Philosophy.tsx`** — large quote paragraph and three uppercase tags (`Coastal`, `Slow Living`, `Timeless`)
- **`src/sections/Works.tsx`** — section heading (`Rooms & Residences`) and eyebrow (`Featured Stays`). **Room cards are rendered from `src/data/rooms.ts`.**
- **`src/sections/Capabilities.tsx`** — services section heading (`Hotel Services`), intro paragraph, and the 8-item bullet grid of service labels + one-line descriptions. Background video is `/videos/spatial.mp4`.
- **`src/sections/Hero.tsx`** — bottom split section: big left-side heading (`Plan your / coastal stay`), form heading (`Reserve a room or send us a note.`) and the inquiry form fields. **This form writes to `reservation_requests` via tRPC.**
- **`src/sections/Footer.tsx`** — three office columns (Amalfi Coast / Malibu / Phuket) and contact block (`reservations@lunamare.com` etc.)
- **`src/sections/Preloader.tsx`** — intro splash; displays the brand wordmark on first load
- **`src/pages/RoomDetail.tsx`** — per-room page layout; content comes from `src/data/rooms.ts`. The `Reserve This Room` button writes to `reservation_requests` via tRPC
- **`src/data/rooms.ts`** — **the source of truth for all six rooms**. Each `Room` has: `id`, `title`, `client` (wing/residence name), `img` (Unsplash URL or `/images/...`), `tagline`, `description` (paragraph array), `features` (string array), `price`, `priceNote`, `sqm`, `occupancy`, `bed`. Modify this array to change the room lineup.
- **`index.html`** — `<title>` string
- **`api/reservation-router.ts`** — tRPC router that persists form submissions and returns the current user's reservations

## Layout Constraints

- **Hero title** (`Spatial.tsx` h1): 2 lines, each ≤ 16 characters — rendered at clamp(44px, 7vw, 108px)
- **Hero eyebrow**: max ~40 characters, uppercase tracked
- **Hero subtitle**: 1 short paragraph, max ~240 characters
- **Header brand wordmark**: max ~12 characters — tracks at 0.22em, turns from white to black on scroll past hero
- **Nav items**: 3 entries, each ≤ ~14 characters; rendered in a horizontal bar
- **Philosophy quote**: one long paragraph, max ~200 characters; rendered at clamp(28px, 4vw, 60px)
- **Services bullet grid**: exactly 8 items reads best (auto-fit grid of ~320px tiles → 2 or 3 columns). Each service `label` ≤ ~22 characters; `detail` ≤ ~70 characters
- **Rooms**: exactly 6 entries in `rooms.ts`; fewer looks sparse on desktop's 2-column grid (each card is ~760px wide × ~428px tall at 16:9). `title` ≤ ~18 characters, `client` ≤ ~22 characters
- **Room detail `tagline`** ≤ ~80 characters (big intro line)
- **Room `features`**: 4–6 entries; each ≤ ~50 characters (rendered in a 2-column list)
- **Footer brand**: same wordmark as header (≤ ~12 characters), rendered giant (clamp 80–320px)
- **Office columns**: 3 cities plus a Contact column (4 cells). City name ≤ ~16 characters, address ≤ ~60 characters

## Database Schema

The template uses 2 tables (see `db/schema.ts`):

- **`users`** — managed by Kimi OAuth (id, unionId, name, email, avatar, role)
- **`reservation_requests`** — guest booking + inquiry submissions (id, userId nullable, checkInDate, checkOutDate, guests, roomType, roomId nullable, fullName, email, message, status enum `pending / confirmed / cancelled`, createdAt)

The six rooms live on the frontend in `src/data/rooms.ts` and are **not** duplicated into the database — the display is static, only reservation writes are persisted.

## Required Assets

### Videos (required — place in `public/videos/`)

- **`/videos/sea-hotel.mp4`** — the full-viewport top hero video. Recommended: ~10s loop, 1920×1080, H.264, coastal / seascape / luxury architecture. Dark, slow, and high-contrast so the overlaid white title reads clearly
- **`/videos/spatial.mp4`** — the background for the services section. Recommended: ~10s loop, 1920×1080, dark architectural / interior footage; the section has a 60% black overlay so lighter footage still works

Leave either path empty → the section renders on plain `#0b0b0b`.

### Images

Room photos come from Unsplash by URL (`https://images.unsplash.com/photo-...?w=1600&h=900&fit=crop&q=80`). No local image files are required. To use curated images, place files in `public/images/` and edit each `room.img` in `src/data/rooms.ts`.

## Auth

Kimi OAuth is the only auth provider. The `/login` page offers the "Sign in with Kimi" button. Sign-in populates the `users` table on first visit.

Reservation behavior around auth:

- The **bottom inquiry form** (`Hero.tsx`) accepts submissions anonymously — `userId` is `null` when the submitter is signed out. When signed in, the row stores the current `userId`
- The **"Reserve This Room"** button on each room detail page routes through Kimi auth first if the user is signed out, then submits with `roomId` + `roomType` pre-filled

## Design Reference

- Backgrounds: alternating `#0b0b0b` (hero / services / bottom split) and `#ffffff` / `#f4f4f5` (Philosophy / Works)
- Top hero has a vertical 0.55 → 0.25 → 0.55 black gradient overlay; services section has a flat 60% black overlay
- Fonts: system sans-serif stack, Helvetica Neue display
- Motion:
  - GSAP `ScrollTrigger` parallax + fade-ins on Works and Philosophy
  - A custom canvas-based glitch effect on the Works cards tied to scroll speed (keep this — it's the signature interaction)
  - A rotating SVG orbital badge in the services header
  - A GLSL rainbow shader in the bottom-left half of the final section (three.js + raw vertex / fragment shaders)
- Header: fixed, transparent over the hero, turns solid white with black text once scrolled past 85vh (also forced solid when a room detail page is open)

## Tech Stack

- React 19 + TypeScript + Vite
- Tailwind CSS v3 + shadcn/ui component kit (components present but rarely used)
- GSAP + ScrollTrigger for parallax
- three.js for the bottom-section shader
- tRPC 11 + Hono + Drizzle ORM + MySQL backend
- Kimi OAuth 2.0
- React Router v7

## Important Notes

- **Fullstack**: `api/`, `db/`, `contracts/`, `Dockerfile`, `tsconfig.server.json`, `vitest.config.ts`, `drizzle.config.ts`, `.backend-features.json` and `.env.example` are all part of this template
- `.backend-features.json` declares `["auth", "db"]`; `backend-building --template` will pick this up
- `.env.example` documents every required environment variable — the app will not start without `DATABASE_URL` etc.
- Do not remove `api/kimi/` — it handles OAuth
- **Do not duplicate `src/data/rooms.ts` into the database.** It is the single source of truth for displayed rooms; only `reservation_requests` persist guest submissions
- The bottom section's form and each room's "Reserve This Room" button are the only frontend controls wired to the backend; header nav items are in-page scroll anchors
