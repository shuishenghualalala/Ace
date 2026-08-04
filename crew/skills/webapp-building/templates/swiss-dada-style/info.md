# Bold Product Landing Page Template

A high-impact, single-page product landing page template with Swiss-inspired grid design, bold typography, and cinematic scroll animations. Built for beauty/fashion/lifestyle brands that want a dark, premium aesthetic.

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Features

- Full-screen hero with animated grid collage and bold title blocks
- Product showcase with 3D mouse parallax and floating animation
- Interactive color palette grid with particle effects on click
- Brand philosophy section with diagonal split layout and glitch effect
- Responsive navigation with mobile slide menu
- Custom cursor with hover detection
- Film grain overlay texture
- Scroll-velocity skew effect on main content

## Tech Stack

- **React 19** + **TypeScript** + **Vite 7**
- **Tailwind CSS 3** with custom animations
- **GSAP 3** with ScrollTrigger (scroll-linked animations, parallax, 3D transforms)
- **Radix UI** + **shadcn/ui** component primitives
- **Lucide React** icons

## Quick Start

1. Clone this repository
2. Install dependencies:
   ```bash
   npm install
   ```
3. Edit `src/config.ts` with your content
4. Add images to `public/images/`
5. Run the dev server:
   ```bash
   npm run dev
   ```
6. Build for production:
   ```bash
   npm run build
   ```

## Configuration

All content is configured in `src/config.ts`. Edit this single file to customize your entire site. Components will hide automatically when their config values are empty.

---

### Navigation

```typescript
export const navigationConfig: NavigationConfig = {
  logo: "",            // Brand name displayed in header (e.g. "DARE")
  logoAccent: ".",     // Character after logo in accent color (e.g. ".")
  navLinks: [],        // Array of { label: string, href: string }
                       // href should match section IDs: "#products", "#palette", "#finale"
  ctaText: "",         // Call-to-action button text in nav bar
};
```

### Hero Section

```typescript
export const heroConfig: HeroConfig = {
  titleLine1: "",         // First line of the hero title (left-aligned block)
  titleLine2: "",         // Second line of the hero title (right-aligned block)
  subtitle: "",           // Small subtitle text below the title
  ctaText: "",            // CTA button text
  ctaHref: "",            // CTA button link (default: "#products")
  backgroundImage: "",    // Path to hero collage image: "/images/hero.jpg"
  gridRows: 6,            // Number of grid rows (default: 6)
  gridCols: 8,            // Number of grid columns (default: 8)
  pinkCells: [],          // Array of { row: number, col: number } for accent-colored cells
                          // These cells show solid pink instead of the background image
};
```

### Product Showcase

```typescript
export const productShowcaseConfig: ProductShowcaseConfig = {
  sectionLabel: "",       // Small label above the heading (e.g. "FEATURED PRODUCT")
  headingMain: "",        // Main heading text in white
  headingAccent: "",      // Heading text in accent color (appended to main)
  productName: "",        // Product name displayed below heading
  description: "",        // Product description paragraph
  price: "",              // Price display (e.g. "$299", "¥299")
  features: [],           // Array of { value: string, label: string }
                          // e.g. { value: "12H", label: "Long lasting" }
  colorSwatches: [],      // Array of hex color strings: ["#ff73c3", "#c41e3a"]
  colorSwatchesLabel: "", // Label above color swatches
  ctaText: "",            // Buy button text
  productImage: "",       // Path: "/images/product.png"
  productImageAlt: "",    // Alt text for product image
  decorativeText: "",     // Large background watermark text (very low opacity)
};
```

### Color Palette

```typescript
export const colorPaletteConfig: ColorPaletteConfig = {
  sectionLabel: "",       // Small centered label above heading
  headingMain: "",        // Main heading text in white
  headingAccent: "",      // Heading text in accent color (appended to main)
  colors: [],             // Array of color objects:
                          // {
                          //   name: string,        // Local name
                          //   nameSecondary: string,      // Secondary/alternative name (displayed on hover)
                          //   color: string,        // Hex color code
                          //   description: string,  // Short description
                          // }
  bottomText: "",         // Instructional text below the grid
  decorativeText: "",     // Large background watermark text
};
```

### Finale / Brand Philosophy

```typescript
export const finaleConfig: FinaleConfig = {
  sectionLabel: "",       // Small label above heading (e.g. "BRAND PHILOSOPHY")
  headingMain: "",        // Main heading (white text, line 1)
  headingAccent: "",      // Accent heading (pink text, line 2)
  tagline: "",            // Multi-line brand statement paragraph
  features: [],           // Array of strings: ["Cruelty-free", "Vegan", "Sustainable"]
  ctaText: "",            // CTA button text
  ctaHref: "",            // CTA button link
  image: "",              // Path: "/images/brand-portrait.jpg"
  imageAlt: "",           // Alt text for the image
  decorativeText: "",     // Large background watermark text
};
```

