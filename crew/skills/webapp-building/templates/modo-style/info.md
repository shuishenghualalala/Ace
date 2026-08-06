# Modo Template

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Features
- Premium single-page exhibition/gallery website template
- Hero section with split-text layout and center image with parallax
- About section with 3-column parallax gallery and statistics
- Exhibitions section with 2x2 staggered scroll reveal grid
- Collections section with full-screen stacking card scroll effect
- Testimonials section with centered quote and author info
- Visit/contact section with icon-based info cards grid
- Footer with infinite marquee, social links, quick links, contact info
- Custom cursor with hover states (auto-hidden on touch devices)
- Lenis smooth scroll integration
- GSAP ScrollTrigger animations throughout
- Scroll-based background color transitions between sections
- Reduced motion and high contrast accessibility support
- Each section returns null when config is empty (selective display)

## Tech Stack
- React 19 + TypeScript
- Vite
- Tailwind CSS 3
- GSAP with ScrollTrigger
- Lenis smooth scroll
- Lucide React icons
- Radix UI + shadcn/ui components

## Quick Start
```bash
npm install
npm run dev
```

## Configuration

All content is configured in `src/config.ts`. The file exports the following objects:

### siteConfig
```ts
{
  language: "",     // HTML lang attribute ("en", "zh", "ja", etc.)
  title: "",        // Page title
  description: "",  // Meta description
}
```

### heroConfig
```ts
{
  brandLeft: "",       // Left headline text
  brandRight: "",      // Right headline text
  tagline: "",         // Subtitle below left headline
  badge: "",           // Label above center image (e.g., location)
  since: "",           // Year text below right headline
  email: "",           // Contact email
  heroImage: "",       // Center image path — MUST be transparent-background PNG (e.g., "/images/hero.png")
  heroImageAlt: "",    // Image alt text
  scrollText: "",      // Bottom-left text
  copyrightText: "",   // Bottom-right text
  navLinks: [],        // Array of { label: string, href: string }
  socialLinks: [],     // Array of { label: string, href: string }
}
```

### aboutConfig
```ts
{
  label: "",          // Section label (e.g., "Established 1892")
  headline: "",       // Main heading
  description: "",    // Body paragraph
  bottomText: "",     // Closing paragraph
  galleryImages: [],  // Array of { src: string, alt: string, label: string }
  stats: [],          // Array of { value: string, label: string }
}
```

### exhibitionsConfig
```ts
{
  label: "",         // Section label
  headline: "",      // Main heading
  ctaText: "",       // Button text
  exhibitions: [],   // Array of { id: number, title: string, image: string, date: string }
}
```

### collectionsConfig
```ts
{
  label: "",         // Section label
  headline: "",      // Main heading
  ctaText: "",       // Per-card button text
  collections: [],   // Array of { id: number, title: string, year: string, description: string, image: string }
}
```

### testimonialsConfig
```ts
{
  quote: "",          // Quote text (no quotation marks needed)
  authorName: "",     // Author name
  authorTitle: "",    // Author title/role
  authorImage: "",    // Author portrait path
}
```

### visitConfig
```ts
{
  label: "",         // Section label
  headline: "",      // Main heading (supports HTML, e.g., "Line 1<br />Line 2")
  description: "",   // Body text
  ctaText: "",       // CTA button text
  infoCards: [],     // Array of { icon: string, title: string, content: string }
                     // icon: "MapPin" | "Clock" | "Calendar" | "Ticket"
                     // content supports HTML for line breaks
}
```

### footerConfig
```ts
{
  marqueeText: "",       // Scrolling marquee text
  brandName: "",         // Footer brand name
  brandDescription: "",  // Short brand description
  socialLinks: [],       // Array of { label: string, href: string }
                         // label maps to icon: "Instagram" | "Twitter" | "Facebook" | "Linkedin" | "Youtube" | "Globe"
  quickLinks: [],        // Array of { label: string, href: string }
  quickLinksTitle: "",   // Column title
  contactTitle: "",      // Column title
  contactItems: [],      // Array of strings (supports HTML)
  bottomLinks: [],       // Array of { label: string, href: string }
}
```

## Required Images

Place in `public/images/`:
- Hero center image (**MUST be a transparent-background PNG**; a radial gradient mask is applied in code, so non-transparent backgrounds will look broken)
- About gallery images (6 recommended, distributed across 3 parallax columns)
- Exhibition card images (4 images, 4:3 aspect ratio)
- Collection card images (4 images, 16:10 aspect ratio)
- Testimonial author portrait (square crop, displayed as circle)

## Design
- Color palette: Grey (#8c8c91), Black (#050505), Light (#f0f0f0), Charcoal (#1a1a1a)
- Typography: Inter font (300-700 weights)
- Custom cursor with mix-blend-mode: difference
- Sections have scroll-triggered background color transitions
- Gallery uses 3-column parallax with different scroll speeds
- Collections use a stacking/pinning card effect
- All animations use GSAP with ScrollTrigger

## Notes
- Each section has a null check: returns null when its primary config field is empty
- Null checks: Hero (brandLeft && brandRight), About (headline), Exhibitions (headline && exhibitions.length), Collections (headline && collections.length), Testimonials (quote), Visit (headline && infoCards.length), Footer (brandName)
- All GSAP animations, Lenis smooth scroll, custom cursor, and ScrollTrigger logic are preserved unchanged
- The Visit section headline and infoCards content fields support HTML strings
- Footer contactItems support HTML strings for multi-line addresses
