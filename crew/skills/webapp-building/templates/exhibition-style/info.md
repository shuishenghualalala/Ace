# Exhibition Style

A high-end editorial/magazine-style single-page website template with cinematic scroll animations, custom cursor, noise overlay, and magnetic hover effects. Ideal for art magazines, creative portfolios, exhibition showcases, and editorial publications.

## Language

If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content

The actual content of the website should match the user's query.

## Features

- Split-screen hero with 3D perspective tilt on mouse move and parallax scroll
- Horizontal scroll article gallery with velocity-based card skew
- Art category section with fixed sidebar, clip-path circle reveal, and pinned scroll
- Scattered polaroid-style lifestyle cards with random fly-in animation
- Mosaic design grid with grayscale-to-color hover and neighbor push effect
- Parallax video background section with scroll-driven playback rate
- Orbital avatar carousel with drag/swipe and dot navigation
- Instagram-style gallery grid with streak hover effect
- Curtain reveal footer with dark-mode transition on email focus
- Custom dot+ring cursor with hover state detection (auto-disabled on touch)
- SVG noise texture overlay for film grain aesthetic
- Magnetic elements that attract toward cursor position
- Lenis smooth scroll for buttery scrolling experience
- All animations respect prefers-reduced-motion

## Tech Stack

- React 19 + TypeScript
- Vite
- Tailwind CSS 3 with custom brand tokens
- GSAP 3 + ScrollTrigger
- Lenis (smooth scroll)
- Lucide React (icons)
- Radix UI primitives (shadcn/ui)

## Quick Start

```bash
npm install
npm run dev
```

## Configuration

All content is configured in `src/config.ts`. Each section has its own config object. When config values are empty strings or empty arrays, sections return `null` (blank page until populated).

### siteConfig
- `title` (string): Document title displayed in browser tab
- `description` (string): Meta description for SEO
- `language` (string): HTML lang attribute (e.g. "en", "zh-CN", "ja")

### navigationConfig
- `brandName` (string): Logo/brand text displayed in the nav bar. Example: "ArtView"
- `links` (NavLink[]): Navigation items. Example: `[{ label: "Art", href: "#art" }, { label: "Design", href: "#design" }]`
- `searchPlaceholder` (string): Search input placeholder. Example: "Search articles, authors, topics..."
- `searchHint` (string): Hint below search. Example: "Press Enter to search or ESC to close"
- `searchAriaLabel` (string): Aria label for search button. Example: "Search"
- `closeSearchAriaLabel` (string): Aria label for close. Example: "Close search"

### heroConfig
- `date` (string): Vertical date display. Example: "October 15, 2023"
- `titleLine1` (string): First line of hero title (light weight). Example: "Street Sculptures in"
- `titleLine2` (string): Second line (medium/bold). Example: "Downtown Prague"
- `readTime` (string): Reading time. Example: "9 min read"
- `description` (string): Hero paragraph text
- `ctaText` (string): CTA button text. Example: "Read More"
- `image` (string): Hero image path. Example: "/hero-sculpture.jpg"
- `imageAlt` (string): Image alt text

### latestArticlesConfig
- `sectionTitle` (string): Section heading. Example: "Latest Exhibitions"
- `articles` (ArticleItem[]): Array with `{id, title, subtitle, image, category}`. 5 items recommended for horizontal scroll.

### artCategoryConfig
- `sectionTitle` (string): Section heading. Example: "Latest Articles"
- `categoriesLabel` (string): Sidebar label. Example: "Categories"
- `eventsLabel` (string): Events label. Example: "Events"
- `categories` (string[]): Filter buttons. Example: `["Art", "Design", "Lifestyle"]`
- `events` (EventItem[]): Sidebar events. Example: `[{date: "Dec 15", title: "Art Exhibition Opening", location: "Shanghai"}]`
- `featuredImage` (string): Featured article image path
- `featuredImageAlt` (string): Featured image alt
- `featuredLabel` (string): Badge text. Example: "Featured Article"
- `featuredTitle` (string): Featured article headline
- `featuredDescription` (string): Featured article body text
- `featuredCtaText` (string): CTA text. Example: "Continue Reading"
- `gridArticles` (GridArticle[]): Array with `{id, title, category, readTime}`. 4 items recommended.
- `readSuffix` (string): Text after read time. Example: " read"

### lifestyleConfig
- `sectionTitle` (string): Section heading. Example: "Lifestyle"
- `viewMoreText` (string): Link text. Example: "View More"
- `articles` (LifestyleArticle[]): Array with `{id, title, excerpt, image, rotation, position, baseZIndex?}`. rotation: -8 to 8 degrees. position: {x: 0-250, y: -30 to 50}. 5 items recommended.

### designConfig
- `sectionTitle` (string): Section heading. Example: "Design"
- `viewMoreText` (string): Link text. Example: "Explore More Designs"
- `items` (DesignItem[]): Array with `{id, title, quote, image, size, gridColumn?}`. size: "normal"|"wide"|"tall". 6 items recommended.