### Footer

```typescript
export const footerConfig: FooterConfig = {
  logo: "",               // Brand name in footer
  logoAccent: ".",        // Accent character after logo
  brandDescription: "",   // Short brand tagline/description
  socialLinks: [],        // Array of { platform: "instagram"|"twitter"|"youtube", href: string, label: string }
  linkSections: [],       // Array of { title: string, links: string[] }
                          // e.g. { title: "Products", links: ["All Products", "New Arrivals"] }
  contact: {
    address: "",          // Physical address
    phone: "",            // Phone number
    email: "",            // Email address
  },
  legalLinks: [],         // Array of { label: string, href: string }
  copyrightText: "",      // Copyright text (year is auto-prepended)
  decorativeText: "",     // Large background watermark text
};
```

### Site Metadata

```typescript
export const siteConfig: SiteConfig = {
  title: "",              // Browser tab title (update in index.html too)
  description: "",        // Site description for SEO
  language: "",            // Language code
};
```

---

## Required Images

Add these images to the `public/images/` directory:

### Hero Section
- **hero background** — A single large image that gets sliced into a grid collage. Recommended: **1920x1080** or larger. The image is divided across a 6x8 grid, so choose an image with distributed visual interest.

### Product Section
- **product image** — Product photo. Recommended: **transparent PNG** background, **800x1200** or similar portrait ratio. Gets displayed with drop shadow and 3D rotation effects.

### Finale Section
- **brand portrait** — Full-height portrait or lifestyle image. Displayed as `object-cover` on the right 40% of the section. Recommended: **800x1200** portrait orientation.

## Design

### Colors
- **Primary/Accent:** Pink `#ff73c3` (with dark `#e055a8` and light `#ff9dd4` variants)
- **Background:** Black `#000000`
- **Foreground:** White `#ffffff`
- **Muted text:** White at 40-60% opacity

To change the accent color, update `--pink` in `src/index.css` and the `pink` values in `tailwind.config.js`.

### Fonts
- **Display:** [Montserrat](https://fonts.google.com/specimen/Montserrat) (weights 100-900) — Used for headings, logos, buttons
- **Body:** [Open Sans](https://fonts.google.com/specimen/Open+Sans) (weights 300-800) — Used for body text, descriptions

Fonts are imported via Google Fonts in `src/index.css`.

### Animations
- **Hero grid:** Cells flip in from random positions with 3D rotateX, then parallax on scroll
- **Title blocks:** Scale and rotate decode animation with elastic easing
- **Product:** 3D mouse-tracking rotation, floating loop, scroll-triggered entrance
- **Color cards:** Flip-in on scroll, particle burst on click
- **Finale:** Diagonal line rotates with scroll, image zoom-in, text stagger reveal, periodic glitch effect on title
- **Global:** Scroll-velocity skew on main content, custom cursor with GSAP tracking

## Build

```bash
npm run build
```

Output is in the `dist/` folder, ready for static hosting (Vercel, Netlify, GitHub Pages, etc.).

## Project Structure

```
├── src/
│   ├── config.ts            ← Edit this file for all content
│   ├── main.tsx             ← React entry point
│   ├── App.tsx              ← Root component (scroll skew, layout)
│   ├── index.css            ← Global styles, fonts, CSS utilities
│   ├── components/
│   │   ├── Navigation.tsx   ← Fixed header + mobile menu
│   │   ├── CustomCursor.tsx ← Custom cursor (desktop only)
│   │   └── ui/              ← shadcn/ui component library
│   ├── sections/
│   │   ├── Hero.tsx         ← Full-screen grid hero
│   │   ├── ProductShowcase.tsx ← Product display with 3D effects
│   │   ├── ColorPalette.tsx ← Interactive color grid
│   │   ├── Finale.tsx       ← Brand philosophy split layout
│   │   └── Footer.tsx       ← Footer with links + contact
│   ├── hooks/
│   │   └── use-mobile.ts   ← Mobile detection hook
│   └── lib/
│       └── utils.ts         ← Utility functions (cn, etc.)
├── public/
│   └── images/              ← Add your images here
│       └── .gitkeep
├── index.html               ← HTML entry (update <title> here)
├── tailwind.config.js       ← Tailwind theme + custom animations
├── vite.config.ts           ← Vite build config
└── package.json             ← Dependencies
```

## Notes

- Edit **only** `src/config.ts` and add images to `public/images/`
- Do not modify component files unless fixing bugs
- Design properties (colors, fonts) are in `tailwind.config.js` and `src/index.css`
- All GSAP animations and scroll effects are preserved in components
- Components return `null` when their config is empty — no blank sections
- Update `<title>` in `index.html` to match your site name
