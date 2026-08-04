# md2pdf Route — Markdown → PDF with citations

Convert a swarm-assembled Markdown report (standard Markdown footnotes) into a
PDF whose citations render as in-text superscript links into a consolidated
**References** section at the end.

Pure-Python stack: `markdown2` → HTML → `xhtml2pdf` (built on ReportLab). No
Pandoc, typst, headless browser, or TeX required.

## When to Use (gate)

Use **only** for multi-section Markdown assembled from several
`*_sec{NN}.md` sections into a final report such as `*.agent.final.md`.

For a one-off PDF the user asks you to write, use the **ReportLab** route
(`routes/reportlab.md`) — hand-authored layout is cleaner than converted
Markdown. This route is the normalization exit for assembled Markdown.

## Quick Start

```bash
python3 {skill_path}/scripts/pdf.py md2pdf <file.md> -o <out.pdf>
# Chinese (or other-language) references heading:
python3 {skill_path}/scripts/pdf.py md2pdf <file.md> -o <out.pdf> --references-heading 参考文献
# explicit CJK font (if auto-detect misses):
python3 {skill_path}/scripts/pdf.py md2pdf <file.md> -o <out.pdf> --font "/path/to/cjk.ttf"
```

Check a file's citations before converting:

```bash
python3 {skill_path}/scripts/md2pdf/citation_md.py --check <file.md>
```

## Citation Input Format

Standard Markdown footnotes; the model writes source metadata inline (no
external citation database):

```markdown
正文断言[^kimi-k26]，再次引用同源[^kimi-k26]。另一来源[^foundry]。

[^kimi-k26]: Introducing Kimi K2.6 in Microsoft Foundry. 2026-04-22. https://techcommunity.microsoft.com/blog/.../4513125
[^foundry]: Azure AI Foundry Overview. 2026-01-01. https://learn.microsoft.com/foundry
```

- Reference: `[^id]`. Definition: `[^id]: <text containing the URL>`.
- The `id` is a handle; reuse the same id for the same source. The **URL is the
  dedup key**. Numbers `[1] [2] …` are assigned automatically by first
  appearance after URL dedup — you never write numbers yourself.

## Pipeline

```
Markdown (standard footnotes)
    ├─ 1. Parse definitions → {id: text, url}
    ├─ 2. Dedup by URL; renumber by appearance
    ├─ 3. Body [^id] → <sup>[N]</sup> anchor link; strip definitions;
    │       append "References" <ol> with #ref-N anchors
    ├─ 4. markdown2 → HTML (tables, fenced code, header ids)
    ├─ 5. Wrap with CSS (A4, CJK font auto-detected)
    └─ 6. xhtml2pdf → PDF
```

## CJK Fonts

The converter auto-detects a CJK-capable font (prefers `.ttf`): explicit
`--font`, then Daimon's `DAIMON_CJK_FONT_REGULAR` env var, then the legacy
`KIMI_CJK_FONT` env var, then common system paths, then `fc-list :lang=zh`.
Normal Daimon Bash sessions should use the managed environment variable and
must not download fonts into the task directory. If no font is found, repair
the runtime or pass an explicit compatible `.ttf`.

## Fidelity Notes

xhtml2pdf supports a practical subset of HTML/CSS (paragraphs, headings,
tables, lists, code blocks, images, links, page setup). Complex CSS layout
(flex/grid, floats) is not supported — this is the intended trade-off for a
zero-system-dependency, high-compatibility converter, matching the skill's
"incomplete features, high compatibility" positioning.

## Dependencies

`pip install markdown2 xhtml2pdf` (both pure Python; xhtml2pdf reuses the
ReportLab already required by the ReportLab route).

## Code Structure

```
scripts/md2pdf/
├── md2pdf_convert.py   # Main pipeline: preprocess → markdown2 → xhtml2pdf
└── citation_md.py      # Standard-footnote parser, URL dedup, superscript anchors + References
```
