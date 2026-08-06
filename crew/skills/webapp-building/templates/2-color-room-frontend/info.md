# 3D Gallery Frontend Template

A dark-themed exhibition website built with CSS 3D perspective rooms, Three.js particle animations, fullscreen video sections, and a terminal-style ticker footer. Designed for art exhibitions, museum showcases, literary retrospectives, or any cultural event needing an immersive, gallery-like web experience.

The site has 6 main sections in order: Hero Room Gallery, Particle Sculpture (editorial text + 3D particles), Lighthouse Video (fullscreen video with glass overlay), Image Gallery (3x3 grid with lightbox), Waves Video (widescreen video with text overlay), and Footer with terminal ticker.

## Language

If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content

The actual content of the website should match the user's query.

## How to Fill `src/config.ts`

Open `src/config.ts` and fill every field. All strings default to `""` and arrays to `[]`. You must provide values for all fields for the site to render.

### siteConfig

```typescript
export const siteConfig: SiteConfig = {
  language: "",      // Language code matching the site content (e.g. "en", "zh-CN", "ja")
  brandName: "",     // Short brand/logo text (top-left corner). Each character is animated individually on hover.
                     // CONSTRAINT: Keep to 3-8 uppercase characters. Longer names break the nav layout.
}
```

### navigationConfig

```typescript
export const navigationConfig: NavigationConfig = {
  menuLabel: "",     // Button text to open fullscreen menu (e.g. "MENU"). Uppercase recommended.
  closeLabel: "",    // Button text to close fullscreen menu (e.g. "CLOSE"). Uppercase recommended.
  fullscreenMenuLinks: [
    // Each link navigates to a section by id.
    // Available section ids: "hero", "consciousness", "lighthouse", "waves-gallery", "waves-video", "footer"
    // { label: "THE ROOMS", target: "hero" },
    // { label: "CONSCIOUSNESS", target: "consciousness" },
    // ...
  ],
  menuSideInfo: [
    // Short info lines displayed on the right side of the fullscreen menu.
    // Typically 2-4 lines of contextual metadata in uppercase.
    // e.g. "CLAUDE MONET 1840-1926", "WATER LILIES", "EXHIBITION 2025"
  ],
}
```

### heroConfig

```typescript
export const heroConfig: HeroConfig = {
  mainTitle: "",     // Large title at bottom center of hero. Uses serif font at clamp(3rem, 7vw, 6rem).
                     // CONSTRAINT: Keep under 30 characters. Longer titles overflow on mobile.
  rooms: [
    // CONSTRAINT: Provide exactly 4 rooms. The navigation arrows cycle through them.
    // Each room MUST have exactly 3 images (back, left, right walls).
    {
      name: "",                     // Room subtitle shown below main title
      className: "room--waves",      // CSS class for wall color. Use one of:
                                     //   "room--waves"      → deep red (#8b2525)
                                     //   "room--monk"       → warm beige (#d5d0c8)
                                     //   "room--lighthouse" → navy blue (#1a3a5c)
                                     //   "room--orlando"    → gold (#c8a82e)
      theme: "dark",                 // "dark" = white text overlay, "light" = dark text overlay
                                     // Use "dark" for room--waves and room--lighthouse
                                     // Use "light" for room--monk and room--orlando
      images: {
        back: ["images/rooms/room1-back.jpg"],    // Back wall (1 image)
        left: ["images/rooms/room1-left.jpg"],    // Left wall (1 image)
        right: ["images/rooms/room1-right.jpg"],  // Right wall (1 image)
      },
    },
    // ... 3 more rooms
  ],
  metaLines: [
    // 2-4 short lines of metadata shown below the subtitle.
    // Each line is separated by a line break.
    // e.g. "A Virginia Woolf Exhibition", "Consciousness · Memory · Perception", "London — Sussex — Cornwall"
  ],
}
```

### particleConfig

