# Forest Chocolate Landing Page Template

A dark, elegant chocolate brand landing page with jungle-themed design, Framer Motion animations, and interactive hotspots. Perfect for luxury chocolate brands or nature-inspired brands.

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Features

- 6 sections: Hero, Story, Product, Explore, Tasting, Footer
- Mouse-following decorative leaves
- Interactive jungle hotspots with modals
- Flavor analysis bars
- Responsive design

## Tech Stack

- **React 19** + **TypeScript** + **Vite 7**
- **Tailwind CSS 3**
- **Framer Motion**
- **Lucide React** icons

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
// 1. Hero Section
heroConfig: {
  subtitle: string,              // Subtitle, e.g. "Artisan Chocolatier"
  titleLine1: string,            // Title line 1, e.g. "CACAO"
  titleLine2: string,            // Title line 2, e.g. "SAUVAGE"
  tagline: string,               // Tagline, e.g. "Wild Cacao · Jungle Secret Realm"
  chocolateText: string,         // Badge text, e.g. "85% Ecuadorian Dark Chocolate"
  ctaText: string,               // CTA button text, e.g. "Explore More"
  heroImage: string,             // Hero image path, e.g. "images/hero_chocolate.jpg"
  leafImages: [string, string]   // Decorative leaf images, e.g. ["images/leaf_1.png", "images/leaf_2.png"]
}

// 2. Story Section
storyConfig: {
  label: string,                 // Section label, e.g. "Brand Story"
  heading: string[],             // Heading text array, e.g. ["Deep in the Jungle"]
  headingAccent: string,         // Accent heading text (gold), e.g. "Sweet Secret"
  paragraphs: string[],          // Paragraph content, 3 items
  stats: Array<{value, label}>,  // Statistics, e.g. [{value: "85%", label: "Cacao Content"}, ...]
  storyImage: string             // Story image, e.g. "images/brand_story_jungle.jpg"
}

// 3. Product Section
productConfig: {
  label: string,                 // Section label, e.g. "Product Showcase"
  heading: string[],             // Heading text array, e.g. ["Jungle"]
  headingAccent: string,         // Accent heading text (gold), e.g. "Treasure"
  productTitle: string,          // Product name, e.g. "85% Ecuadorian Dark Chocolate"
  description: string,           // Product description
  features: string[],            // Product features, 6 items
  price: string,                 // Price, e.g. "$298"
  priceLabel: string,            // Price label, e.g. "Price"
  specs: string,                 // Specifications, e.g. "80g / bar"
  specsLabel: string,            // Specs label, e.g. "Specification"
  ctaPrimary: string,            // Primary button text, e.g. "Buy Now"
  ctaSecondary: string,          // Secondary button text, e.g. "Learn More"
  productImage: string           // Product image, e.g. "images/product_packaging.jpg"
}

// 4. Explore Section (interactive)
exploreConfig: {
  label: string,                 // Section label, e.g. "Interactive Experience"
  heading: string[],             // Heading text array, e.g. ["Explore"]
  headingAccent: string,         // Accent heading text (gold), e.g. "Jungle"
  description: string,           // Section description
  hint: string,                  // Hint text, e.g. "Click the spots to explore more"
  exploreImage: string,          // Explore background image, e.g. "images/explore_jungle.jpg"
  hotspots: Array<{
    id: string,                  // Unique hotspot ID
    x: number,                   // Horizontal position 0-100%
    y: number,                   // Vertical position 0-100%
    title: string,               // Hotspot title
    description: string,         // Hotspot description
    iconType: "bird" | "pawprint" | "treepine" | "flower",  // Icon type
    image: string                // Hotspot image path
  }>
}

