# Playza Template

A high-impact, immersive single-page website template designed for music artists, bands, and entertainment brands. Features a dark cyberpunk aesthetic with neon accents, 3D album cube, parallax galleries, and smooth scroll-driven animations.

## Language

If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content

The actual content of the website should match the user's query.

## Features

- Full-screen hero with animated text decode effect
- 3D rotating album cube (Three.js / React Three Fiber)
- Dual parallax image strips with scroll-driven movement
- Horizontal-scrolling gallery with pinned scroll
- Infinite marquee ticker
- Tour schedule section with venue previews
- Full-screen artist portrait with parallax overlay
- Footer with social links, contact info, newsletter signup
- Lenis smooth scrolling integrated with GSAP ScrollTrigger
- Velocity-based motion blur and letter-spacing effects
- Fully responsive design

## Tech Stack

- React 19 + TypeScript
- Vite 7
- Tailwind CSS 3.4 + custom CSS animations
- Three.js + React Three Fiber + Drei
- GSAP 3 + ScrollTrigger
- Lenis (smooth scroll)
- Lucide React (icons)
- Radix UI primitives

## Quick Start

```bash
npm install
npm run dev
```

## Configuration

All content is managed in `src/config.ts`. Each section has its own config object with TypeScript interfaces.

### SiteConfig

```ts
siteConfig: {
  title: string;       // Browser tab title
  description: string; // Site meta description
  language: string;    // Language code
}
```

### HeroConfig

```ts
heroConfig: {
  backgroundImage: string;     // Hero background image path
  brandName: string;           // Brand name in top-left logo
  decodeText: string;          // Main title with decode animation
  decodeChars: string;         // Characters for scramble effect
  subtitle: string;            // Subtitle below title
  ctaPrimary: string;          // Primary CTA button text
  ctaPrimaryTarget: string;    // Section ID to scroll to
  ctaSecondary: string;        // Secondary CTA button text
  ctaSecondaryTarget: string;  // Section ID to scroll to
  cornerLabel: string;         // Top-right corner label
  cornerDetail: string;        // Top-right corner detail
  navItems: [                  // Navigation pill buttons
    {
      label: string;
      sectionId: string;       // Target: "albums", "gallery", "tour", "contact"
      icon: "disc" | "play" | "calendar" | "music";
    }
  ];
}
```

### AlbumCubeConfig

```ts
albumCubeConfig: {
  albums: [
    {
      id: number;
      title: string;           // Album title (displayed large)
      subtitle: string;         // Subtitle text (background watermark)
      image: string;           // Album cover image path
    }
  ];
  cubeTextures: string[];      // Exactly 6 images for cube faces
                               // Order: right, left, top, bottom, front, back
  scrollHint: string;          // Bottom-right scroll hint text
}
```

### ParallaxGalleryConfig

```ts
parallaxGalleryConfig: {
  sectionLabel: string;        // Parallax section label
  sectionTitle: string;        // Parallax section title
  galleryLabel: string;        // Gallery section label
  galleryTitle: string;        // Gallery section title
  marqueeTexts: string[];      // Marquee ticker texts
  endCtaText: string;          // End-of-gallery CTA text
  parallaxImagesTop: [         // Top row (6 images recommended)
    { id: number; src: string; alt: string; }
  ];
  parallaxImagesBottom: [      // Bottom row (6 images recommended)
    { id: number; src: string; alt: string; }
  ];
  galleryImages: [             // Horizontal gallery (6 images recommended)
    { id: number; src: string; title: string; date: string; }
  ];
}
```

### TourScheduleConfig

```ts
tourScheduleConfig: {
  sectionLabel: string;
  sectionTitle: string;
  vinylImage: string;          // Spinning vinyl disc image
  buyButtonText: string;
  detailsButtonText: string;
  bottomNote: string;
  bottomCtaText: string;
  statusLabels: {
    onSale: string;
    soldOut: string;
    comingSoon: string;
    default: string;
  };
  tourDates: [
    {
      id: number;
      date: string;            // "YYYY.MM.DD"
      time: string;            // "HH:MM"
      city: string;
      venue: string;
      status: "on-sale" | "sold-out" | "coming-soon";
      image: string;           // Venue preview image
    }
  ];
}
```

### FooterConfig

```ts
footerConfig: {
  portraitImage: string;         // Full-screen portrait image
  portraitAlt: string;
  heroTitle: string;             // Large overlay title
  heroSubtitle: string;
  artistLabel: string;
  artistName: string;
  artistSubtitle: string;
  brandName: string;
  brandDescription: string;
  quickLinksTitle: string;
  quickLinks: string[];
  contactTitle: string;
  emailLabel: string;
  email: string;
  phoneLabel: string;
  phone: string;
  addressLabel: string;
  address: string;
  newsletterTitle: string;
  newsletterDescription: string;
  newsletterButtonText: string;
  subscribeAlertMessage: string;
  copyrightText: string;
  bottomLinks: string[];
  socialLinks: [
    {
      icon: "instagram" | "twitter" | "youtube" | "music";
      label: string;
      href: string;
    }
  ];
  galleryImages: [
    { id: number; src: string; }
  ];
}
```

## Required Images

Place in `public/` directory:

| Image | Size | Usage |
|-------|------|-------|
| Hero background | 1920x1080px | Hero section background |
| Album covers (4) | 800x800px | Cube faces + album data |
| Extra cube texture | 800x800px | Additional cube face |
| Concert photos (6+) | 800x500px | Parallax strips + gallery |
| Venue photos (4) | 800x600px | Tour venue previews |
| Artist portrait | 800x1200px | Footer portrait section |
| Vinyl disc | 400x400px PNG | Tour section spinner |

## Design

### Colors
- Void Black: `#050508` (primary background)
- Void Dark: `#0A0A0F` (secondary background)
- Neon Cyan: `#00D4FF` (primary accent)
- Neon Blue: `#4D9FFF` (secondary accent)
- Soft Blue: `#9DC4FF` (tertiary accent, tour section bg)

### Fonts
- Display: Inter (800 weight) -- headings, buttons
- Monospace: JetBrains Mono -- labels, dates, decode text

## Notes

- Edit ONLY `src/config.ts` to change content.
- All animations are scroll-driven via GSAP ScrollTrigger -- they remain unchanged.
- The 3D cube requires exactly 6 textures.
- Images go in `public/` with paths like `"/image-name.jpg"`.
- Sections return null when config is empty.