```typescript
export const particleConfig: ParticleConfig = {
  sectionLabel: "",  // Section number + label (e.g. "02 / CONSCIOUSNESS"). Uppercase, short.
  title: "",         // Section heading in serif font. clamp(2rem, 5vw, 4rem).
                     // CONSTRAINT: Keep under 50 characters for best layout.
  paragraphs: [
    // Array of paragraph strings. HTML is supported (<em>, <strong>).
    // Displayed in sans-serif 16px with 1.7 line-height.
    // Recommended: 3-4 paragraphs of 3-5 sentences each.
    // e.g. "In <em>Mrs Dalloway</em>, Virginia Woolf dissolved linear time..."
  ],
  quote: "",         // A short quote in serif italic. clamp(1.5rem, 3vw, 2.5rem).
                     // CONSTRAINT: Keep under 80 characters. Displayed with left orange border.
}
```

### lighthouseVideoConfig

```typescript
export const lighthouseVideoConfig: LighthouseVideoConfig = {
  sectionLabel: "",  // Label inside the glass panel (e.g. "TO THE LIGHTHOUSE"). Uppercase.
  dataPoints: [
    // Array of short data point strings displayed in monospace font.
    // Recommended: 3 items. Each should be under 40 characters.
    // e.g. "PART I: THE WINDOW · 1910"
  ],
  description: "",   // Italic quote or description below the divider line.
                     // CONSTRAINT: Keep under 100 characters. Centered text.
  videoPath: "",     // Path to video file, e.g. "videos/lighthouse.mp4"
}
```

### wavesVideoConfig

```typescript
export const wavesVideoConfig: WavesVideoConfig = {
  sectionLabel: "",  // Section label above the video (e.g. "05 / THE WAVES"). Uppercase.
  title: "",         // Large overlay text on the video. Serif font, clamp(2rem, 5vw, 4.5rem).
                     // CONSTRAINT: Keep under 40 characters. Displayed with text shadow.
  ctaText: "",       // Call-to-action text with arrow (e.g. "ENTER THE EXHIBITION"). Uppercase.
  videoPath: "",     // Path to video file, e.g. "videos/waves.mp4"
}
```

### galleryConfig

```typescript
export const galleryConfig: GalleryConfig = {
  sectionLabel: "",      // Section label (e.g. "04 / ARCHIVE"). Uppercase.
  sectionTitle: "",      // Section title in large serif font. clamp(2.5rem, 6vw, 5rem).
  items: [
    // CONSTRAINT: Provide exactly 9 items for a perfect 3x3 grid.
    // Providing fewer or more will break the visual layout.
    {
      src: "",           // Image path, e.g. "images/gallery/item1.jpg"
      caption: "",       // Short caption below the image. Keep under 40 characters.
      description: "",   // Long description shown in lightbox. 3-6 sentences recommended.
    },
    // ... 8 more items
  ],
  lightboxCloseHint: "", // Hint text at bottom of lightbox (e.g. "Press Esc or click outside to close")
}
```

### footerConfig

```typescript
export const footerConfig: FooterConfig = {
  linkColumns: [
    // CONSTRAINT: Provide exactly 2 columns for the 2-column grid layout.
    {
      heading: "",       // Column heading in uppercase (e.g. "THE ROOMS")
      links: [],         // Array of link text strings (3-5 items recommended)
    },
    {
      heading: "",       // Second column heading
      links: [],         // Array of link text strings
    },
  ],
  tickerWords: [
    // Words that appear in the terminal ticker animation.
    // CONSTRAINT: Use uppercase words, 3-10 characters each. Provide 5-15 words.
    // The ticker randomly selects words and animates them with a sweep effect.
    // e.g. "LIGHTHOUSE", "MEMORY", "TIME", "WAVES"
  ],
  copyright: "",         // Copyright line (e.g. "© 2025 Exhibition Name")
}
```

## Required Images

If the required image assets do not already exist, write image-generation prompts based on the user's request and this template's visual style, call the `generate_image` tool, save the generated files into `public/images/`, and then reference those final file paths in `src/config.ts`.

### Room Images (12 total)

Place in `public/images/rooms/`. Each room needs 3 images:

| Image | Recommended Size | Aspect Ratio | Notes |
|-------|-----------------|--------------|-------|
| Room 1 back wall | 1200x800 | 3:2 landscape | Displayed on the far wall |
| Room 1 left wall | 1200x800 | 3:2 landscape | Displayed on the left wall |
| Room 1 right wall | 1200x800 | 3:2 landscape | Displayed on the right wall |
| Room 2 back wall | 1200x800 | 3:2 landscape | |
| Room 2 left wall | 1200x800 | 3:2 landscape | |
| Room 2 right wall | 1200x800 | 3:2 landscape | |
| Room 3 back wall | 1200x800 | 3:2 landscape | |
| Room 3 left wall | 1200x800 | 3:2 landscape | |
| Room 3 right wall | 1200x800 | 3:2 landscape | |
| Room 4 back wall | 1200x800 | 3:2 landscape | |
| Room 4 left wall | 1200x800 | 3:2 landscape | |
| Room 4 right wall | 1200x800 | 3:2 landscape | |

Images are rendered inside 3D CSS perspective walls. They display at max 40% width and 60% height of the wall. High-quality photos, artwork, or portraits work best.

### Gallery Images (9 total)

Place in `public/images/gallery/`:

| Image | Recommended Size | Aspect Ratio | Notes |
|-------|-----------------|--------------|-------|
| Gallery image 1-9 | 800x1067 | 3:4 portrait | Displayed in 3x3 grid with hover zoom |

Gallery images are shown in portrait orientation. The lightbox shows them at full size with a description panel on the right.

## Required Videos (2 total)

If the required video assets do not already exist, write video-generation prompts based on the user's request and this template's visual style, call the `generate_video` tool, save the generated files into `public/videos/`, and then reference those final file paths in `src/config.ts`.

Place in `public/videos/`:

| Video | Recommended Size | Format | Notes |
|-------|-----------------|--------|-------|
| Lighthouse video | 1920x1080 | MP4 (H.264) | Fullscreen background, autoplays muted and looped |
| Waves video | 1920x1080 | MP4 (H.264) | 16:9 widescreen, autoplays muted and looped |

Videos should be atmospheric/ambient footage. Keep file sizes reasonable (10-30 MB each) for web performance.

## Layout Constraints Summary

- **brandName**: 3-8 uppercase characters
- **mainTitle**: Under 30 characters
- **rooms**: Exactly 4 rooms, each with exactly 3 images (back/left/right)
- **particleConfig.title**: Under 50 characters
- **particleConfig.quote**: Under 80 characters
- **particleConfig.paragraphs**: 3-4 paragraphs, supports `<em>` HTML tags
- **lighthouseVideoConfig.dataPoints**: 3 items, each under 40 characters
- **lighthouseVideoConfig.description**: Under 100 characters
- **wavesVideoConfig.title**: Under 40 characters
- **galleryConfig.items**: Exactly 9 items for 3x3 grid
- **galleryConfig.items[].caption**: Under 40 characters
- **footerConfig.linkColumns**: Exactly 2 columns
- **footerConfig.tickerWords**: 5-15 uppercase words, 3-10 characters each

## Design Notes

**Typography system:**
- `--font-serif`: Instrument Serif (Google Fonts) — titles, headings, menu links
- `--font-sans`: Inter 300/400 (Google Fonts) — body text, labels, buttons
- `--font-mono`: Source Code Pro 400 (Google Fonts) — data points, ticker

**Color palette:**
- Background: `#0a0a0b` (void black)
- Light section: `#f5f4f0` (alabaster, used for particle section)
- Accent: `#f25b29` (orange, used for borders, hover states, CTA text)
- Metal text tones: `#e6e7e9`, `#b0b2b5`, `#7a7c7f`
- Room wall colors are defined in CSS (index.css), not in config:
  - `.room--waves`: `#8b2525` (deep red)
  - `.room--monk`: `#d5d0c8` (warm beige)
  - `.room--lighthouse`: `#1a3a5c` (navy blue)
  - `.room--orlando`: `#c8a82e` (gold)

**Key animations (preserved in code, do not modify):**
- CSS 3D perspective room with mouse-tilt interaction
- Room transition: Z-retreat, lateral slide, re-enter new room
- Three.js particle torus-knot with simplex noise vertex shader
- GSAP ScrollTrigger fade-in on all sections
- Terminal ticker: GSAP tween sweep-reveal and sweep-erase cycle
- Logo letter 3D rotation on hover
- Fullscreen menu GSAP timeline
