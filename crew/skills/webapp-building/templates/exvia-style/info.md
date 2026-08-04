# Exvia

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## Features
- Cinematic full-screen hero with blurred background and sharp mouse-tracking image window (crosshair cursor, role labels on sides)
- Scroll-triggered reveal animations using Intersection Observer with staggered timing
- Mouse parallax hover images on service cards
- Auto-advancing testimonial slider with star ratings, dot navigation, and arrow controls
- Page load overlay with animated progress bar
- Fixed navigation bar with transparent-to-white scroll transition and animated mobile hamburger menu
- Animated buttons with text slide effect on hover
- Bento grid portfolio layout with featured projects and inline CTA card
- Full footer with link columns, social icons, and newsletter subscription
- All content driven by a single config.ts file with null checks for empty sections

## Tech Stack
- React 19 + TypeScript
- Vite 7
- Tailwind CSS 3 with custom Exvia design system tokens
- Lucide React icons (dynamically resolved by PascalCase name)
- Geist font family (Regular 400, Medium 500, Mono 500) via CDN
- shadcn/ui component primitives

## Quick Start
```bash
npm install
npm run dev
```

## Configuration

All content is in `src/config.ts`. Each config export controls one section. Empty values cause the section to return null (not rendered).

### siteConfig
```ts
{
  language: "",      // site language code (e.g. "en", "zh", "ja")
  title: "",         // site title shown in page overlay and browser tab
  description: "",   // site meta description
}
```

### navigationConfig
```ts
{
  logo: "",            // logo text in navbar
  links: [],           // array of { label: string, href: string } for nav links
  contactLabel: "",    // contact button text
  contactHref: "",     // contact button href
}
```

### heroConfig
```ts
{
  name: "",              // Large hero heading at bottom of screen. Prefer a studio/brand name (not a personal name) unless the user specifically requests otherwise.
  roles: [],             // array of role strings displayed on left and right sides of hero
  backgroundImage: "",   // path to hero background image — should be a portrait/people photography for best visual effect with the mouse-tracking reveal (e.g. "/images/hero-bg.jpg")
}
```

### aboutConfig
```ts
{
  label: "",              // section label (e.g. "About")
  description: "",        // about paragraph text
  experienceValue: "",    // large number/text (e.g. "10")
  experienceLabel: "",    // label below experience value (e.g. "Years of\nExperience")
  stats: [],              // array of { value: string, label: string }
  images: [],             // array of { src: string, alt: string } - 4 images for 2x2 grid
}
```

### servicesConfig
```ts
{
  label: "",      // section label (e.g. "Services")
  heading: "",    // section heading
  services: [],   // array of { iconName: string, title: string, description: string, image: string }
}
```
Icon names use Lucide PascalCase: `Compass`, `PenTool`, `Layout`, `Code`, `Palette`, `Monitor`, `Camera`, `Layers`, etc.

### portfolioConfig
```ts
{
  label: "",          // section label (e.g. "Portfolio")
  heading: "",        // section heading
  description: "",    // section description paragraph
  projects: [],       // array of { title: string, category: string, year: string, image: string, featured?: boolean }
  cta: {              // inline CTA card in the portfolio grid
    label: "",        // small label text
    heading: "",      // CTA heading
    linkText: "",     // link text
    linkHref: "",     // link href
  },
  viewAllLabel: "",   // "View All Projects" button text
}
```
The grid layout uses: project[0] spans 2 cols, project[1] single, project[2] single, project[3] single, CTA card single, project[4] spans full 3 cols.

### testimonialsConfig
```ts
{
  label: "",          // section label (e.g. "Testimonials")
  heading: "",        // section heading
  testimonials: [],   // array of { quote: string, author: string, role: string, company: string, image: string, rating: number }
}
```

### ctaConfig
```ts
{
  tags: [],              // array of tag strings (e.g. ["UI/UX Designer", "Brand Designer"])
  heading: "",           // main CTA heading
  description: "",       // CTA description text
  buttonText: "",        // primary button text
  buttonHref: "",        // primary button href (e.g. "mailto:...")
  email: "",             // email address displayed as secondary link
  backgroundImage: "",   // path to CTA background image
}
```

### footerConfig
```ts
{
  logo: "",                    // footer logo text
  description: "",             // footer brand description
  columns: [],                 // array of { title: string, links: { label: string, href: string }[] }
  socialLinks: [],             // array of { iconName: string, href: string, label: string }
  newsletterHeading: "",       // newsletter section heading
  newsletterDescription: "",   // newsletter description
  newsletterButtonText: "",    // subscribe button text
  newsletterPlaceholder: "",   // email input placeholder
  copyright: "",               // copyright line text
  credit: "",                  // credit/attribution line text
}
```
Social icon names use Lucide PascalCase: `Dribbble`, `Twitter`, `Linkedin`, `Instagram`, `Github`, `Facebook`, `Youtube`, etc.

## Required Images

Place all images in `public/images/`:

| Image | Usage | Recommended Size |
|-------|-------|-----------------|
| `hero-bg.jpg` | Hero background (full-screen) — should be a portrait/people photography for best effect with the mouse-tracking reveal | 1920x1080+ |
| `about-1.jpg` to `about-4.jpg` | About section 2x2 grid | 800x1000 (4:5) |
| `service-1.jpg` to `service-4.jpg` | Service card hover previews | 640x400 |
| `portfolio-1.jpg` to `portfolio-5.jpg` | Portfolio project thumbnails | 1200x900 (4:3) |
| `testimonial-1.jpg` to `testimonial-3.jpg` | Testimonial author photos | 600x750 (4:5) |
| `cta-bg.jpg` | CTA section background | 1920x1080+ |

## Design System

- **Colors**: Black (#131313), White (#FFFFFF), Base Black (#1D1D1D), Subtle (#EAEAEA), Border (#EFEFF2), Blue (#0082F3), Focus (#4D65FF)
- **Typography**: Geist font (400 regular, 500 medium), GeistMono (500 medium) for labels and monospace
- **Easing**: Custom timing functions - out-quad, out-cubic, out-quart, out-circ, in-out-quad
- **Layout**: Full-width sections, container-large (80rem max), responsive grid layouts

## Notes

- The hero section has a unique mouse-tracking effect: a sharp image window follows the cursor over a blurred background, with a white border frame and crosshair
- All animations use CSS transitions and Intersection Observer (no animation libraries)
- Services section uses mouse parallax for hover image positioning
- Testimonials auto-advance every 6 seconds with slide transitions
- The page overlay shows during initial load and fades out after content is ready
- Navigation transitions from transparent to white background on scroll
- Icons in services and footer are dynamically resolved from Lucide by PascalCase name string
- Portfolio grid uses a bento-style layout with the first project spanning 2 columns and the last spanning full width
