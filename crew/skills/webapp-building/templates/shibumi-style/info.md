# Shibumi Template

A premium artisan handcrafted home goods e-commerce template with elegant animations, parallax effects, and a full shopping cart system. Features 11 configurable sections driven entirely by a single `config.ts` file.

## Language

If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content

The actual content of the website should match the user's query.

## Features

- Full-screen parallax hero with fade-in animations and dual CTA buttons
- SubHero section with dual parallax images, clip-path reveals, 3D tilt effects, and animated stat counters
- Split-screen video/content section with staggered text reveals
- Product grid with category filtering, hover zoom, and add-to-cart with confirmation feedback
- Features section with icon-mapped cards in a 4-column grid
- Blog journal with full-bleed image cards, gradient overlays, and hover reveal animations
- Accordion FAQ with animated plus/cross toggle
- Multi-panel About section with full-width parallax images and alternating layouts
- Contact section with background image, contact info, and form with submission states
- Footer with link groups, social icons, and newsletter subscription
- Slide-out shopping cart sidebar with quantity controls
- Full-screen navigation menu with search, links, social icons, and background image
- Each section has null check - renders nothing when config is empty

## Tech Stack

- React 19 + TypeScript
- Vite
- Tailwind CSS 3
- Lucide React (icons)

## Quick Start

```bash
npm install
npm run dev
```

## Configuration

All content is in `src/config.ts`. Each section has a typed config object with empty placeholder values.

### siteConfig

```ts
{
  title: "",          // Browser tab title
  description: "",    // Meta description
  language: "",       // HTML lang attribute (e.g. "en", "zh", "ja")
}
```

### navigationConfig

```ts
{
  brandName: "",              // Brand name shown in nav bar and cart header
  menuLinks: [],              // Array of { label: string, href: string }
  socialLinks: [],            // Array of { icon: "Instagram"|"Facebook"|"Twitter", label: string, href: string }
  searchPlaceholder: "",      // Placeholder text for search input
  cartEmptyText: "",          // Text shown when cart is empty
  cartCheckoutText: "",       // Checkout button text
  continueShoppingText: "",   // Continue shopping button text
  menuBackgroundImage: "",    // Image path for the menu background panel
}
```

### heroConfig

```ts
{
  tagline: "",            // Small uppercase text above the title
  title: "",              // Main heading (use \n for line breaks)
  ctaPrimaryText: "",     // Primary CTA button text
  ctaPrimaryTarget: "",   // Primary CTA scroll target (e.g. "#products")
  ctaSecondaryText: "",   // Secondary CTA button text
  ctaSecondaryTarget: "", // Secondary CTA scroll target (e.g. "#about")
  backgroundImage: "",    // Path to hero background image
}
```

### subHeroConfig

```ts
{
  tag: "",              // Small uppercase label (e.g. "Our Philosophy")
  heading: "",          // Section heading
  bodyParagraphs: [],   // Array of paragraph strings
  linkText: "",         // Link text below paragraphs
  linkTarget: "",       // Link scroll target (e.g. "#about")
  image1: "",           // Main image path (top-right, larger)
  image2: "",           // Secondary image path (bottom-left, smaller)
  stats: [],            // Array of { value: number, suffix: string, label: string }
                        // suffix examples: "+", "%", "" (empty for plain number)
}
```

### videoSectionConfig

```ts
{
  tag: "",              // Small uppercase label
  heading: "",          // Section heading
  bodyParagraphs: [],   // Array of paragraph strings
  ctaText: "",          // CTA button text
  ctaTarget: "",        // CTA scroll target
  backgroundImage: "",  // Left-side background image path
}
```

### productsConfig

```ts
{
  tag: "",              // Small uppercase label (e.g. "Our Collection")
  heading: "",          // Section heading
  description: "",      // Description paragraph
  viewAllText: "",      // View all button text
  addToCartText: "",    // Add to cart button text
  addedToCartText: "",  // Confirmation text after adding
  categories: [],       // Array of category strings (first is "All" equivalent)
  products: [],         // Array of { id: number, name: string, price: number, category: string, image: string }
}
```

### featuresConfig

```ts
{
  features: [],   // Array of { icon: "Truck"|"ShieldCheck"|"Leaf"|"Heart", title: string, description: string }
}
```

### blogConfig

