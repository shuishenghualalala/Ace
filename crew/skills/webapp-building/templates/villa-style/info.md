# Villa Template

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Title Guidelines
- **Hero titles must be short and impactful.** Aim for 3-6 words. Use the `scriptText` field above the title for secondary messaging instead of cramming everything into `mainTitle`.
- **If a title must wrap to multiple lines** (using `\n`), ensure the lines are roughly equal in length for visual symmetry. Avoid lopsided breaks like a long first line and a short second line.
- **Section headings** (`mainTitle` in each config) should also be concise — preferably a single line. Move details to subtitles or body text.
- These rules apply to ALL languages.

## Features

- **Preloader** with animated brand reveal (Wine icon, brand name, script subname, loading line)
- **Navigation** with desktop dropdowns and full-screen mobile menu with icon support
- **Hero Section** with Ken Burns background, staggered reveal animations (bg -> title -> CTA -> stats), and count-up statistics
- **Wine Showcase** with tab-based wine selector, animated bottle display with color glow effects, tasting notes (ABV, temperature, aging), feature cards with icons, and quote block
- **Winery Carousel** with auto-advancing slides (6s interval), Ken Burns effect, location tag, area stats, and slide indicators
- **Museum Section** with tabbed content (icon tabs), horizontal timeline, year badge, opening hours, founder photo with sepia filter, and quote block
- **News Section** with 4-column article grid (category badges, dates, excerpts), 3-column testimonials with star ratings, and story section with timeline highlights and quote overlay
- **Contact Form** with Formspree integration, 4 contact info cards with icons, visit date picker, visitors dropdown, and success/error states
- **Footer** with newsletter subscription, social link icons, 2 link groups, contact items, copyright bar, legal links, ICP text, back-to-top button, and age verification notice
- **Scroll To Top** floating gold button
- All sections have scroll-triggered entrance animations (fade-up, slide-in-left, slide-in-right, scale-in) via IntersectionObserver
- Fully responsive design (mobile-first)
- All content driven by a single `config.ts` file
- Components render nothing when their config is empty

## Tech Stack

- React 18 + TypeScript
- Vite
- Tailwind CSS
- Lucide React (icons)
- shadcn/ui components
- Formspree (contact form / newsletter)

## Quick Start

```bash
cd villa
npm install
npm run dev
```

Open `src/config.ts` and fill in all configuration objects with your content.

## Configuration

All content is managed in `src/config.ts`. Every string field defaults to `""`, every array to `[]`, every number to `0`.

### siteConfig

```typescript
{
  title: "",        // Page title
  description: "",  // Meta description
  language: "",     // HTML lang attribute
  keywords: "",     // Meta keywords
  ogImage: "",      // Open Graph image URL
  canonical: "",    // Canonical URL
}
```

### navigationConfig

```typescript
{
  brandName: "",      // Brand name in navbar
  brandSubname: "",   // Secondary name (script font)
  tagline: "",        // Small tagline below brand
  navLinks: [],       // Array of { name, href, icon (string), dropdown?: [{ name, href }] }
  ctaButtonText: "",  // CTA button text
}
```

Available icon strings for navLinks: "Home", "Grape", "Users", "Newspaper", "BookOpen", "Mail", "Wine"

### preloaderConfig

```typescript
{
  brandName: "",    // Main brand name during loading
  brandSubname: "", // Script-font subname during loading
  yearText: "",     // e.g. "Est. 1887"
}
```

### heroConfig

```typescript
{
  scriptText: "",      // Decorative script text above title
  mainTitle: "",       // Main hero heading
  ctaButtonText: "",   // CTA button text
  ctaTarget: "",       // CTA scroll target (e.g. "#wines")
  stats: [],           // Array of { value: number, suffix: string, label: string }
  decorativeText: "",  // Vertical decorative text (left side)
  backgroundImage: "", // Hero background image path (e.g. "/images/hero-banner.jpg")
}
```

### wineShowcaseConfig

