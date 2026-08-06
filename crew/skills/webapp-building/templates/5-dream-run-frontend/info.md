# 5 Gird Sun Frontend Template

A cinematic exhibition archive template with a procedural hero shader, current exhibitions list, linked exhibition detail pages, manifesto with side video, and a minimal curatorial footer.

## Language
If the user has not specified a language of the website, then the language of the website (the content you insert into the template) must match the language of the user's query.
If the user has specified a language of the website, then the language of the website must match the user's requirement.

## Content
The actual content of the website should match the user's query.

## How To Fill This Template

All editable content is in `src/config.ts`. Do not modify shader logic or animation logic unless there is a real bug.

This template works well for:

- Exhibition archives
- Digital art institutions
- Curatorial platforms
- Speculative architectural showcases
- Conceptual gallery presentations

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
    // { label: "", href: "#exhibitions" }
  ],
}
```

Constraints:

- `brandName`: short and strong, ideally 1-3 words
- `links`: 2-4 links recommended
- `href`: should target valid section ids such as `#exhibitions`, `#pavilions`, `#contact`

### `heroConfig`

```ts
export const heroConfig = {
  titleLines: [],
  subtitle: "",
}
```

Constraints:

- `titleLines`: 1-3 lines only
- Each line should remain concise to preserve the monumental hero composition
- `subtitle`: keep short; uppercase styling works best

### `manifestoConfig`

```ts
export const manifestoConfig = {
  headingText: "",
  bodyText: "",
  videoPath: "",
}
```

Constraints:

- `headingText`: best under ~40 characters
- `bodyText`: one paragraph only; around 25-60 English words is a good range
- `videoPath`: use a 16:9 video for the current right-side layout

### `exhibitionsConfig`

```ts
export const exhibitionsConfig = {
  sectionLabel: "",
  countLabel: "",
  detailBackText: "",
  items: [
    {
      slug: "",
      title: "",
      image: "",
      year: "",
      eyebrow: "",
      intro: "",
      sections: [
        // { heading: "", body: "" }
      ],
    },
  ],
}
```

Constraints:

- `items`: 3-8 items recommended
- `slug`: must be unique and URL-safe
- `title`: keep under ~30 characters for clean list layout
- `image`: one image per exhibition
- `intro`: one concise editorial paragraph
- `sections`: 2-4 article sections recommended
- Each `sections[].heading`: short and clear
- Each `sections[].body`: around 60-140 English words or equivalent in the target language

### `pavilionsConfig`

```ts
export const pavilionsConfig = {
  sectionLabel: "",
  videos: [
    {
      src: "",
      caption: "",
    },
  ],
}
```

Constraints:

- 1-2 videos recommended
- `caption`: keep concise
- Videos should feel cinematic and stable rather than fast-cut or chaotic

### `footerConfig`

```ts
export const footerConfig = {
  visitLabel: "",
  visitLines: [],
  connectLabel: "",
  connectLinks: [
    // { label: "", href: "" }
  ],
  brandName: "",
  rightsText: "",
  coordinatesText: "",
}
```

Constraints:

- `visitLines`: 2-5 lines recommended
- `connectLinks`: 2-4 links recommended
- `brandName`: short, large-format wordmark
- `rightsText`: one short line
- `coordinatesText`: one short line

## Required Images

Place all images in `public/images/`.

If the required image assets do not already exist, write image-generation prompts based on the user's request and this template's visual style, call the `generate_image` tool, save the generated files into `public/images/`, and then reference those final file paths in `src/config.ts`.

- One image per exhibition item in `exhibitionsConfig.items`
- Recommended aspect ratio: portrait or near-portrait, ideally 4:5
- Minimum recommended size: 1200px on the short edge

## Required Videos

Place all videos in `public/videos/`.

If the required video assets do not already exist, write video-generation prompts based on the user's request and this template's visual style, call the `generate_video` tool, save the generated files into `public/videos/`, and then reference those final file paths in `src/config.ts`.

- Optional manifesto video used by `manifestoConfig.videoPath`
- One or more pavilion videos used by `pavilionsConfig.videos`
- Recommended aspect ratio: 16:9

## Design Notes

- The hero shader is part of the layout identity and remains fixed in code
- Exhibition titles should feel monumental and editorial
- The detail pages are image-led and article-driven, so use thoughtful but not overlong text
- The site tone works best when it feels curatorial, mysterious, and composed

## Technical Notes

- Exhibition detail pages are generated from the same data source as the exhibition list
- The template uses client-side pathname routing for detail pages
- Static hosting should rewrite unknown paths to `index.html` so exhibition detail URLs continue to work
