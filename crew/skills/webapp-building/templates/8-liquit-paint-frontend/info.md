# Shader Painting Frontend Template

A long-scroll brutalist **exhibition / gallery** site powered by WebGL shaders. Three sections plus clickable sub-pages:

1. **Hero** — a static image (e.g. a painting) is continuously distorted by a simplex-noise UV shader, producing a slow fluid / oil-painting flow. Typewritten subtitle fades in, then a CTA button.
2. **Selected Works** — a 4-column parallax grid of cards. Each card is clickable and routes to its own detail page.
3. **The Instant** — three kinetic text lines staggered over a looping background video.

Each card opens a detail page at `/work/:id` with a large image on top and a multi-paragraph article below (artist / location / medium + prose).

Best suited for: art gallery / exhibition sites, photography portfolios, editorial collections, long-form visual essays.

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Config File

All content is defined in `src/config.ts`. Fill in every field according to the sections below. Do not modify any component files.

### `siteConfig`

```typescript
export const siteConfig: SiteConfig = {
  language: "",       // BCP-47, e.g. "en", "zh-CN", "ja". Leave empty if unspecified.
  title: "",          // Browser tab title. Also update <title> in index.html to match.
  description: "",    // Meta description used by search engines / social cards.
  brandName: "",      // Reserved; not currently used in the UI. Can be left empty.
};
```

### `heroConfig`

Full-viewport hero with the fluid-flow shader.

```typescript
export const heroConfig: HeroConfig = {
  titleText: "",        // Big Geist Mono title shown large and centered.
                        // Example: "Impressionism", "The Northern Light", "Atlas of Form"
                        // Constraint: max ~20 characters — rendered at clamp(3rem, 8vw, 7rem).

  subtitleLines: [],    // Array of strings. Joined with "\n" and typewritten line by line.
                        // Each line is rendered as a separate <div>, so break paragraphs naturally.
                        // Recommended: 2–5 lines, each 40–90 characters.

  ctaLabel: "",         // CTA button text. Rendered UPPERCASE with wide letter-spacing.
                        // Example: "Enter the Exhibition", "Begin"
                        // Constraint: max ~24 characters.

  roomLabel: "",        // Small uppercase label centered at the very bottom of the hero.
                        // Example: "Room 01 // Light Studies"
                        // Constraint: max ~40 characters.

  fluidImagePath: "",   // Path to the source image the shader distorts. Must start with "/images/".
                        // The image fills the hero at "cover" scale.
                        // Example: "/images/hero-painting.jpg"
                        // Leave empty → hero renders on a plain black background.
};
```

### `galleryConfig`

The cascading gallery section.

```typescript
export const galleryConfig: GalleryConfig = {
  eyebrowLabel: "",     // Small mono label above the gallery title in the sticky sidebar.
                        // Example: "ROOM 02 // GALLERY"
                        // Constraint: max ~30 characters.

  titleLines: [],       // Array of strings rendered as a stacked title (<br> between).
                        // Example: ["Selected", "Works"] → two-line display
                        // Constraint: 1–3 short lines; each ≤ 12 characters.

  stats: [              // Key/value rows shown below the title. 3–6 rows recommended.
    { label: "", value: "" },
  ],

  sideLabel: "",        // Small faded mono label at the very bottom of the sticky sidebar.
                        // Example: "STUDIO::ROOM_02"
                        // Constraint: max ~24 characters.

  works: [],            // Array of WorkItem — the cards. See the WorkItem schema below.
                        // Recommended: 16–32 entries (4 columns × 4–8 rows).
                        // Fewer than 4 leaves visible empty columns; more than 40 makes the page too long.
};
```

**`WorkItem` schema** (one entry per card):

```typescript
{
  id: "",          // Unique short id. Used in the URL as /work/{id.toLowerCase()}.
                   // Example: "NO-001", "W-014". Must be URL-safe.
                   // Constraint: max ~12 characters; unique across the array.

  title: "",       // Card title — displayed bold. Underscores become spaces on the detail page.
                   // Example: "SUNRISE", "WATER_LILIES"
                   // Constraint: max ~18 characters.

  type: "",        // Small eyebrow label shown next to the id on the card.
                   // Example: "oil-on-canvas", "garden-study"
                   // Constraint: max ~20 characters.

  status: "",      // Left-bottom status badge.
                   // Example: "ON VIEW", "ARCHIVED", "LOAN", "ROTATING"
                   // Constraint: max ~12 characters.

  metrics: "",     // Right-bottom value. Usually a year or short number.
                   // Example: "1872"
                   // Constraint: max ~8 characters.

  image: "",       // Path from public/, e.g. "/images/work-1.jpg".
                   // MUST be a topic-relevant image. Generate it with generate_image, or
                   // source/download a relevant asset. Do NOT point this at placeholder
                   // services like picsum.photos, unsplash random, lorem.space, etc. —
                   // they return topic-unrelated images and the page will look incoherent.

  artist: "",      // Shown on the detail page under the Artist label.
                   // Example: "Claude Monet". Leave empty to hide the row.

  location: "",    // Detail page Location row. Example: "Le Havre"

  medium: "",      // Detail page Medium row. Example: "Oil on canvas, 48 × 63 cm"

  article: "",     // The detail-page article. Plain text.
                   // Separate paragraphs with two newlines (\n\n).
                   // Recommended: 2–4 paragraphs, ~150–400 characters each.
                   // No Markdown; line breaks within a paragraph are preserved but not styled.
}
```