```typescript
{
  scriptText: "",  // Script-font label (e.g. "Our Wines")
  subtitle: "",    // Uppercase subtitle
  mainTitle: "",   // Section heading
  wines: [],       // Array of { id, name, subtitle, year, image, filter, glowColor, description, tastingNotes, alcohol, temperature, aging }
  features: [],    // Array of { icon (string), title, description }
  quote: { text: "", attribution: "", prefix: "" },
}
```

Available icon strings for features: "Wine", "Thermometer", "Clock", "Sparkles"
Wine filter examples: "" (no filter), "brightness(1.3) sepia(0.4) hue-rotate(30deg) saturate(0.6)" (white wine), "brightness(1.15) sepia(0.3) hue-rotate(-20deg) saturate(1.3)" (rose)
Wine glowColor examples: "bg-red-900/20", "bg-gold-500/15", "bg-pink-500/15"

### wineryCarouselConfig

```typescript
{
  scriptText: "",   // Script-font label
  subtitle: "",     // Uppercase subtitle
  mainTitle: "",    // Section heading
  locationTag: "",  // Location with map pin (e.g. "Napa Valley, California")
  slides: [],       // Array of { image, title, subtitle, area, unit, description }
}
```

### museumConfig

```typescript
{
  scriptText: "",       // Script-font label
  subtitle: "",         // Uppercase subtitle
  mainTitle: "",        // Section heading
  introText: "",        // Introduction paragraph
  timeline: [],         // Array of { year, event }
  tabs: [],             // Array of { id, name, icon (string), image, content: { title, description, highlight } }
  openingHours: "",     // e.g. "Daily 9:00 AM - 5:00 PM"
  openingHoursLabel: "", // e.g. "Opening Hours"
  ctaButtonText: "",    // CTA button text
  yearBadge: "",        // e.g. "1887"
  yearBadgeLabel: "",   // e.g. "Founded"
  quote: { prefix: "", text: "", attribution: "" },
  founderPhotoAlt: "",  // Alt text for founder photo
  founderPhoto: "",     // Founder photo path (e.g. "/images/photo-retro.png")
}
```

Available icon strings for tabs: "History", "BookOpen", "Award"

### newsConfig

```typescript
{
  scriptText: "",              // Script-font label
  subtitle: "",                // Uppercase subtitle
  mainTitle: "",               // Section heading
  viewAllText: "",             // "View all" button text
  readMoreText: "",            // "Read more" link text
  articles: [],                // Array of { id, image, title, excerpt, date, category }
  testimonialsScriptText: "",  // Testimonials script label
  testimonialsSubtitle: "",    // Testimonials subtitle
  testimonialsMainTitle: "",   // Testimonials heading
  testimonials: [],            // Array of { name, role, text, rating (1-5) }
  storyScriptText: "",         // Story script label
  storySubtitle: "",           // Story subtitle
  storyTitle: "",              // Story heading
  storyParagraphs: [],         // Story body paragraphs (array of strings)
  storyTimeline: [],           // Array of { value, label }
  storyQuote: { prefix: "", text: "", attribution: "" },
  storyImage: "",              // Story image path
  storyImageCaption: "",       // Story image alt text
}
```

### contactFormConfig

```typescript
{
  scriptText: "",        // Script-font label
  subtitle: "",          // Uppercase subtitle
  mainTitle: "",         // Section heading
  introText: "",         // Intro text below heading
  contactInfoTitle: "",  // Contact info sidebar heading
  contactInfo: [],       // Array of { icon (string), label, value, subtext }
  form: {
    nameLabel: "",
    namePlaceholder: "",
    emailLabel: "",
    emailPlaceholder: "",
    phoneLabel: "",
    phonePlaceholder: "",
    visitDateLabel: "",
    visitorsLabel: "",
    visitorsOptions: [],  // e.g. ["1", "2", "3-5", "6-10", "10+"]
    messageLabel: "",
    messagePlaceholder: "",
    submitText: "",
    submittingText: "",
    successMessage: "",
    errorMessage: "",
  },
  privacyNotice: "",
  formEndpoint: "",  // Formspree URL
}
```

