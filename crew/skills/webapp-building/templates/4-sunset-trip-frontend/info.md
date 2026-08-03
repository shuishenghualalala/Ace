# 4 Video Travel Frontend Template

A luxury travel and heritage journey website template with a looping hero video, editorial manifesto reveal, pale principles section, membership tier showcase, and refined footer navigation.

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## How To Fill This Template

All editable content is in `src/config.ts`. Do not modify component logic unless there is a real bug.

This template is well suited for:

- Luxury travel clubs
- Heritage rail experiences
- Private journey memberships
- Premium hospitality concepts
- Slow travel editorials

## Config Objects

### `siteConfig`

```ts
export const siteConfig = {
  language: "",         // Language code, e.g. "en", "zh-CN", "fr"
  siteTitle: "",        // Browser tab title
  siteDescription: "",  // Meta description
}
```

Constraints:

- `siteTitle`: keep under ~60 characters
- `siteDescription`: keep under ~160 characters
- `language`: leave empty unless the user explicitly requests a fixed language

### `navigationConfig`

```ts
export const navigationConfig = {
  brandName: "",        // Brand name shown in the nav
  links: [
    // { label: "", target: "#manifesto" }
  ],
}
```

Constraints:

- `brandName`: best at 1-3 short words
- `links`: 3 items is the ideal count for the current header layout
- `label`: keep under ~12 English characters or ~6 Chinese characters
- `target`: should point to existing section ids such as `#manifesto`, `#anatomy`, `#tiers`

### `heroConfig`

```ts
export const heroConfig = {
  videoPath: "",        // Path like "videos/hero.mp4"
  eyebrow: "",          // Small label above the title
  titleLine: "",        // First title line
  titleEmphasis: "",    // Second italic title line
  subtitleLine1: "",    // First subtitle line
  subtitleLine2: "",    // Second subtitle line
  ctaText: "",          // CTA text
  ctaTargetId: "",      // Scroll target like "#tiers"
}
```

Constraints:

- `eyebrow`: keep under ~20 characters
- `titleLine`: keep under ~18 English characters or ~10 Chinese characters
- `titleEmphasis`: keep under ~18 English characters or ~10 Chinese characters
- `subtitleLine1` / `subtitleLine2`: short editorial lines; each should stay under ~55 English characters or ~24 Chinese characters
- `ctaText`: keep under ~20 English characters or ~10 Chinese characters

### `manifestoConfig`

```ts
export const manifestoConfig = {
  sectionLabel: "",     // Small uppercase label
  text: "",             // Main manifesto paragraph
}
```

Constraints:

- `sectionLabel`: keep short
- `text`: one strong paragraph, ideally 45-90 English words or 70-150 Chinese characters
- Because the reveal is word-by-word, overly long text weakens the pacing and will feel slow

### `anatomyConfig`

```ts
export const anatomyConfig = {
  sectionLabel: "",     // Small uppercase label
  title: "",            // Main heading
  pillars: [
    {
      label: "",
      title: "",
      body: "",
    },
  ],
}
```

Constraints:

- `sectionLabel`: keep under ~20 characters
- `title`: keep under ~40 English characters or ~18 Chinese characters
- `pillars`: 3 items recommended
- `label`: short uppercase micro-label
- `title`: keep under ~32 English characters
- `body`: around 60-120 English words or 90-180 Chinese characters for readable column length

### `tiersConfig`

```ts
export const tiersConfig = {
  sectionLabel: "",     // Small uppercase label
  title: "",            // Main heading
  tiers: [
    {
      name: "",
      price: "",
      frequency: "",
      journeys: "",
      image: "",
      description: "",
      amenities: [],
      ctaText: "",
      ctaHref: "",
    },
  ],
}
```

Constraints:

- `tiers`: 2-4 items recommended; 3 is ideal for this layout
- `name`: keep short, ideally 1-3 words
- `price`: numeric or short price string only; long pricing notes will break the line
- `frequency`: short qualifier such as "per annum"
- `journeys`: short label, best under ~25 characters
- `description`: 30-70 English words or 50-120 Chinese characters
- `amenities`: 4-7 items recommended
- Each amenity: one concise line
- `image`: should use a 4:3 composition

### `footerConfig`

```ts
export const footerConfig = {
  ageGateText: "",
  brandName: "",
  brandTaglineLines: [],
  columns: [
    {
      heading: "",
      links: [
        // { label: "", href: "" }
      ],
    },
  ],
  copyright: "",
}
```

Constraints:

- `ageGateText`: one short italic statement
- `brandName`: 1-3 short words
- `brandTaglineLines`: 1-3 short lines
- `columns`: 2-4 columns recommended
- Each column: 3-5 links works best
- `copyright`: keep to one line

## Required Images

Place all images in `public/images/`.

If the required image assets do not already exist, write image-generation prompts based on the user's request and this template's visual style, call the `generate_image` tool, save the generated files into `public/images/`, and then reference those final file paths in `src/config.ts`.

For the membership section:

- `images/tier-01.jpg`
- `images/tier-02.jpg`
- `images/tier-03.jpg`

Recommended specs:

- Aspect ratio: 4:3
- Resolution: at least 1200x900
- Style: premium interiors, trains, landscapes, dining cars, heritage details, quiet cinematic travel

## Required Video

Place all videos in `public/videos/`.

If the required video asset does not already exist, write a video-generation prompt based on the user's request and this template's visual style, call the `generate_video` tool, save the generated file into `public/videos/`, and then reference that final file path in `src/config.ts`.

- `videos/hero.mp4`

Recommended specs:

- Resolution: 1920x1080 or above
- Calm, elegant motion suitable for continuous looping
- Avoid overly fast cuts; the hero card sits over the footage and needs visual stability

## Design Notes

- The site alternates between deep brown-black and pale parchment tones
- Navigation text switches automatically for dark and light sections, so keep section ids intact
- The manifesto reveal is tuned for one paragraph, not multiple stacked paragraphs
- The membership section depends on strong photography for visual hierarchy
- The overall tone should feel exclusive, patient, and editorial rather than loud or adventurous

## Motion Notes

- The hero content uses entrance blur/fade animation
- The manifesto text reveals word-by-word on scroll
- The principles section uses a sticky 3D helix visual with scrolling text blocks
- Membership rows animate text content on entry
- Do not remove section ids unless you also update the navigation targets