### `instantConfig`

The final section — three lines of kinetic text over a looping video.

```typescript
export const instantConfig: InstantConfig = {
  textLines: ["", "", ""],  // EXACTLY 3 strings.
                            // [0] — big Geist Mono heading (1 word, max ~10 chars).
                            //       Rendered at clamp(3rem, 10vw, 8rem).
                            // [1] — assembly phrase, slides in horizontally as one line.
                            //       Example (EN): "seen before it hardens"
                            //       Example (ZH): "在色彩凝固之前 捕捉它"
                            //       Constraints:
                            //         - Must fit on ONE line (nowrap). Max ~40 English chars,
                            //           or ~18 Chinese chars, or things get clipped at most viewports.
                            //         - Do NOT wrap the phrase in literal "[ ... ]" brackets.
                            //           The square-bracket styling in the original English example
                            //           was stylistic only; in Chinese / Japanese / Korean it reads
                            //           as incidental punctuation and looks odd.
                            // [2] — small mono caption below.
                            //       Constraint: max ~80 characters.

  videoPath: "",            // Path to the background loop video. Must start with "/videos/".
                            // Example: "/videos/ambient.mp4"
                            // Leave empty → section renders on plain black.
                            // Recommended: ~10 second loop, dark/abstract so text stays legible.

  roomLabel: "",            // Small mono label in the bottom-right corner.
                            // Example: "Room 03 // The Instant"
                            // Constraint: max ~40 characters.
};
```

### `footerConfig`

```typescript
export const footerConfig: FooterConfig = {
  brandText: "",                // Big brand name shown top-left of the footer. Serif display.
                                // Constraint: max ~20 characters.

  taglineLines: [],             // Array of short uppercase mono strings shown below the brand.
                                // Recommended: 2–3 lines, each ≤ 40 characters.

  navigationHeading: "",        // Small heading above the nav list. Example: "NAVIGATION"
  navigationLinks: [            // Section names for the footer nav list.
    { label: "", href: "" },
  ],                            // Recommended: 3–5 entries. href is optional; currently not wired.

  contactHeading: "",           // Small heading above the contact list. Example: "CONTACT"
  contactLinks: [               // Contact items (email, social). href optional.
    { label: "", href: "" },
  ],                            // Recommended: 2–4 entries.

  copyright: "",                // Bottom-left fine print. Example: "© 2026 STUDIO NAME"
  creditText: "",               // Bottom-right fine print. Example: "BUILT WITH LIGHT AND GESTURE"
};
```

### `workDetailConfig`

Labels for the `/work/:id` sub-page. All fields are short UI strings.

```typescript
export const workDetailConfig: WorkDetailConfig = {
  backLabel: "",             // Top-left back button. Example: "← BACK"
  artistLabel: "",           // Left column of the meta dl. Example: "Artist"
  locationLabel: "",         // Example: "Location"
  mediumLabel: "",           // Example: "Medium"
  backToGalleryLabel: "",    // Bottom-left text link. Example: "← Back to gallery"
  metaRoomSuffix: "",        // Appended after the work id in top-right. Example: "ROOM 02"
  footerNote: "",            // Bottom-right note. Example: "Your Brand · Room 02"
  notFoundTitle: "",         // Shown when the URL id matches no work. Example: "404 · Work not found"
  notFoundLink: "",          // Back-to-gallery link text when not found. Example: "← BACK TO GALLERY"
};
```

## Layout Constraints Summary

