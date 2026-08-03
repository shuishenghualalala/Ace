# Vortex Portfolio Frontend Template

A minimal photography / portfolio site built around a WebGL cylindrical image vortex. The landing route (`/`) shows a swirling wall of photographs that rotate as the user scrolls; clicking any image — the central one or any background instance — opens a fullscreen detail overlay with a per-image caption. The secondary route (`/info`) is a quiet two-column layout with a bio on the left and a contact list on the right.

Best suited for: independent photographers, galleries, studios, small portfolio / about sites.

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
  language: "",       // BCP-47 language code, e.g. "en", "zh-CN", "ja". Leave empty if unspecified.
  brandName: "",      // Top-left logo on both pages. Serif, 18px.
                      // Example: "MySpace"
                      // Constraint: max ~14 characters (longer breaks the single-line logo slot)
  copyright: "",      // Centered at the bottom of the home page; also shown under the Info contact list.
                      // Example: "© 2026 MySpace"
                      // Constraint: max ~40 characters
};
```

### `navigationConfig`

```typescript
export const navigationConfig: NavigationConfig = {
  infoLinkLabel: "",  // Top-right link on the home page, routes to /info.
                      // Example: "Info"
                      // Constraint: max ~10 characters
};
```

### `infoPageConfig`

The `/info` sub-page. A two-column layout: bio paragraphs on the left, contact list vertically centered on the right.

```typescript
export const infoPageConfig: InfoPageConfig = {
  backLinkLabel: "",     // Top-right link on the info page, routes to /.
                         // Example: "Back"
                         // Constraint: max ~10 characters

  eyebrow: "",           // Small uppercase lead-in above the title.
                         // Example: "About — Independent Photographer"
                         // Constraint: max ~60 characters (it's tracked uppercase at 11px)

  title: "",             // Large serif headline. Rendered at clamp(32px, 3.6vw, 52px).
                         // Example: "I make quiet photographs of restless places."
                         // Constraint: max ~80 characters; 1–2 lines is ideal

  paragraphs: [],        // Bio paragraphs in reading order.
                         // Recommended: 2–4 paragraphs, each 2–4 sentences.
                         // Each paragraph ~300–500 characters.

  contactLabel: "",      // Uppercase eyebrow above the contact list.
                         // Example: "Contact"
                         // Constraint: max ~20 characters

  contactEntries: [],    // See entry shape below. Recommended 3–6 entries.
};
```

Each entry in `contactEntries` has this shape:

```typescript
{
  label: "",     // Left column label. Example: "Email", "Instagram", "Studio".
                 // Constraint: max ~16 characters (left column is 130px wide)
  value: "",     // Right column value. Use "\n" to insert line breaks.
                 // Example: "hello@myspace.studio"
                 // Example (multi-line): "Keizersgracht 148, Amsterdam\nRua da Boavista 61, Lisbon"
  href: "",      // Optional. If provided, the value becomes a clickable link.
                 // Use "mailto:...", "tel:...", or "https://..." forms.
                 // External links (starting with http) open in a new tab.
}
```

### `overlayConfig`

Text labels shown inside the image detail overlay (the modal that opens when a photo is clicked). All four are optional — any empty field hides that particular label in the overlay.

```typescript
export const overlayConfig: OverlayConfig = {
  frameDetailLabel: "",  // Appended after the image's category in the overlay eyebrow.
                         // Example: "Frame Detail" → eyebrow reads "Portrait — Frame Detail"
                         // Constraint: max ~18 characters
  fileLabel: "",         // Meta-strip label shown next to the filename.
                         // Example: "File"
                         // Constraint: max ~12 characters
  seriesLabel: "",       // Meta-strip label shown next to the series name (parsed from the image title).
                         // Example: "Series"
                         // Constraint: max ~12 characters
  closeLabel: "",        // Text on the close button under the description.
                         // Example: "Close"
                         // Constraint: max ~12 characters
};
```

### `galleryConfig`

The image set that powers the vortex AND the detail overlay. Every image in `galleryConfig.images` gets a slot in the WebGL vortex and a caption to display when clicked.

```typescript
export const galleryConfig: GalleryConfig = {
  images: [
    {
      src: "/images/photo_01.jpg",        // path inside public/, always starting with "/images/"
      category: "Portrait",               // short label shown in the overlay eyebrow
      title: "Strangers — No. 01",        // bold headline in the overlay
      description: "A stranger I asked..."// paragraph caption in the overlay
    },
    // ...
  ],
};
```

**Layout constraints for each image entry:**

- `src`: always `"/images/<filename>.jpg"`. Do NOT use absolute URLs or external hosts.
- `category`: max ~20 characters. Good: `"Portrait"`, `"Still Life"`, `"城市街头"`. Bad: long sentences.
- `title`: max ~60 characters; displayed at clamp(28px, 2.6vw, 40px) serif. A concise "Series — No. NN" style works well; single short phrases also fine.
- `description`: 1–3 sentences, ~200–400 characters. This is the paragraph caption. Longer works but the panel scrolls.

**How many images:**

- The vortex builds a cylinder of 600 instances that randomly pick from this list. More images → less visible repetition.
- Recommended: 20–60 images for a full-looking vortex.
- Minimum: 1 (will work but every vortex card is the same).

## Required Images

All images go in `public/images/` and must be referenced from `galleryConfig.images` with paths like `/images/<filename>.jpg`.

**Recommended specs:**
- Orientation: **4:5 portrait** works best (the vortex cells are 4:5). Landscape and square work but letterbox inside cells.
- Resolution: ~1600px on the long edge. High-resolution is used when the image is zoomed in the detail overlay.
- Format: `.jpg` preferred (`.png` also works).
- Avoid pure white subjects / backgrounds — the page background is white.

## Asset Generation Note

When generating images for this template, the builder should:
1. Write 20–60 photography-style image prompts matching the user's query (tone, palette, subject).
2. Call `generate_image` for each one (opaque → `.jpg`).
3. Save each output into `public/images/` with a descriptive filename, e.g. `portrait_01.jpg`, `street_07.jpg`.
4. For each generated image, write a matching entry in `galleryConfig.images` with the final `src` path, the `category`, a `title`, and a short `description`. Keep captions in the same voice as the rest of the site.

## Design

**Colors:**
- Background: `#ffffff`
- Text: `#000000`
- Overlay scrim: `rgba(10,10,10,0.94)` with blur

**Fonts:**
- Display / headings: `'Times New Roman', serif`
- Body / UI: `system-ui, -apple-system, sans-serif`

**Animations / interaction:**
- WebGL instanced vortex driven by wheel velocity (Lenis for smoothing)
- Center plane cycles through the image list as the user scrolls
- Clicking an image pauses the vortex and animates the detail overlay in (opacity + scale)
- ESC, clicking the backdrop, the close button, or the × corner all close the overlay

## Design Notes

- The page is a single-viewport hero — there is no second section below the fold on `/`.
- The `/info` page is one tall panel; content must fit comfortably with `paragraphs` of reasonable length.
- Keep `brandName` / navigation labels short; the corners are the only nav and long strings will wrap onto the canvas.
- The overlay is designed for readable captions; avoid filling `description` with marketing copy — write like a photographer's notebook.
