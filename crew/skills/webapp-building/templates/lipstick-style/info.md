# Lipstick Product Landing Page Template

A bold, edgy product landing page with Swiss-inspired grid design, GSAP scroll animations, and dramatic typography. Perfect for beauty/cosmetics brands.

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Features

- Full-screen grid collage hero with animated title reveal
- 7 sections: Hero, Manifesto, Product Spotlight, Texture, Shade Range, Final Statement, Contact
- Pinned scroll sections with GSAP ScrollTrigger
- Film grain overlay texture
- Scroll snap navigation
- Fully responsive

## Tech Stack

- **React 19** + **TypeScript** + **Vite 7**
- **Tailwind CSS 3**
- **GSAP 3** with ScrollTrigger
- **shadcn/ui** components

## Quick Start

1. Install dependencies:
   ```bash
   npm install
   ```
2. Edit `src/config.ts` with your content
3. Add images to `public/images/`
4. Build: `npm run build`

## Configuration

All content is in **`src/config.ts`**. Do NOT modify component files.

### Required Config Objects

```typescript
// 1. Navigation
navigationConfig: {
  logo: string,                  // Brand name
  links: Array<{label, href}>    // Nav links
}

// 2. Hero Section
heroConfig: {
  heroImage: string,             // Path: "images/hero_face.jpg"
  titleText: string,             // 10-letter title (e.g. "DARETOWEAR")
  subtitleLabel: string,         // Top label
  ctaText: string                // Button text
}

// 3. Manifesto Section
manifestoConfig: {
  image: string,                 // Path: "images/manifesto_face.jpg"
  phrases: string[]              // 9 short phrases (see Tile Text Rules below)
}

// 4. Product Spotlight Section
productSpotlightConfig: {
  productImage: string,          // Path: "images/product_lipstick.jpg"
  portraitImage: string,         // Path: "images/spotlight_face.jpg"
  titlePhrases: string[],        // 6 phrases (see Tile Text Rules below)
  ctaText: string,
  price: string
}

// 5. Texture Section
textureConfig: {
  portraitImage: string,         // Path: "images/texture_face_side.jpg"
  macroImage: string,            // Path: "images/texture_lips.jpg"
  titlePhrases: string[],        // 6 phrases (see Tile Text Rules below)
  subtitle: string
}

// 6. Shade Range Section
shadeRangeConfig: {
  heading: string[],             // Heading lines
  headingAccent: string,         // Accent line (pink)
  shades: Array<{name, image}>, // Product grid
  price: string,
  ctaText: string
}

// 7. Final Statement Section
finalStatementConfig: {
  image1: string,                // Path: "images/closing_face_1.jpg"
  image2: string,                // Path: "images/closing_face_2.jpg"
  phrases: string[],             // 7 phrases (see Tile Text Rules below)
  subtitle: string
}

// 8. Contact Section
contactConfig: {
  leftLinks: string[],           // Left column links
  formHeading: string[],         // Form title lines
  formHeadingAccent: string,     // Accent line (pink)
  formDescription: string,
  emailPlaceholder: string,
  subscribeButtonText: string,
  socialLinks: Array<{label, href}>,
  copyright: string,
  tagline: string
}

// 9. Site Metadata
siteConfig: {
  title: string,                 // Browser tab title
  description: string,
  language: string
}
```

## Tile Text Rules (IMPORTANT)

Each phrase in `manifestoConfig.phrases`, `productSpotlightConfig.titlePhrases`, `textureConfig.titlePhrases`, and `finalStatementConfig.phrases` is displayed inside a small grid tile. The tile has very limited space, so you MUST follow these rules:

- **English: strictly 1 word per tile.** e.g. `["BOLD", "FEARLESS", "FIERCE", "FREE"]`. Never put 2+ words in one tile — it will overflow and be invisible.
- **CJK/Asian languages: max 4 characters per tile.** e.g. `["自然之美", "自信绽放", "丝绒柔雾"]`.
- **Do NOT increase the font size** in CSS. The default tile font size is already calibrated for the grid cell size. Larger fonts will cause text to overflow and become hidden.

