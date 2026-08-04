# 3 Flow Shader Frontend Template

An immersive creative-studio / portfolio template built around a persistent Three.js fluid shader background. The top three sections (hero, philosophy carousel, featured works gallery) all share the same living shader; the mediums glossary and footer sit on a solid dark base. Each featured work is clickable and opens a full editorial detail page (left article, right sticky image).

This template is well suited for:

- Creative / digital studios
- Interaction design & new-media agencies
- Experiential / installation artists
- Immersive technology brands
- Editorial design portfolios

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## How To Fill This Template

All editable content is in `src/config.ts`. Do not modify component logic unless there is a real bug.

Workflow:

1. Read `src/config.ts` to see the shape of every config object.
2. Fill every field (or leave empty `""` / `[]` for optional bits — empty sections hide automatically).
3. Generate any required images / videos, save them into `public/images/` and `public/videos/`, and reference the paths (e.g. `"images/project-1.jpg"`) in config.

## Config Objects

### `siteConfig`

```ts
export const siteConfig = {
  language: "",          // "en", "zh-CN", "ja", etc. Leave empty unless language is explicit.
  siteTitle: "",         // Browser tab title
  siteDescription: "",   // Meta description
}
```

Constraints:

- `siteTitle`: keep under ~60 characters
- `siteDescription`: keep under ~160 characters

### `navigationConfig`

```ts
export const navigationConfig = {
  brandMark: "",         // Short 1–3 character mark shown top-left
  links: [
    // { label: "", targetId: "" }
  ],
}
```

Constraints:

- `brandMark`: 1–3 characters ideal (e.g. "SL", "溯流", "NØ"). English initials or CJK single words work best. Keep under 4 chars.
- `links`: 3–4 items ideal.
- `label`: keep under ~8 English characters or ~4 Chinese characters. Anything longer crowds the header.
- `targetId`: must match one of the section wrapper ids in `App.tsx`. Valid values: `"hero-section"`, `"philosophy"`, `"gallery"`, `"mediums"`, `"footer"`. Using any other value will produce a non-working link.

### `heroConfig`

```ts
export const heroConfig = {
  wordmarkText: "",       // Big wordmark on the left half, 1–2 lines long
  eyebrow: "",            // Small UPPERCASE label
  titleLine1: "",         // Title line 1
  titleLine2: "",         // Title line 2 (optional)
  descriptionLine1: "",   // Short description line 1
  descriptionLine2: "",   // Short description line 2 (optional)
  ctaText: "",            // Button label
  ctaTargetId: "",        // Scroll target id when the CTA is clicked, e.g. "philosophy"
}
```

Constraints:

- `wordmarkText`: keep under ~14 characters. Big serif rendering — short phrases only (e.g. "SULIU STUDIO", "溯流 SULIU").
- `eyebrow`: keep under ~22 characters. Will be rendered in uppercase tracking.
- `titleLine1` / `titleLine2`: each line under ~14 English characters or ~8 Chinese characters. The two lines stack vertically via `<br>` — don't try to cram a sentence into one line.
- `descriptionLine1` / `descriptionLine2`: each line under ~45 English characters or ~22 Chinese characters.
- `ctaText`: keep under ~10 characters.
- `ctaTargetId`: typically `"philosophy"` (next section down).

### `philosophyConfig`

```ts
export const philosophyConfig = {
  eyebrow: "",            // Small uppercase label
  title: "",              // Section title
  body: "",               // Short paragraph
  rollingWords: [],       // Short words shown in the 3D rolling ring
}
```

Constraints:

- `eyebrow`: keep under ~16 characters.
- `title`: keep under ~14 English characters or ~8 Chinese characters.
- `body`: 1–3 short sentences, under ~160 characters total.
- `rollingWords`: 4–6 items ideal. Each word must be short — **keep under ~12 characters** — because the ring has very large typography (`clamp(42px, 8vw, 100px)`). Single English words in UPPERCASE read best (e.g. "PERCEPTION", "RESONANCE", "FLUIDITY"). Long multi-word phrases break the ring.

### `galleryConfig`

```ts
export const galleryConfig = {
  sectionLabel: "",       // Small UPPERCASE label, e.g. "FEATURED WORKS / 002"
  title: "",              // Section heading
  projects: [
    // {
    //   id: "",                           // Unique id (used internally)
    //   title: "",                        // Project short title
    //   location: "",                     // Location
    //   year: "",                         // Year string
    //   image: "images/project-1.jpg",    // Main image (portrait 1024×1536)
    //   subtitle: "",                     // One-line italic subtitle on the detail page
    //   meta: [ { label: "", value: "" } ],
    //   paragraphs: [ "" ],               // 2–4 body paragraphs on the detail page
    // }
  ],
}
```

Constraints:

- `sectionLabel`: keep under ~30 characters.
- `title`: keep under ~14 English characters or ~8 Chinese characters.
- `projects`: exactly **4 projects** gives the best staggered vertical rhythm. The layout uses handcrafted `marginTop` offsets per index (0 / -55vh / -20vh / -45vh) — with fewer or more items the stagger still works but looks less intentional.
- `id`: unique string per project. Used for selection — must be unique.
- `title`: keep under ~8 characters. Large serif in both list and detail.
- `location` / `year`: each under ~8 characters.
- `image`: portrait aspect ratio (1024×1536 recommended). Images are displayed at `aspectRatio: 1024 / 1536`.
- `subtitle` (detail page): 1 short editorial line, under ~40 characters.
- `meta`: 2–4 rows ideal. Each `label` under ~6 characters (uppercase), each `value` under ~28 characters.
- `paragraphs` (detail page body): 2–4 paragraphs, each roughly 80–200 characters. Body is justified serif at `fontSize: 15, lineHeight: 2` so very long paragraphs read fine.

### `mediumsConfig`

```ts
export const mediumsConfig = {
  sectionLabel: "",       // UPPERCASE label above the list
  items: [
    // { cn: "", en: "", description: "" }
  ],
}
```

Constraints:

- `sectionLabel`: keep under ~14 characters.
- `items`: 3–5 items ideal. Each row is an SVG gooey-hover swap.
- `cn`: keep under ~6 characters (rendered in large serif).
- `en`: keep under ~20 characters (rendered in bold sans, UPPERCASE). Must be short — very long English phrases clip the row width.
- `description`: 1–3 sentences, under ~140 characters. Shown on the right on hover.

Note: the `cn` / `en` field names come from the original Chinese/English bilingual design, but you can use any two languages (or the same language twice) — e.g. a display word + its category name. They are two states of the same row that gooey-swap on hover.

### `footerConfig`

```ts
export const footerConfig = {
  visionText: "",         // Long vision paragraph, serif display
  brandName: "",          // Bottom-left brand name
  columns: [
    // {
    //   heading: "",
    //   entries: [
    //     { text: "", href: "" },         // Rendered as <a> when href is set
    //     { text: "Multi\nline\ntext" },  // Rendered as plain text when href is absent
    //   ],
    // },
  ],
  copyright: "",
  videoPath: "",          // Optional ambient footer video, e.g. "videos/footer.mp4"
}
```

Constraints:

- `visionText`: ~2–4 sentences, up to ~280 characters total. Rendered as large serif, high line-height.
- `brandName`: keep under ~12 characters.
- `columns`: 3 columns is the visual sweet spot. 2 also works.
- `columns[].heading`: keep under ~6 characters, rendered UPPERCASE.
- `columns[].entries[].text`: each entry line under ~30 characters. For multi-line addresses, use `\n` inside `text` and leave `href` empty.
- `copyright`: keep under ~40 characters.
- `videoPath`: optional. If provided, must be a silent, short, looping mp4 (muted and decorative — fills the background at 15% opacity).

### `projectDetailConfig`

```ts
export const projectDetailConfig = {
  backLabel: "",          // Back button label on the detail page
}
```

Constraints:

- `backLabel`: keep under ~8 characters. Suggested: `"← 返回"`, `"← Back"`, `"← Retour"`.

## Required Assets

Paths below are relative to `public/`.

### Images (1 per project in `galleryConfig.projects`)
- `images/project-1.jpg`, `images/project-2.jpg`, … — portrait **1024×1536** recommended. High-quality editorial photography, dark/moody palettes read best against the fluid shader.

### Videos (optional)
- `videos/footer.mp4` — short, silent, looping ambient clip. Shown at 15% opacity behind the footer. Keep under 5 seconds, small filesize.

## Asset Generation Note

When this template requires image or video assets, write descriptive prompts, call `generate_image` / `generate_video`, save outputs into `public/images/` or `public/videos/`, and then reference those final paths in `src/config.ts`. For the shader-heavy aesthetic, prefer dark/moody architectural / installation / light-study photography for gallery images.

## Design Reference

**Colors:**
- Base dark: `#050A0F`
- Ivory on dark: `#EDE8E4`
- Cyan accent: `#30B0D0`
- All hero / philosophy / gallery text is white with `textShadow: '0 2px 24px rgba(0,0,0,0.45)'` so it stays readable on the shader.

**Fonts** (Google Fonts, loaded in `index.html`):
- Display serif: Noto Serif SC
- Body sans: Noto Sans SC

Both families support CJK + Latin — suitable for Chinese, Japanese, Korean, and Western languages. If you need to retarget for a non-CJK language, the existing font loads still work (Noto Serif SC / Noto Sans SC render Latin characters well).

**Animations:**
- Persistent Three.js fluid shader (shared background across hero / philosophy / gallery)
- IntersectionObserver pauses the shader render loop below the fold
- GSAP ScrollTrigger scrub drives a 3D rolling text ring with speed-reactive skew + motion blur
- Lenis smooth scroll globally (`lerp: 0.05`)
- Staggered vertical overlap on gallery projects — scroll position is preserved when opening / returning from a detail page
- Gooey SVG filter swap on mediums rows
- Footer ambient video at 15% opacity (if provided)