Available icon strings for contactInfo: "MapPin", "Phone", "Mail", "Clock"

### footerConfig

```typescript
{
  brandName: "",
  tagline: "",
  description: "",
  socialLinks: [],         // Array of { icon (string), label, href }
  linkGroups: [],           // Array of { title, links: [{ name, href }] }
  contactItems: [],         // Array of { icon (string), text }
  newsletterLabel: "",
  newsletterPlaceholder: "",
  newsletterButtonText: "",
  newsletterSuccessText: "",
  newsletterErrorText: "",
  newsletterEndpoint: "",   // Formspree URL
  copyrightText: "",
  legalLinks: [],           // Array of strings (e.g. ["Privacy Policy", "Terms of Use"])
  icpText: "",
  backToTopText: "",
  ageVerificationText: "",
}
```

Available icon strings for socialLinks: "Instagram", "Facebook", "Twitter", "Youtube"
Available icon strings for contactItems: "MapPin", "Phone", "Mail"

### scrollToTopConfig

```typescript
{
  ariaLabel: "",  // e.g. "Back to top"
}
```

## Required Images

Place the following images in `public/images/`:

- `hero-banner.jpg` - Hero section background (landscape, ~1920x1080)
- `wine-bottle.png` - Wine showcase bottle (transparent PNG, ~400x900)
- `slider01.jpg` - Winery carousel slide 1 (landscape)
- `slider02.jpg` - Winery carousel slide 2 (landscape)
- `slider03.jpg` - Winery carousel slide 3 (landscape)
- `museum-tab1.jpg` - Museum tab 1 image (portrait, 4:5 ratio)
- `museum-tab2.jpg` - Museum tab 2 image (portrait, 4:5 ratio)
- `museum-tab3.jpg` - Museum tab 3 image (portrait, 4:5 ratio)
- `museum.jpg` - Story section image (portrait, 4:5 ratio)
- `news01.jpg` - News article 1 image (landscape, 3:2 ratio)
- `news02.jpg` - News article 2 image (landscape, 3:2 ratio)
- `news03.jpg` - News article 3 image (landscape, 3:2 ratio)
- `photo-retro.png` - Museum founder photo (square, ~200x200)

## Design

### Colors
- **Gold palette**: #d2a855 (primary gold-500), #dbb977 (gold-400), #e4cb99 (gold-300), #a88644 (gold-600), #7e6533 (gold-700)
- **Wine/dark palette**: #141414 (page background), #0e0e0e (wine-800), #070707 (wine-900), #1d1d1d (wine-600)
- **Accent**: #c71717 (red)

### Fonts
- **Cormorant Garamond** (serif) - headings and display text
- **Poppins** (sans-serif) - body text
- **Qwitcher Grypen** (script/cursive) - decorative accents and section labels

### Animations
- Scroll-triggered entrance animations via IntersectionObserver (fade-up, slide-in-left, slide-in-right, scale-in)
- Ken Burns zoom effect on hero and carousel images
- Count-up animated statistics in hero section
- Staggered reveal sequence in hero (background -> title -> CTA -> stats)
- Preloader with fade-in text, loading line, and fade-out sequence
- Bottle switcher transitions with opacity and translate
- Tab content crossfade transitions

## Notes

- All components check for empty config and render nothing if unconfigured
- The `language` field in siteConfig should be set to `""` in the template and populated when deploying
- Icon fields in config use string names (e.g. "Wine", "MapPin") that are resolved via icon lookup maps in each component
- The contact form and newsletter use Formspree endpoints - replace with your own form IDs
- Images should be optimized for web (JPG for photos, PNG for transparent images)
- The wine bottle image can be reused for all wines with different CSS `filter` values to change appearance
- The template uses a dark theme (#141414 background) - all text colors are white with varying opacity
