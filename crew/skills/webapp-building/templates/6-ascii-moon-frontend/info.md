# 6 Ascii Moon Frontend Template

A monochrome lunar observatory template with a split hero, interactive ASCII moon field, manifesto video/text split, facilities directory with detail pages, live observation feed, and archive carousel.

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## How To Fill This Template

All editable content is in `src/config.ts`. Do not modify the ASCII hero logic, the archive carousel logic, or the facility detail routing unless there is a real bug.

This template is well suited for:

- Lunar or orbital observatories
- Sci-fi research institutions
- Speculative telemetry archives
- Experimental archive interfaces
- Monochrome narrative landing pages

## Config Objects

### `siteConfig`

```ts
export const siteConfig = {
  language: "",
  siteTitle: "",
  siteDescription: "",
}
```

Constraints:

- `siteTitle`: keep under ~60 characters
- `siteDescription`: keep under ~160 characters

### `navigationConfig`

```ts
export const navigationConfig = {
  brandName: "",
  links: [
    // { label: "", href: "#archives" }
  ],
}
```

Constraints:

- `brandName`: best as one short word
- `links`: 2-4 links recommended
- Keep labels short and uppercase-friendly

### `heroConfig`

```ts
export const heroConfig = {
  eyebrow: "",
  titleLines: [],
  leadText: "",
  supportingNotes: [],
}
```

Constraints:

- `eyebrow`: one short system-style line
- `titleLines`: 2-3 lines only
- `leadText`: one lead paragraph
- `supportingNotes`: use exactly 3 items for the current staggered layout
- Keep each note concise so the left panel composition remains clean

### `manifestoConfig`

```ts
export const manifestoConfig = {
  videoPath: "",
  text: "",
}
```

Constraints:

- `text`: one paragraph only
- `videoPath`: use a 16:9 video

### `facilitiesConfig`

```ts
export const facilitiesConfig = {
  sectionLabel: "",
  detailBackText: "",
  detailNotFoundText: "",
  detailReturnText: "",
  items: [
    {
      slug: "",
      name: "",
      code: "",
      address: "",
      status: "",
      email: "",
      phone: "",
      ctaText: "",
      ctaHref: "",
      image: "",
      utcOffset: 0,
      article: {
        title: "",
        paragraphs: [],
      },
    },
  ],
}
```

Constraints:

- `items`: 3-6 items recommended; 4 fits the current design best
- `slug`: must be unique and URL-safe
- `name`: short uppercase-compatible identifier
- `code`: short station code or blank
- Use either `address` or `status` for each item, or both
- `article.paragraphs`: 2-4 paragraphs recommended
- Facility images should be portrait-oriented

### `observationConfig`

```ts
export const observationConfig = {
  sectionLabel: "",
  videoPath: "",
  statusText: "",
  latLabel: "",
  lonLabel: "",
  initialLat: 0,
  initialLon: 0,
}
```

Constraints:

- `statusText`: short uppercase line
- `videoPath`: use a 16:9 video
- `latLabel` / `lonLabel`: keep short

### `archivesConfig`

```ts
export const archivesConfig = {
  sectionLabel: "",
  vaultTitle: "",
  closeText: "",
  items: [
    {
      src: "",
      label: "",
    },
  ],
}
```

Constraints:

- `items`: 4 images is ideal for the current carousel and preview layout
- `vaultTitle`: must fit inside the centered button
- `label`: short archival caption

### `footerConfig`

```ts
export const footerConfig = {
  copyrightText: "",
  statusText: "",
}
```

Constraints:

- Both lines should stay short

## Required Images

Place all images in `public/images/`.

If the required image assets do not already exist, write image-generation prompts based on the user's request and this template's visual style, call the `generate_image` tool, save the generated files into `public/images/`, and then reference those final file paths in `src/config.ts`.

### Facilities

- One image per facility item in `facilitiesConfig.items`
- Recommended ratio: portrait, ideally 3:4
- Recommended minimum size: 1200 x 1600
- Style: stark, monochrome, infrastructural, scientific, archival

### Archives

- One image per archive item in `archivesConfig.items`
- Recommended ratio: portrait or near-portrait
- Recommended minimum size: 1200px on the short edge
- Style: celestial, archival, grayscale-friendly

## Required Videos

Place all videos in `public/videos/`.

If the required video assets do not already exist, write video-generation prompts based on the user's request and this template's visual style, call the `generate_video` tool, save the generated files into `public/videos/`, and then reference those final file paths in `src/config.ts`.

### Manifesto

- 1 video used by `manifestoConfig.videoPath`
- Recommended ratio: 16:9

### Observation Feed

- 1 video used by `observationConfig.videoPath`
- Recommended ratio: 16:9

## Design Notes

- The hero is split 40/60: editorial left panel and ASCII moon field right panel
- The left hero panel uses `Geist Pixel` for the main title and `IBM Plex Mono` for the supporting text
- The site is intentionally monochrome and should stay restrained rather than decorative
- Facility detail pages reuse the same source data as the facilities grid
- Archive images are shown in grayscale, so composition matters more than color

## Technical Notes

- Facility detail pages are client-side routes
- Static hosting should support SPA fallback to `index.html` so `/facility/:slug` URLs resolve correctly