// 5. Tasting Section
tastingConfig: {
  label: string,                 // Section label, e.g. "Tasting Guide"
  heading: string[],             // Heading text array, e.g. ["Tasting"]
  headingAccent: string,         // Accent heading text (gold), e.g. "Journey"
  description: string,           // Section description
  tastingCards: Array<{
    iconType: "eye" | "wind" | "sparkles",  // Icon type
    title: string,               // Card title, e.g. "Appearance"
    description: string,         // Card description
    notes: string[]              // Tasting notes, e.g. ["Deep Brown", ...]
  }>,
  flavorWheel: {
    title: string,               // Flavor wheel title, e.g. "Flavor Wheel"
    description: string,         // Flavor wheel description
    tags: string[],              // Flavor tags, e.g. ["Cacao", "Blackberry", ...]
    bars: Array<{
      label: string,             // Flavor bar label, e.g. "Bitterness"
      value: number,             // Value 0-100
      color: string              // Color value, e.g. "#3D2817"
    }>
  }
}

// 6. Footer Section
footerConfig: {
  brandName: string,             // Brand name, e.g. "CACAO SAUVAGE"
  brandTagline: string,          // Brand tagline, e.g. "Gift from the Jungle"
  brandDescription: string,      // Brand description
  socialLinks: Array<{
    platform: "instagram" | "facebook" | "twitter",  // Social platform
    href: string                 // Link URL
  }>,
  navSectionTitle: string,       // Navigation title, e.g. "Quick Links"
  navLinks: Array<{label, href}>,  // Navigation links
  contactSectionTitle: string,   // Contact title, e.g. "Contact Us"
  contactAddress: string,        // Contact address, use \n for line breaks
  contactPhone: string,          // Contact phone
  contactEmail: string,          // Contact email
  newsletterTitle: string,       // Newsletter title
  newsletterDescription: string, // Newsletter description
  newsletterPlaceholder: string, // Input placeholder text
  newsletterButton: string,      // Subscribe button text
  copyright: string,             // Copyright text
  policyLinks: Array<{label, href}>  // Policy links
}

// 7. Site Metadata
siteConfig: {
  title: string,                 // Site title
  description: string,           // Site description
  language: string               // Language code, e.g. "en"
}
```

## Required Images (10 total)

Add to `public/images/` directory:

### Hero Section (3)
- **hero_chocolate.jpg** - Product photo, 800x1200 portrait, with floating animation
- **leaf_1.png** - Decorative leaf PNG with transparent background, 800x800
- **leaf_2.png** - Decorative leaf PNG with transparent background, 800x800

### Story Section (1)
- **brand_story_jungle.jpg** - Jungle scene, 1200x800 landscape

### Product Section (1)
- **product_packaging.jpg** - Product packaging, 800x1200 portrait

### Explore Section (5)
- **explore_jungle.jpg** - Interactive jungle scene, 1600x900 landscape
- **toucan.png** - PNG with transparent background, 600x600
- **jaguar.png** - PNG with transparent background, 600x600
- **cacao.png** - PNG with transparent background, 600x600
- **orchid.png** - PNG with transparent background, 600x600

## Design

### Colors
- **Gold accent:** `#C9A227`
- **Dark greens:** `#0D2818`, `#05140A`, `#0a1f12`
- **Beige text:** `#F5F5DC`
- **Light green text:** `#8FBC8F`

### Animations
- **Hero:** Staggered entry, floating product, mouse-following leaves
- **Story:** Scroll reveals, hover zoom, stats counter
- **Product:** Floating image, rotating decorative elements, staggered features
- **Explore:** Pulsing hotspots, spring-animated modal entrance
- **Tasting:** Card hover lift, icon rotation, flavor bar fill
- **Footer:** Social icon scale, link slide-in, leaf rotation

### Custom Components
- **JungleLeaf** - Decorative leaf component with mouse tracking and sway effect
- **ScrollReveal** - Scroll animation wrapper (directions: up/down/left/right)

## Build

```bash
npm run build
```

Output: `dist/` folder (deployable to Vercel/Netlify/static hosting)

## Project Structure

