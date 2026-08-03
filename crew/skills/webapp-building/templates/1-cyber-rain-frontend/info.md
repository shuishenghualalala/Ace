# Digital Rain Frontend Template

A dark, cinematic landing page template featuring a canvas-based amber digital rain animation (falling mathematical symbols that splash into a simulated water surface). Designed for AI agent / memory infrastructure / deep-tech companies.

The layout includes a full-viewport hero with the digital rain background, a capabilities section with hover-reveal images, a cinematic video section, a research project grid, capability detail sub-pages with article-style content, and a footer.

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
  language: "",       // BCP-47 language code, e.g. "en", "zh-CN", "ja"
  brandName: "",      // Brand name shown in nav bar and detail page header
                      // Example: "Memory Lab."
                      // Constraint: max ~20 characters (displayed in nav at 18px mono)
};
```

### `navigationConfig`

```typescript
export const navigationConfig: NavigationConfig = {
  links: [],          // Array of { label, href } for the main nav
                      // href should be anchor IDs: "#curriculum", "#cinematic", "#alumni", "#footer"
                      // Recommended: 4-6 links
                      // Example: { label: "Capabilities", href: "#curriculum" }
  ctaText: "",        // Right-side call-to-action link text
                      // Example: "Get access"
                      // Constraint: max ~15 characters
};
```

### `heroConfig`

```typescript
export const heroConfig: HeroConfig = {
  title: "",          // Main hero heading (same as or similar to brandName)
                      // Displayed at clamp(48px, 6vw, 96px) in GeistMono
                      // Constraint: max ~20 characters for single-line display
  subtitleLine1: "",  // First subtitle paragraph below the title
                      // Constraint: max ~90 characters to stay within title width
  subtitleLine2: "",  // Second subtitle paragraph
                      // Constraint: max ~60 characters
  ctaText: "",        // Button text in the liquid-glass button
                      // Example: "Explore capabilities"
                      // Constraint: max ~25 characters
};
```

### `capabilitiesConfig`

```typescript
export const capabilitiesConfig: CapabilitiesConfig = {
  sectionLabel: "",   // Uppercase label above divider, e.g. "Capabilities"
                      // Constraint: max ~20 characters (displayed at 12px uppercase)
  items: [],          // Array of CapabilityItem objects (recommended: exactly 4)
                      // Each item:
                      // {
                      //   title: "",        // Large display title (EB Garamond at clamp 40-86px)
                      //                     // Constraint: max ~25 characters for single line
                      //   slug: "",         // URL-safe slug, e.g. "episodic-recall"
                      //                     // Must match a key in capabilityDetailConfig.capabilities
                      //   description: "",  // 1-2 sentence description shown alongside title
                      //                     // Constraint: 100-200 characters for balanced layout
                      //   image: "",        // Path to hover-reveal image
                      //                     // Example: "images/capability-1.jpg"
                      // }
};
```

### `capabilityDetailConfig`

```typescript
export const capabilityDetailConfig: CapabilityDetailConfig = {
  sectionLabel: "",   // Label above the detail page title, e.g. "Capability"
  backLinkText: "",   // Text for the back-to-home link, e.g. "Back to home"
  prevLabel: "",      // Label above previous capability name, e.g. "Previous"
  nextLabel: "",      // Label above next capability name, e.g. "Next"
  notFoundText: "",   // Message when slug not found, e.g. "Capability not found."
  capabilities: {},   // Record<string, CapabilityDetailData> keyed by slug
                      // Each slug must match a slug in capabilitiesConfig.items
                      // Each value:
                      // {
                      //   title: "",      // Page heading (EB Garamond at clamp 40-72px)
                      //   subtitle: "",   // One-line summary below heading
                      //                   // Constraint: max ~60 characters
                      //   paragraphs: []  // Array of 2-4 paragraphs of body text
                      //                   // Each paragraph: 150-400 characters recommended
                      // }
};
```

### `architectureConfig`

```typescript
export const architectureConfig: ArchitectureConfig = {
  sectionLabel: "",   // Uppercase label, e.g. "Architecture"
  videoPath: "",      // Path to the looping video file
                      // Example: "/videos/cinematic-vision.mp4"
  title: "",          // Large heading below video (EB Garamond at clamp 32-64px)
                      // Constraint: max ~55 characters for balanced 50/50 layout
  description: "",    // Paragraph beside the title
                      // Constraint: 150-300 characters recommended
};
```

### `researchConfig`

```typescript
export const researchConfig: ResearchConfig = {
  sectionLabel: "",   // Uppercase label, e.g. "Research"
  projects: [],       // Array of ResearchProject objects
                      // Displayed in a 4-column grid; use multiples of 4 (8 or 12 recommended)
                      // Each project:
                      // {
                      //   title: "",       // Project name (EB Garamond at 18px)
                      //                    // Constraint: max ~30 characters
                      //   year: "",        // Year string, e.g. "2026"
                      //   discipline: "",  // Category label (Inter at 12px)
                      //                    // Constraint: max ~25 characters
                      //   image: "",       // Square image for the card
                      //                    // Example: "images/research-1.jpg"
                      // }
};
```

### `footerConfig`

```typescript
export const footerConfig: FooterConfig = {
  heading: "",        // Large footer heading (EB Garamond at clamp 40-80px)
                      // Constraint: max ~35 characters
  columns: [],        // Array of FooterLinkColumn (recommended: 2 columns)
                      // Each column:
                      // {
                      //   title: "",   // Column heading (uppercase, 12px)
                      //   links: []    // Array of link label strings (3-5 per column)
                      // }
  copyright: "",      // Copyright line at bottom
                      // Example: "\u00A9 2026 Brand Name. All rights reserved."
  bottomLinks: [],    // Array of { label, href } for bottom-row links
                      // Example: { label: "Privacy Policy", href: "#" }
};
```

## Required Images

Place all images in `public/images/`.

If the required image assets do not already exist, write image-generation prompts based on the user's request and this template's visual style, call the `generate_image` tool, save the generated files into `public/images/`, and then reference those final file paths in `src/config.ts`.

### Capability Section (4 images)
- `images/capability-1.jpg` - Hover-reveal image for capability 1 (aspect ratio flexible, min 400x300)
- `images/capability-2.jpg` - Hover-reveal image for capability 2
- `images/capability-3.jpg` - Hover-reveal image for capability 3
- `images/capability-4.jpg` - Hover-reveal image for capability 4

### Research Grid (3-4 unique images, reused across 12 cards)
- `images/research-1.jpg` - Square format recommended (1:1 aspect ratio, min 400x400)
- `images/research-2.jpg` - Square format
- `images/research-3.jpg` - Square format
- `images/research-4.jpg` - Square format (optional)

Images are displayed with grayscale filter (colorizes on hover in research grid), and grayscale(30%) in capabilities. Moody, abstract, or architectural photography works best with the dark amber theme.

## Required Video

Place video in `public/videos/`.

If the required video asset does not already exist, write a video-generation prompt based on the user's request and this template's visual style, call the `generate_video` tool, save the generated file into `public/videos/`, and then reference that final file path in `src/config.ts`.

- `videos/cinematic-vision.mp4` - Looping background video for the architecture section
  - Aspect ratio: 21:9 (ultrawide)
  - Autoplays muted, loops continuously
  - Recommended: 10-30 seconds, abstract or architectural footage
  - File size: keep under 15MB for fast loading

## Layout Constraints

These limits exist to keep text inside the designed typographic scale. Violating them causes truncation, wrap-breakage, or overflow in the fixed-width columns.

- **`siteConfig.brandName`**: max ~20 characters — shown in nav at 18px mono
- **`navigationConfig.links`**: 4–6 entries; each `label` ≤ ~16 characters — the nav bar is horizontal and will wrap past that
- **`navigationConfig.ctaText`**: max ~15 characters
- **`heroConfig.title`**: max ~20 characters, single line — rendered at clamp(48px, 6vw, 96px)
- **`heroConfig.subtitleLine1`**: max ~90 characters — must fit within the title's text column
- **`heroConfig.subtitleLine2`**: max ~60 characters
- **`heroConfig.ctaText`**: max ~25 characters — inside the liquid-glass button
- **`capabilitiesConfig.sectionLabel`**: max ~20 characters, uppercase at 12px
- **`capabilitiesConfig.items`**: exactly 4 entries — the hover-reveal image grid is built for 4 cards; fewer leaves gaps, more causes layout drift
- **`capabilitiesConfig.items[].title`**: max ~25 characters, single line — EB Garamond at clamp(40px, 86px)
- **`capabilitiesConfig.items[].description`**: 100–200 characters
- **`capabilitiesConfig.items[].slug`**: URL-safe slug; must match a key in `capabilityDetailConfig.capabilities`
- **`capabilityDetailConfig.capabilities[slug].subtitle`**: max ~60 characters
- **`capabilityDetailConfig.capabilities[slug].paragraphs`**: 2–4 paragraphs, 150–400 characters each, rendered at max-width 860px
- **`architectureConfig.title`**: max ~55 characters — sits in a 50/50 layout against the description paragraph
- **`architectureConfig.description`**: 150–300 characters
- **`researchConfig.projects`**: use multiples of 4 (recommended 8 or 12) — the research section is a 4-column desktop grid
- **`researchConfig.projects[].title`**: max ~30 characters — EB Garamond at 18px
- **`researchConfig.projects[].discipline`**: max ~25 characters — Inter at 12px
- **`footerConfig.heading`**: max ~35 characters — rendered at clamp(40px, 80px) EB Garamond
- **`footerConfig.columns`**: recommended 2 columns; each column 3–5 links

## Design Notes

### Fonts
- **GeistMono** (loaded from node_modules via @font-face in index.css) - Brand name, hero title, nav links, button text
- **EB Garamond** (Google Fonts, serif) - Section headings, capability titles, footer heading, research card titles
- **Inter** (Google Fonts, sans-serif) - Body text, descriptions, section labels
- **Fira Code** (Google Fonts, monospace) - Year labels in research grid

### Color Palette
- Background: `#0a0a0a` (near-black)
- Primary text: `#ffffff` (white)
- Secondary text: `#dadada` (light gray)
- Accent: `rgba(200, 170, 130)` / amber tones (hover states, waterline, digital rain)
- Primary HSL: `34 68% 75%` (warm amber)

### Key Animations
- **Digital rain** (AmberCascades.tsx): Canvas-based falling math symbols with water surface simulation, ripple physics, and interactive click/touch response
- **Liquid glass button**: SVG displacement map filter creating a glass-lens refraction effect
- **GSAP scroll reveals**: Curriculum items fade in with staggered delays, cinematic text slides up, research grid items animate on intersection
- **Hover effects**: Capability titles change to amber, description text cross-fades to image; research images transition from grayscale to color

### Layout Notes
- The hero section is full-viewport height (100vh)
- The digital rain canvas sits behind the hero and is reused on capability detail pages (at 40% opacity)
- Navigation becomes semi-transparent with blur on scroll
- The research grid uses a 4-column layout on desktop, 2-column on mobile
- Capability detail pages are article-style with max-width 860px