```ts
{
  tag: "",            // Small uppercase label (e.g. "Journal")
  heading: "",        // Section heading
  viewAllText: "",    // View all link text
  readMoreText: "",   // Read more text on each card
  posts: [],          // Array of { id: number, title: string, date: string, image: string, excerpt: string }
}
```

### faqConfig

```ts
{
  tag: "",          // Small uppercase label (e.g. "Support")
  heading: "",      // Section heading
  ctaText: "",      // CTA link text below FAQs
  ctaTarget: "",    // CTA scroll target (e.g. "#contact")
  faqs: [],         // Array of { id: number, question: string, answer: string }
}
```

### aboutConfig

```ts
{
  sections: [],   // Array of about section objects:
                  // {
                  //   tag: string,             // Small uppercase label
                  //   heading: string,          // Section heading
                  //   paragraphs: string[],     // Array of paragraph strings (used when no quote)
                  //   quote: string,            // Quote text (if present, paragraphs are ignored)
                  //   attribution: string,      // Quote attribution (e.g. "-- Name, Title")
                  //   image: string,            // Background image path
                  //   backgroundColor: string,  // CSS color for content area (e.g. "#423d3f")
                  //   textColor: string,         // CSS color for text (e.g. "#ffffff")
                  // }
                  // Sections alternate layout: odd indices are reversed
}
```

### contactConfig

```ts
{
  heading: "",            // Large heading text
  description: "",        // Description paragraph
  locationLabel: "",      // Label for location
  location: "",           // Location text
  emailLabel: "",         // Label for email
  email: "",              // Email address
  phoneLabel: "",         // Label for phone
  phone: "",              // Phone number
  formFields: {
    nameLabel: "",        // Name field label
    namePlaceholder: "",  // Name field placeholder
    emailLabel: "",       // Email field label
    emailPlaceholder: "", // Email field placeholder
    messageLabel: "",     // Message field label
    messagePlaceholder: "", // Message field placeholder
  },
  submitText: "",         // Submit button text
  submittingText: "",     // Text while submitting
  submittedText: "",      // Text after submission
  successMessage: "",     // Success message below form
  backgroundImage: "",    // Background image path
}
```

### footerConfig

```ts
{
  brandName: "",              // Brand name in footer
  brandDescription: "",       // Short brand description
  newsletterHeading: "",      // Newsletter section heading
  newsletterDescription: "",  // Newsletter description text
  newsletterPlaceholder: "",  // Email input placeholder
  newsletterButtonText: "",   // Subscribe button text
  newsletterSuccessText: "",  // Success message after subscribing
  linkGroups: [],             // Array of { title: string, links: { label: string, href: string }[] }
  legalLinks: [],             // Array of { label: string, href: string }
  copyrightText: "",          // Copyright text
  socialLinks: [],            // Array of { icon: "Instagram"|"Facebook"|"Twitter", label: string, href: string }
}
```

## Required Images

Place in `public/images/` directory:

- **Hero**: 1 background image (landscape, full-screen)
- **SubHero**: 2 images (1 large top-right, 1 smaller bottom-left)
- **Video Section**: 1 background image (left panel)
- **Products**: 1 image per product (portrait or square)
- **Blog**: 1 image per blog post (portrait, used as full-bleed card background)
- **About**: 1 image per about section (landscape, used as parallax background)
- **Contact**: 1 background image (landscape, full-screen)
- **Navigation**: 1 background image for the menu panel (optional)

## Design

- **Theme**: Clean white with warm brown (#8b6d4b) accent color
- **Typography**: DM Serif Display (headings), Libre Franklin (body text)
- **Animations**: CSS transitions with intersection observer triggers, parallax scrolling, clip-path reveals, count-up numbers
- **Layout**: Full-width sections with max-width containers, alternating backgrounds
- **Colors**: Primary brown (#8b6d4b), dark gray (#696969), light backgrounds (#fafafa, #f7f7f7)

## Notes

- Edit ONLY `src/config.ts` to change content
- All animations are preserved unchanged
- Images go in `public/images/`
- Sections return null when config is empty
- Feature icons use `icon` field mapped to Lucide components: `Truck`, `ShieldCheck`, `Leaf`, `Heart`
- Footer and navigation social icons use `icon` field mapped to: `Instagram`, `Facebook`, `Twitter`
- Hero title supports multi-line via `\n` in the string
- About sections alternate layout automatically (odd indices are reversed)
- Shopping cart functionality is built into App.tsx
