# Kaleo Template

An editorial, earth-toned single-page website template with premium scroll-driven animations. Features warm organic colors (sand, cream, terracotta, charcoal), serif/sans-serif typography pairing (Cormorant Garamond + Inter), and cinematic GSAP-powered scroll effects.

## Language

If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content

The actual content of the website should match the user's query.

## Features

- Full-viewport hero with Ken Burns background animation and parallax scroll
- Scroll-triggered narrative text reveal with spinning star decorations
- Pinned scroll-driven card stack gallery with stacking/parallax effect
- Cinematic breath section with scale-up scroll animation and dynamic rounded corners
- Alternating zigzag image/text grid with internal image parallax
- Dark footer with magnetic button hover effect, parallax background, and structured contact grid
- Lenis smooth scrolling integrated with GSAP ScrollTrigger
- Each section has a null check and returns null when config is empty

## Tech Stack

- React 19 + TypeScript
- Vite
- Tailwind CSS 3 with custom earth-toned theme
- GSAP with ScrollTrigger plugin
- @studio-freight/lenis for smooth scrolling
- Lucide React for icons
- Google Fonts: Cormorant Garamond (display/headings) + Inter (body text)

## Quick Start

```bash
npm install
npm run dev
```

## Configuration

All content is centralized in `src/config.ts`. Each section imports its own config object and renders content from it. When config values are empty, the section returns null (not rendered).

### siteConfig (SiteConfig)
```ts
{
  language: "",        // HTML lang attribute, e.g., "en", "zh", "ja"
  siteName: "",        // Site name for display
  siteDescription: "", // Meta description
}
```

### heroConfig (HeroConfig)
```ts
{
  backgroundImage: "", // Path to hero background image, e.g., "/hero-bg.jpg"
  backgroundAlt: "",   // Alt text for the background image
  title: "",           // Large display title (shown as display-size text)
  subtitle: "",        // Uppercase tracking subtitle below the title
}
```

### narrativeTextConfig (NarrativeTextConfig)
```ts
{
  line1: "", // Main headline (large serif font)
  line2: "", // Subheadline (italic serif, slightly smaller)
  line3: "", // Body paragraph (sans-serif, smaller with tracking)
}
```

### breathSectionConfig (BreathSectionConfig)
```ts
{
  backgroundImage: "", // Path to cinematic background image
  backgroundAlt: "",   // Alt text for the background image
  title: "",           // Large display overlay text
  subtitle: "",        // Uppercase tracking subtitle
  description: "",     // Paragraph text below the image banner
}
```

### cardStackConfig (CardStackConfig)
```ts
{
  sectionTitle: "",    // Section heading text
  sectionSubtitle: "", // Small uppercase subheading
  cards: [             // Array of cards (recommended 3)
    {
      id: 1,             // Unique numeric ID
      image: "",         // Path to card image, e.g., "/card-1.jpg"
      title: "",         // Card title text
      description: "",   // Card description text
      rotation: -2,      // Tilt angle in degrees (negative = left tilt)
    },
  ],
}
```

### zigZagGridConfig (ZigZagGridConfig)
```ts
{
  sectionLabel: "",  // Small uppercase label above the title
  sectionTitle: "",  // Section heading text
  items: [           // Array of grid items (recommended 2-4)
    {
      id: "",          // Unique string ID, e.g., "land"
      title: "",       // Item heading
      subtitle: "",    // Small uppercase label above the heading
      description: "", // Item description paragraph
      image: "",       // Path to item image, e.g., "/grid-1.jpg"
      imageAlt: "",    // Alt text for the image
      reverse: false,  // true = image on right, text on left
    },
  ],
}
```

### footerConfig (FooterConfig)
```ts
{
  heading: "",       // Footer CTA heading
  description: "",   // Footer CTA description paragraph
  ctaText: "",       // CTA button label text
  contact: [         // Array of contact items
    {
      type: "email",   // "email" or "phone"
      label: "",       // Display label, e.g., "hello@example.com"
      value: "",       // Actual value
      href: "",        // Link href, e.g., "mailto:hello@example.com"
    },
  ],
  locationLabel: "", // Label for the address section, e.g., "Location"
  address: [],       // Array of address lines, e.g., ["123 Main St", "City, ST 12345"]
  socialLabel: "",   // Label for social links section, e.g., "Follow"
  socials: [         // Array of social link objects
    {
      platform: "",    // Platform name: "instagram" or "facebook"
      href: "",        // Social profile URL
    },
  ],
  logoText: "",      // Large footer logo text (displayed as SVG)
  copyright: "",     // Copyright line, e.g., "2025 Company. All rights reserved."
  links: [           // Array of footer bottom links
    {
      label: "",       // Link text, e.g., "Privacy Policy"
      href: "",        // Link URL
    },
  ],
}
```

## Required Images

Place all images in the `public/` directory.

| Image | Section | Recommended Aspect | Notes |
|---|---|---|---|
| Hero background | Hero | Landscape, full-screen | High-resolution, atmospheric |
| Breath background | Breath Section | 16:9 or 21:9 | Cinematic wide shot |
| Card images (x3) | Card Stack | 4:3 | One per card |
| Grid images (x2-4) | ZigZag Grid | 4:3 | One per grid item |
| Footer background | Footer | Any landscape | Atmospheric, shown at 30% opacity (optional) |

## Design

- **Color palette**: Sand (#EAE4D9), Cream (#F3F0EB), Terracotta (#8C7B6B), Charcoal (#1C1C1C), Earth (#2A2A2A)
- **Typography**: Cormorant Garamond (serif, headings/display) + Inter (sans-serif, body)
- **Layout**: Single-page vertical editorial flow with max-width containers
- **Animations**: All scroll-driven via GSAP ScrollTrigger -- Ken Burns on hero image, parallax on multiple layers, scroll-pinned card stacking, scale-up breath section with animated border radius, staggered text reveals, magnetic button hover effect
- **Smooth scrolling**: Lenis with GSAP ticker sync

## Notes

- The template uses `siteConfig.language` to dynamically set the `<html lang>` attribute at runtime
- All animations are preserved exactly as-is -- only content strings are configurable
- Footer social icons support "instagram" and "facebook" platforms via Lucide React icons
- Card `rotation` values create a natural scattered-stack appearance (use small values like -2 to 2)
- ZigZag grid items alternate layout via the `reverse` boolean field
- The breath section dynamically rounds its corners and scales up as you scroll into view