```
├── src/
│   ├── config.ts              ← Edit this file for all content
│   ├── App.tsx                ← Root component
│   ├── sections/              ← 6 section components
│   ├── components/custom/     ← JungleLeaf, ScrollReveal
│   └── components/ui/         ← shadcn/ui component library
├── public/images/             ← Add 10 images here
└── README.md                  ← Full documentation
```

## Notes

- **Only edit `src/config.ts`** — components read content from config
- Components auto-hide when config is empty
- All Framer Motion animations are preserved in components
- Remember to update `<title>` in `index.html`
- Use `\n` for multi-line text (e.g. addresses)
- Hotspot positions are percentage values (0-100)

## Example Config

```typescript
export const heroConfig: HeroConfig = {
  subtitle: "Artisan Chocolatier",
  titleLine1: "CACAO",
  titleLine2: "SAUVAGE",
  tagline: "Wild Cacao · Jungle Secret Realm",
  chocolateText: "85% Ecuadorian Dark Chocolate",
  ctaText: "Explore More",
  heroImage: "images/hero_chocolate.jpg",
  leafImages: ["images/leaf_1.png", "images/leaf_2.png"]
};

export const exploreConfig: ExploreConfig = {
  label: "Interactive Experience",
  heading: ["Explore"],
  headingAccent: "Jungle",
  description: "Click the spots in the scene to discover secrets hidden deep in the jungle",
  hint: "Click the spots to explore more",
  exploreImage: "images/explore_jungle.jpg",
  hotspots: [
    {
      id: "toucan",
      x: 25,
      y: 30,
      title: "Toucan",
      description: "The messenger of the tropical rainforest, delivering the jungle's news with its vibrant beak...",
      iconType: "bird",
      image: "images/toucan.png"
    },
    {
      id: "jaguar",
      x: 55,
      y: 65,
      title: "Jaguar",
      description: "Guardian of the jungle, patrolling this mysterious land under the moonlight...",
      iconType: "pawprint",
      image: "images/jaguar.png"
    },
    {
      id: "cacao",
      x: 75,
      y: 25,
      title: "Wild Cacao",
      description: "The source of flavor, quietly growing deep in Ecuador's rainforest...",
      iconType: "treepine",
      image: "images/cacao.png"
    },
    {
      id: "orchid",
      x: 15,
      y: 70,
      title: "Orchid",
      description: "The soul of the rainforest, blossoming gracefully in the dim forest...",
      iconType: "flower",
      image: "images/orchid.png"
    }
  ]
};

export const tastingConfig: TastingConfig = {
  label: "Tasting Guide",
  heading: ["Tasting"],
  headingAccent: "Journey",
  description: "Use your senses to explore every layer of Cacao Sauvage's flavor",
  tastingCards: [
    {
      iconType: "eye",
      title: "Appearance",
      description: "Deep as the jungle night, glossy as moonlight pours down",
      notes: ["Deep brown color", "Delicate luster", "Perfect snap"]
    },
    {
      iconType: "wind",
      title: "Aroma",
      description: "Rich cacao interwoven with tropical floral notes",
      notes: ["Rich cacao", "Tropical fruit", "Subtle floral scent"]
    },
    {
      iconType: "sparkles",
      title: "Texture",
      description: "Silky as morning mist, layered like the depths of the jungle",
      notes: ["Melts in your mouth", "Rich layering", "Long finish"]
    }
  ],
  flavorWheel: {
    title: "Flavor Wheel",
    description: "Cacao Sauvage features a rich and complex flavor profile, starting with intense cacao, followed by mid-palate tropical fruit notes, and ending with a lingering woody tone—like a journey through the Ecuadorian rainforest.",
    tags: ["Cacao", "Blackberry", "Vanilla", "Nuts", "Woody", "Floral"],
    bars: [
      { label: "Bitterness", value: 85, color: "#3D2817" },
      { label: "Sourness", value: 45, color: "#8B4513" },
      { label: "Sweetness", value: 30, color: "#D4AF37" },
      { label: "Aroma", value: 90, color: "#C9A227" }
    ]
  }
};
```
