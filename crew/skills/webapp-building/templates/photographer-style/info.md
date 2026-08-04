# Photographer Style Template

A high-end, dark-themed single-page website template with dramatic GSAP animations, 3D transforms, particle effects, and a cinematic aesthetic. Ideal for photographers, creative studios, visual artists, and luxury brands.

## Language

If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content

The actual content of the website should match the user's query.

## Features

- Full-screen hero with chromatic aberration and parallax scrolling
- GSAP ScrollTrigger animations on every section (scroll-triggered reveals, 3D flips, elastic bounces)
- Interactive custom cursor with hover states (desktop only)
- Canvas-based floating particle field background
- Noise texture overlay for cinematic grain
- Floating image preview on service item hover
- 3D card transforms with mouse-tracking rotation
- Animated price counter with GSAP tweening
- Typewriter effect for blog section title
- Clip-path polygon reveal animations
- Marquee text with per-character highlighting in footer
- Accordion FAQ with alternating left/right offset
- Responsive mobile menu with staggered link animations
- Reduced motion support via prefers-reduced-motion

## Tech Stack

- React 19 + TypeScript
- Vite 7
- Tailwind CSS 3 + tailwindcss-animate
- GSAP 3 (ScrollTrigger)
- Lucide React (icons)
- Geist font (Google Fonts)

## Quick Start

```bash
npm install
npm run dev
```

## Configuration

All content is managed in `src/config.ts`. Each section reads from its own exported config constant. When config fields are empty, the corresponding section returns `null` and is hidden.

### siteConfig
- `title` (string): Browser tab title. Example: `"Aperture Studio"`
- `description` (string): Meta description. Example: `"Professional photography services"`
- `language` (string): HTML lang attribute. Example: `"en"`

### navigationConfig
- `logo` (string): Brand name in the navbar. Example: `"APERTURE"`
- `items` (NavItem[]): Nav links. Example: `[{ label: "Works", href: "#works" }, { label: "About", href: "#about" }]`

### heroConfig
- `title` (string): Large animated hero title (each character animates). Example: `"APERTURE"`
- `subtitle` (string): Subtitle text. Example: `"Capture Your Vision"`
- `backgroundImage` (string): Path to hero image. Example: `"/hero-main.jpg"`
- `servicesLabel` (string): Vertical text on left side. Example: `"Photography | Video | Post"`
- `copyright` (string): Bottom-right text. Example: `"© 2024 Aperture Studio"`

### aboutConfig
- `titleLine1` (string): First title line. Example: `"We blend artistry with technical excellence,"`
- `titleLine2` (string): Second title line. Example: `"creating visual stories that captivate and endure."`
- `description` (string): About paragraph text.
- `image1` (string): Primary about image path. Example: `"/about-1.jpg"`
- `image1Alt` (string): Alt text for image 1.
- `image2` (string): Secondary overlapping image path. Example: `"/about-2.jpg"`
- `image2Alt` (string): Alt text for image 2.
- `authorImage` (string): Author portrait path. Example: `"/photographer.jpg"`
- `authorName` (string): Author name. Example: `"Alex Chen"`
- `authorBio` (string): Author biography text (animated character by character).

### worksConfig
- `title` (string): Section title (animated per character). Example: `"Selected Works"`
- `subtitle` (string): Subtitle. Example: `"A curated collection of our finest work."`
- `projects` (WorkItem[]): Portfolio items. Example: `[{ id: 1, title: "Aurora Series", category: "Editorial", image: "/work-1.jpg" }]`

### servicesConfig
- `title` (string): Section title. Example: `"Our Craft"`
- `subtitle` (string): Subtitle. Example: `"Every project is an opportunity to push boundaries."`
- `services` (ServiceItem[]): Service items. Example: `[{ id: "01", title: "Editorial Photography", description: "High-end fashion and commercial shoots", image: "/service-1.jpg" }]`

### testimonialsConfig
- `title` (string): Section title. Example: `"Client Voices"`
- `testimonials` (TestimonialItem[]): Items. Example: `[{ id: 1, name: "Sarah Johnson", title: "Creative Director", quote: "Extraordinary work.", image: "/testimonial-1.jpg" }]`