### Good Examples

English:
```
phrases: ["BOLD", "FEARLESS", "FIERCE", "FREE"]
titlePhrases: ["PURE", "COLOR", "PURE", "POWER"]
```

CJK/Asian languages:
```
phrases: ["自然之美", "自信绽放", "优雅永恒", "花颜悦色"]
titlePhrases: ["丝绒柔雾", "持久显色", "滋润保湿", "轻盈质地"]
```

### Bad Examples (will break layout)
```
phrases: ["BE BOLD AND FEARLESS", "EMBRACE YOUR INNER BEAUTY"]  // too many words
titlePhrases: ["PURE LUXURIOUS COLOR"]  // multiple words crammed in one tile
```

## Required Images (14 total)

Add to `public/images/`:

### Hero Section (1)
- **hero_face.jpg** - Portrait, 1920x1080+, sliced into 6 segments

### Manifesto Section (1)
- **manifesto_face.jpg** - Portrait, 800x1200, sliced into 5 segments

### Product Spotlight (2)
- **product_lipstick.jpg** - Product photo, 800x1200, sliced into 4 segments
- **spotlight_face.jpg** - Portrait, 800x1200, sliced into 7 segments

### Texture Section (2)
- **texture_face_side.jpg** - Side profile, 800x1200, sliced into 4 segments
- **texture_lips.jpg** - Macro texture, 1200x800, sliced into 7 segments

### Shade Range (6)
- **shade_swatch_1.jpg** to **shade_swatch_6.jpg** - Swatches, 600x600 each

### Final Statement (2)
- **closing_face_1.jpg** - Portrait, 800x1200, sliced into 4 segments
- **closing_face_2.jpg** - Portrait, 800x1200, sliced into 4 segments

## Design

### Colors
- **Accent:** Pink `#ff73c3`
- **Background:** Light grey `#f5f3ef`
- **Text:** Black `#0a0a0a`
- **Contact BG:** Black `#000000`

### Fonts
- **Display:** Montserrat (100-900) - Headings
- **Body:** Open Sans (300-800) - Body text
- **Mono:** Space Mono (400) - Labels/prices

### Animations
- **Hero:** Load sequence → Scroll exit scatter
- **Pinned Sections (Manifesto, Product, Texture, Final):** Entrance (0-30%) → Hold (30-70%) → Exit (70-100%)
- **Shade Range:** Scroll-triggered card rotation
- **Contact:** Scroll-triggered fade-in
- **Global:** Scroll snap for smooth section navigation

## Build

```bash
npm run build
```

Output: `dist/` folder (ready for Vercel/Netlify/static hosting)

## Project Structure

```
├── src/
│   ├── config.ts              ← Edit for all content
│   ├── App.tsx                ← All sections + scroll snap
│   ├── App.css                ← Component styles
│   ├── index.css              ← Global styles + fonts
│   ├── components/ui/         ← shadcn/ui library
│   └── hooks/lib/             ← Utilities
├── public/images/             ← Add 14 images here
├── README.md                  ← Full documentation
└── package.json
```

## Notes

- **Only edit `src/config.ts`** - components read from config
- Components auto-hide when config is empty
- Keep phrase arrays concise (1-2 words each)
- Hero `titleText` should be 10 characters
- All GSAP animations preserved in components
- Update `<title>` in `index.html`

## Example Config

```typescript
export const heroConfig: HeroConfig = {
  heroImage: "images/hero_face.jpg",
  titleText: "DARETOWEAR",
  subtitleLabel: "New Season Drop",
  ctaText: "Shop the Drop"
};

export const shadeRangeConfig: ShadeRangeConfig = {
  heading: ["CHOOSE", "YOUR"],
  headingAccent: "TRUTH",
  shades: [
    { name: "Stage Red", image: "images/shade_swatch_1.jpg" },
    { name: "Afterhours Plum", image: "images/shade_swatch_2.jpg" },
    // ... 4 more
  ],
  price: "$28",
  ctaText: "Add to Bag"
};
```