- **Hero title**: 1 line, ≤ 20 characters (rendered very large)
- **Hero subtitle**: 2–5 lines, each 40–90 characters — the typewriter speed is fixed (38 ms/char), so ~200 chars takes ~8 seconds
- **Gallery titleLines**: 1–3 short lines; ≤ 12 characters each
- **Gallery works**: 16–32 entries ideal; max ~40 (page grows tall)
- **Instant textLines[0]**: single short word (3–10 chars)
- **Instant textLines[1]**: must fit on one line, slides in horizontally. ~40 English chars / ~18 Chinese chars max. Do NOT wrap in `[ ... ]` brackets — they are a stylistic artifact of the original English example and look odd in CJK content.
- **CTA / roomLabels / meta rows**: short uppercase strings, ≤ 40 characters

## Required Images

Two kinds of images are required:

- **`heroConfig.fluidImagePath`** — the image that the shader distorts. Any aspect ratio works (it is covered across the hero). Painterly / textured images work best — oil paintings, dense fabric, peeled walls. JPEG recommended. High resolution (at least 2000 px on the long edge) so the distortion stays crisp.

- **`galleryConfig.works[].image`** — one image per work card. These appear both on the grid (480×320 crop) and on the work detail page (1400×933 crop), so favor 3:2 landscape at ~1600×1067 or larger, JPEG. Every image MUST be topic-relevant to the work it illustrates. There are two acceptable sources:
  1. **Generated** — call `generate_image` with a prompt specific to that work (artist / subject / medium), save into `public/images/`, then set `image: "/images/work-XX.jpg"`.
  2. **Searched / downloaded** — if you fetch an asset from the web, it must match the work's subject. Keep in mind the subject does not have to match the template's original theme (impressionism) — match whatever the **user's query** is about.

  ❌ Do NOT point `image` at placeholder services (picsum.photos, unsplash random, lorem.space, etc.). They return topic-unrelated imagery and the gallery ends up visually incoherent. The previous version of this template used `picsum.photos/seed/{seed}` and it produced exactly this problem.

## Required Videos

- **`instantConfig.videoPath`** — a short looping mp4 for the Instant section. Recommended: dark, slow, abstract content (stars, water, smoke, particles) so the bright kinetic text stays readable. ~10 seconds, no audio needed (muted anyway).

Leave `videoPath` empty to skip the video — the section will render on a plain black background.

## Asset Generation Note

When generating assets for this template:

1. **Hero image** — write a prompt for a single painterly / textured image. Call `generate_image` with `background="opaque"`, save as `.jpg` into `public/images/` (e.g. `public/images/hero-source.jpg`), then set `heroConfig.fluidImagePath = "/images/hero-source.jpg"`.
2. **Instant video** — write a prompt for a ~10-second dark abstract loop. Call `generate_video`, save into `public/videos/ambient.mp4`, then set `instantConfig.videoPath = "/videos/ambient.mp4"`.
3. **Gallery cards** — one image per work. For each `WorkItem`, either call `generate_image` with a prompt tailored to that work's subject (artist, scene, medium), or download a topic-relevant asset. Save to `public/images/` (e.g. `work-1.jpg`, `work-2.jpg`, …) and set the corresponding `image` field to `"/images/work-N.jpg"`. NEVER use placeholder/random image services — the page must look coherent with the user's topic.

## Design

- Backgrounds: `#000000` (hero / instant / footer) and `#ffffff` (gallery).
- Text: white on dark, black on white.
- Fonts: Geist Mono (display), Inter (body), Space Mono (small labels). Loaded from Google Fonts.
- Motion: WebGL simplex noise UV distortion (hero), typewriter (subtitle), GSAP parallax (gallery, ±4–12%), staggered translate-up (Instant).
- Cursor: custom cursor active on all routes (replaces the native cursor — designed for desktop).

## Tech Stack

- React 19 + TypeScript + Vite
- Tailwind CSS v3 + shadcn/ui (shadcn components present but rarely used)
- Three.js for the fluid hero shader and the ambient mesh behind the gallery
- GSAP + ScrollTrigger for parallax on the gallery tracks
- `react-router` v7 for the `/` → `/work/:id` split

## Important Notes

- Do not modify component files unless fixing a bug. All content must be driven from `src/config.ts`.
- If any config array is empty (`works`, `subtitleLines`, `textLines`), the corresponding section gracefully hides or renders a reduced form — but for the site to feel complete, all sections should be populated.
- The custom cursor is active on all routes; if the deployment is primarily mobile-first, consider removing `<CustomCursor />` from `src/App.tsx`.
- The gallery's card URLs use the `id` field lowercased (e.g. `/work/no-001`). Keep ids unique and URL-safe.
- The WebGL shader intensity is controlled in `src/sections/FluidSubconscious.tsx` (`uIntensity`, currently 0.6 desktop / 0.4 mobile). Do not remove this file.