### pricingConfig
- `title` (string): Section title. Example: `"Investment"`
- `subtitle` (string): Subtitle. Example: `"Premium packages crafted for exceptional results."`
- `ctaButtonText` (string): Card button text. Example: `"Choose Plan"`
- `plans` (PricingPlan[]): Plans. Example: `[{ id: 1, name: "Essential", price: 500, unit: "per session", featured: false, features: ["2-hour shoot", "20 edited photos"] }]`

### faqConfig
- `title` (string): Section title. Example: `"FAQ"`
- `faqs` (FAQItem[]): Questions. Example: `[{ question: "What is your turnaround time?", answer: "Most projects complete within 2-4 weeks." }]`

### blogConfig
- `title` (string): Section title. Example: `"Studio Notes"`
- `subtitle` (string): Subtitle. Example: `"Insights from our creative journey."`
- `allPostsLabel` (string): View all button. Example: `"All Posts"`
- `readMoreLabel` (string): Read more link. Example: `"Read More"`
- `readTimePrefix` (string): Prefix before read time. Example: `"Read "`
- `posts` (BlogPost[]): Blog posts. Example: `[{ id: 1, title: "The Art of Natural Light", excerpt: "...", readTime: "5 min", date: "Mar 15, 2024", image: "/blog-1.jpg", category: "Tips" }]`

### contactConfig
- `title` (string): Section title (animated per character). Example: `"Let's Create Together"`
- `subtitle` (string): Subtitle. Example: `"Your vision deserves an extraordinary presentation."`
- `nameLabel` (string): Name field label. Example: `"Name *"`
- `emailLabel` (string): Email field label. Example: `"Email *"`
- `projectTypeLabel` (string): Project type label. Example: `"Project Type"`
- `projectTypePlaceholder` (string): Select placeholder. Example: `"Select..."`
- `projectTypeOptions` (ContactFormOption[]): Options. Example: `[{ value: "editorial", label: "Editorial Photography" }]`
- `messageLabel` (string): Message label. Example: `"Message"`
- `submitButtonText` (string): Submit button. Example: `"Send Message"`
- `image` (string): Contact section image path. Example: `"/contact.jpg"`

### footerConfig
- `marqueeText` (string): Scrolling text. Example: `"Every Vision Deserves to Be Captured"`
- `marqueeHighlightChars` (string[]): Characters to glow in red. Example: `["V", "C"]`
- `navLinks1` (FooterLink[]): Column 1 links. Example: `[{ label: "Home", href: "#hero" }]`
- `navLinks2` (FooterLink[]): Column 2 links. Supports icon field: `"Instagram"` or `"Dribbble"`. Example: `[{ label: "Instagram", href: "#", icon: "Instagram" }]`
- `ctaText` (string): CTA text. Example: `"View More"`
- `ctaHref` (string): CTA href. Example: `"#contact"`
- `copyright` (string): Copyright line. Example: `"© 2024 Aperture Studio. All rights reserved."`
- `tagline` (string): Tagline. Example: `"Crafted with passion"`

## Required Images

Place in `public/` directory:

- `hero-main.jpg` - Hero background (1920x1080+)
- `about-1.jpg` - About primary image (800x1200)
- `about-2.jpg` - About secondary image (600x900)
- `photographer.jpg` - Author portrait (400x400)
- `work-1.jpg` to `work-4.jpg` - Portfolio items (800x1067, 3:4 ratio)
- `service-1.jpg` to `service-4.jpg` - Service preview images (600x800)
- `testimonial-1.jpg` to `testimonial-3.jpg` - Testimonial avatars (400x400)
- `blog-1.jpg`, `blog-2.jpg` - Blog covers (1200x675, 16:9 ratio)
- `contact.jpg` - Contact section image (800x1200)

## Design

- **Color scheme**: Dark theme with black background (#000000), white text, red accent (#ea0000)
- **Font**: Geist (Google Fonts), with a dramatic type scale from 14px to 168px
- **Visual effects**: Noise overlay, chromatic aberration, particle field, custom cursor
- **Animations**: GSAP-powered with scroll triggers on every section

## Notes

- Sections with empty config automatically return null (hidden)
- Custom cursor and particle effects degrade gracefully on touch/mobile devices
- Footer icon field supports "Instagram" and "Dribbble" as string values mapped to Lucide icons
- The FAQ answer text is rendered as a single paragraph (no sentence splitting)
- Price values animate from 0 to target value when scrolled into view
- The `language` field in siteConfig should be set to match the content language (e.g., "en", "zh-CN", "ja")