### greenTribeConfig
- `sectionTitle` (string): Section heading. Example: "Green Tribe"
- `sectionDescription` (string): Description text
- `readMoreText` (string): Card CTA. Example: "Read Full Article"
- `joinTitle` (string): Sidebar heading. Example: "Join the Tribe"
- `joinDescription` (string): Sidebar description
- `emailPlaceholder` (string): Email placeholder. Example: "Your email address"
- `subscribeText` (string): Button text. Example: "Subscribe"
- `memberCountText` (string): Member count. Example: "12,847 members have joined"
- `videoSrc` (string): Background video URL (mp4)
- `videoPoster` (string): Video poster image path
- `members` (TribeMember[]): Array with `{id, name, role, title, excerpt, avatar}`. 3-5 items recommended.

### authorsConfig
- `sectionTitle` (string): Section heading. Example: "Our Authors"
- `sectionSubtitle` (string): Subtitle. Example: "Drag or click avatars to explore"
- `articlesSuffix` (string): After count. Example: "articles"
- `authors` (Author[]): Array with `{id, name, role, avatar, articles, social: {instagram, twitter}}`. 5 items recommended.

### instagramGalleryConfig
- `handle` (string): Instagram handle. Example: "@artview_mag"
- `handleUrl` (string): Profile URL. Example: "https://instagram.com/artview_mag"
- `description` (string): Description. Example: "Follow us on Instagram for daily art inspiration"
- `followText` (string): Button text. Example: "Follow Us"
- `likesSuffix` (string): After likes count. Example: "likes"
- `images` (InstagramImage[]): Array with `{id, image, likes}`. 10 items recommended.

### footerConfig
- `brandWatermark` (string): Large background text. Example: "ArtView"
- `newsletterTitle` (string): Heading. Example: "Stay Connected"
- `newsletterDescription` (string): Description text
- `emailPlaceholder` (string): Placeholder. Example: "Your email address"
- `subscribeText` (string): Button. Example: "Subscribe"
- `subscribeSuccessMessage` (string): Alert text. Example: "Thanks for subscribing!"
- `categoriesLabel` (string): Column heading. Example: "Categories"
- `categories` (string[]): Category links
- `pagesLabel` (string): Column heading. Example: "Pages"
- `pages` (string[]): Page links
- `legalLabel` (string): Column heading. Example: "Legal"
- `legalLinks` (string[]): Legal links
- `socialLabel` (string): Column heading. Example: "Follow Us"
- `socialLinks` (object): `{instagram: "", twitter: "", youtube: ""}` URLs
- `backToTopText` (string): Button. Example: "Back to Top"
- `copyright` (string): Copyright. Example: "(c) 2024 ArtView. All rights reserved."
- `credit` (string): Credit line

## Required Images

All images are referenced from the `public/` directory via config fields:

- **Hero**: 1 image (1:1 aspect ratio, ~800x800px)
- **Latest Articles**: 5 images (3:4 aspect ratio, ~600x800px)
- **Art Category**: 1 featured image (16:10 aspect ratio, ~1200x750px)
- **Lifestyle**: 5 images (2:3 aspect ratio, ~600x900px)
- **Design**: 6 images (1:1 aspect ratio, ~600x600px)
- **Green Tribe**: 5 avatar images (1:1, ~200x200px) + 1 video poster (16:9)
- **Authors**: 5 avatar images (1:1, ~300x300px)
- **Instagram**: 10 images (1:1, ~400x400px)
- **Green Tribe Video**: 1 mp4 video URL (can be external)

Total: ~33 images + 1 video

## Design

### Colors
- Background: #f2ede7 (warm linen)
- Primary text: #161616
- Secondary text: #535353
- Muted text: #b1b1b1
- Borders: #e5e5e5
- Overlays: #000 (with opacity)
- Dark accents: #333

### Typography
- Headings: Oswald (200-700 weight, Google Fonts)
- Body: Roboto (100-900 weight with italic, Google Fonts)

### Key Animations
- GSAP ScrollTrigger for all scroll-based animations
- Lenis smooth scroll engine
- Custom cursor with dot + ring following mouse
- Magnetic effect on interactive elements
- Noise texture overlay with grain animation

## Notes

- The template uses `cursor: none` globally and relies on the custom cursor component. This is automatically disabled on touch devices.
- The Lifestyle section scattered card layout uses `rotation` and `position` for visual scatter. Use values: rotation between -8 and 8, position.x between 0 and 250, position.y between -30 and 50.
- The Design section mosaic uses `size` ("normal", "wide", "tall") and optional `gridColumn` for grid placement.
- The Green Tribe section can accept an external video URL (e.g. from Mixkit, Pexels) or a local video file in public/.
- Footer has a unique dark-mode transition: the entire footer turns dark when the newsletter email input is focused.
- All section components return `null` when their config is empty, enabling incremental population.
