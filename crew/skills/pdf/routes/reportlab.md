# ReportLab PDF Creation Route

Create professional PDFs using pure Python (reportlab).

---

## Step 0: Install Dependencies

```bash
pip install reportlab matplotlib
```

- **reportlab**: PDF generation (layouts, tables, charts, fonts)
- **matplotlib**: Complex charts rendered as images, then inserted

---

## Basic Document Structure

```python
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, PageBreak, KeepTogether
)

doc = SimpleDocTemplate("output.pdf", pagesize=A4,
    topMargin=2.5*cm, bottomMargin=2.5*cm, leftMargin=3*cm, rightMargin=2.5*cm)
styles = getSampleStyleSheet()
story = []
story.append(Paragraph("Title", styles['Title']))
story.append(Paragraph("Body text.", styles['Normal']))
doc.build(story)
```

---

## Chinese Font Registration

ReportLab requires explicit font registration for CJK text. Daimon's Bash environment exposes managed TrueType fonts at `DAIMON_CJK_FONT_REGULAR` and `DAIMON_CJK_FONT_BOLD`; use those deterministic paths instead of scanning system fonts or downloading fonts into the workspace:

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

regular_font = os.environ.get('DAIMON_CJK_FONT_REGULAR')
bold_font = os.environ.get('DAIMON_CJK_FONT_BOLD')
if not regular_font or not bold_font:
    raise RuntimeError(
        'Daimon managed CJK fonts are unavailable; run kimi-daimon runtime repair'
    )
pdfmetrics.registerFont(TTFont('DaimonCJK', regular_font))
pdfmetrics.registerFont(TTFont('DaimonCJK-Bold', bold_font))
pdfmetrics.registerFontFamily(
    'DaimonCJK',
    normal='DaimonCJK',
    bold='DaimonCJK-Bold',
    italic='DaimonCJK',
    boldItalic='DaimonCJK-Bold',
)

# Use in styles
style_cn = ParagraphStyle(
    'Chinese',
    parent=styles['Normal'],
    fontName='DaimonCJK',
    fontSize=12,
    leading=18,
)
story.append(Paragraph("中文内容", style_cn))
```

The managed font variables point to TrueType `glyf` fonts that ReportLab `TTFont` supports. Do not pass Daimon's older CFF/PostScript-outline OTF assets to `TTFont`. Do not copy or download fonts into the task directory.

---

## Page Headers, Footers, and Page Numbers

Use `onPage` / `onLaterPages` callbacks for headers and footers:

```python
from reportlab.lib.pagesizes import A4

def header_footer(canvas, doc):
    canvas.saveState()
    width, height = A4

    # Header
    canvas.setFont('Helvetica', 9)
    canvas.setFillColor(HexColor('#666666'))
    canvas.drawCentredString(width / 2, height - 1.5*cm, "Document Title")
    canvas.line(3*cm, height - 1.7*cm, width - 2.5*cm, height - 1.7*cm)

    # Footer with page number
    canvas.drawCentredString(width / 2, 1.5*cm, f"Page {doc.page}")

    canvas.restoreState()

def first_page(canvas, doc):
    """No header/footer on first page (cover)."""
    pass

doc.build(story, onFirstPage=first_page, onLaterPages=header_footer)
```

For "Page X of Y" format, use a two-pass build or `canvas.drawString` with `NumberedCanvas` (see reportlab docs).

---

## Table of Contents

```python
toc = TableOfContents()
story.append(toc)
story.append(PageBreak())
# Headings must notify TOC via doc.notify('TOCEntry', (level, text, pageNum)) in afterFlowable
```

**Note**: TOC requires `BaseDocTemplate` with `afterFlowable` hook + multi-pass build (`multiBuild`).

---

## Tables

Default to three-line academic style (no vertical lines, no colored backgrounds):

```python
table = Table(data, colWidths=[6*cm, 3*cm, 3*cm])
table.setStyle(TableStyle([
    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ('LINEABOVE', (0, 0), (-1, 0), 1.5, HexColor('#000000')),   # top rule thick
    ('LINEBELOW', (0, 0), (-1, 0), 0.75, HexColor('#000000')),  # header-bottom medium
    ('LINEBELOW', (0, -1), (-1, -1), 1.5, HexColor('#000000')), # bottom rule thick
    ('TOPPADDING', (0, 0), (-1, -1), 6),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
]))
```

---

## Images and Charts

### Insert Image

```python
from reportlab.platypus import Image

img = Image('/path/to/image.png', width=12*cm, height=8*cm)
story.append(img)
```

### Matplotlib Charts

Generate with matplotlib, insert as image:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(['Q1', 'Q2', 'Q3', 'Q4'], [120, 180, 150, 200])
ax.set_title('Quarterly Revenue')
fig.savefig('/tmp/chart.png', dpi=150, bbox_inches='tight')
plt.close()

story.append(Image('/tmp/chart.png', width=14*cm, height=8.4*cm))
```

For built-in ReportLab charts (VerticalBarChart, LinePlot, Pie), use `Drawing` + chart object + `story.append(drawing)`. Prefer matplotlib for complex charts.

---

## Advanced Content

### Math Formulas

ReportLab has no built-in LaTeX math rendering. Use matplotlib to render formulas as images:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(6, 1))
ax.text(0.5, 0.5, r'$E = mc^2$', fontsize=20, ha='center', va='center',
        transform=ax.transAxes)
ax.axis('off')
fig.savefig('/tmp/formula.png', dpi=150, bbox_inches='tight', transparent=True)
plt.close()

story.append(Image('/tmp/formula.png', width=8*cm, height=1.5*cm))
```

For complex multi-line equations, use matplotlib's `mathtext` or render with a local LaTeX installation if available.

### Flowcharts and Diagrams

Use graphviz or matplotlib to generate diagram images, then insert:

```python
# Option 1: graphviz (pip install graphviz)
import graphviz
dot = graphviz.Digraph()
dot.node('A', 'Start')
dot.node('B', 'Process')
dot.node('C', 'End')
dot.edge('A', 'B')
dot.edge('B', 'C')
dot.render('/tmp/flowchart', format='png', cleanup=True)
story.append(Image('/tmp/flowchart.png', width=10*cm))

# Option 2: matplotlib for simple diagrams
# Use matplotlib patches, arrows, and text for custom layouts
```

### Code Highlighting

Use Pygments to syntax-highlight code, then render as styled paragraphs:

```python
from pygments import highlight
from pygments.lexers import PythonLexer
from pygments.formatters import HtmlFormatter

# Pre-highlight code, then render as Paragraph with inline styles
# Or render to image with matplotlib for exact formatting
code = 'def hello():\n    print("world")'
# Simple approach: use Preformatted with monospace font
from reportlab.platypus import Preformatted
from reportlab.lib.styles import ParagraphStyle
code_style = ParagraphStyle('Code', fontName='Courier', fontSize=9,
                            backColor=HexColor('#f5f5f5'), leading=12)
story.append(Preformatted(code, code_style))
```

**CJK in code blocks**: If code contains Chinese comments or strings, the monospace font must have CJK coverage. Register a CJK monospace font (e.g., Noto Sans Mono CJK) or use a registered CJK font as fallback — bare Courier will render Chinese as black rectangles.

---

## Page Breaks and Flow Control

`PageBreak()` for new page. `KeepWithNext(heading)` to prevent orphaned titles. `KeepTogether([heading, table])` to keep groups on same page.

---

## Hyperlinks

```python
# External URL
story.append(Paragraph(
    'Visit <a href="https://example.com" color="blue">Example</a>',
    styles['Normal']
))

# Internal bookmark
story.append(Paragraph(
    '<a name="sec1"/>Section 1: Introduction',
    styles['Heading1']
))
# Later: link to it
story.append(Paragraph(
    'See <a href="#sec1" color="blue">Section 1</a>',
    styles['Normal']
))
```

**Clickable internal links** — TOC entries, in-text citations, cross-references:

```python
# Anchor at chapter heading
Paragraph('<a name="ch1"/>Chapter 1: Introduction', styles['Heading1'])

# TOC entry linking to chapter
Paragraph('<a href="#ch1" color="blue">Chapter 1: Introduction</a>', styles['Normal'])

# In-text citation linking to reference
Paragraph('Deep learning has transformed NLP'
          '<super><a href="#ref1" color="black">[1]</a></super>.', styles['Normal'])

# Reference with anchor
Paragraph('<a name="ref1"/>[1] Devlin et al. BERT. NAACL, 2019.', styles['Normal'])
```

**All references must be clickable.** Every in-text mention of "Figure 1", "Table 2", "Section 3" should be an `<a href="#id">` link to the corresponding element. TOC entries must link to actual sections. Dead references are unacceptable.

---

## Custom Styles and Metadata

Use `styles.add(ParagraphStyle(name, parent, fontName, fontSize, leading, spaceBefore, spaceAfter, textColor))` for custom styles. Set `doc.title`, `doc.author`, `doc.subject` for PDF metadata.

---

## Design and Quality Standards

### Default Style: Professional Academic

Default output should approximate professional academic style, not web/UI style.

**Prohibited UI components** — use professional alternatives:

| Prohibited | Use Instead |
|------------|-------------|
| Card components (bordered + header) | Three-line tables or plain paragraphs |
| Statistics dashboards (number card grids) | Tables for data display |
| Dark title bars | Bold titles + thin underline |
| Timeline components | Numbered lists or tables |
| Rounded borders, shadow effects | Square or no borders, no shadows |
| Emoji / decorative icons | Plain text (emoji fails on Linux servers) |

### Cover Design

If the document warrants a cover, keep it simple — title, subtitle, author, date, centered on page. Use `canvas` in `onFirstPage` for basic geometric decoration (lines, rectangles) if needed. Avoid complex designs that risk looking amateurish.

### Color Standards

Body text: dark gray (`#333`). Backgrounds: light gray (`#f5f5f5`). Low-saturation palette throughout. Pick a color direction based on document scenario (cool grays for tech, navy/burgundy for academic, earth tones for nature). Do not mix warm and cool tones.

### Typography

- **Body**: Serif preferred (Georgia, "Noto Serif SC"). 11pt, leading 1.5-1.6x
- **Headings**: 14-20pt, bold, darker color
- **Captions**: 9pt, gray `#666666`
- **CJK documents**: Register Chinese fonts (see font section above)

### Three-Line Tables (Default Table Style)

All data tables should default to academic three-line style (code example in Tables section above):
- Top rule: 1.5pt black
- Header-bottom rule: 0.75pt black
- Bottom rule: 1.5pt black
- No vertical lines, no colored backgrounds

### Images in Multi-Column Layout

When using Frame-based multi-column layout, image width must not exceed the frame (column) width. Calculate column width as `(page_width - margins - gutter) / num_columns` and set image width accordingly. Images wider than the column will overflow into adjacent columns or off-page.

### Reference / Bibliography Formatting

**Citations are not decoration.** For reports, papers, and any document citing external data:
- ❌ Reference list at the end with zero in-text citation marks — this is fake completeness
- ✅ Every data claim in body text must have a superscript citation `<super>[N]</super>` at point of use
- ✅ Reference list entries must correspond 1:1 with in-text marks
- ✅ All references must be real and verifiable — fabricating citations is prohibited
- Format: Chinese → GB/T 7714; English → APA

**Citation style** (black superscript):
- In-text citations: small font, superscript, black color (not blue)
- Use `<super>` tag in Paragraph: `Paragraph('Text<super>1</super>', style)`

**Hanging indent** for reference list:
```python
ref_style = ParagraphStyle('Reference', fontName='Helvetica', fontSize=10,
                           leftIndent=24, firstLineIndent=-24, leading=14)
story.append(Paragraph('[1] Author. <i>Title</i>. Publisher, 2024.', ref_style))
```

**APA italics**: Book titles and journal names must be italicized with `<i>` tag.

### Two-Column Layout

Use `Frame` objects for multi-column layouts:

```python
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

frame1 = Frame(doc.leftMargin, doc.bottomMargin,
               doc.width/2 - 6, doc.height, id='col1')
frame2 = Frame(doc.leftMargin + doc.width/2 + 6, doc.bottomMargin,
               doc.width/2 - 6, doc.height, id='col2')

doc.addPageTemplates([PageTemplate(id='TwoCol', frames=[frame1, frame2])])
```

### Figure and Table Numbering

For documents with multiple figures/tables:
- Number sequentially: "Figure 1", "Table 1", etc.
- Place captions below figures, above tables
- Use cross-references in body text: "As shown in Figure 1..."

### Layout

- A4 margins: 2.5-3cm sides, 2.5cm top/bottom
- Use `Spacer(1, 12)` between elements for breathing room
- Tables: pad cells with 6pt top/bottom
- `KeepWithNext` on headings to prevent orphaned titles

---

## Script Reference

| Component | Purpose |
|-----------|---------|
| reportlab.platypus | High-level document layout (SimpleDocTemplate, Paragraph, Table, Image) |
| reportlab.lib | Units, colors, page sizes, styles |
| reportlab.graphics | Drawing, built-in charts |
| reportlab.pdfbase | Font registration |
| matplotlib | Complex charts rendered as PNG |

## Mandatory Rendered QA

After writing the final PDF, render every page and generate a contact sheet:

```bash
python "{skill_path}/scripts/pdf.py" inspect /absolute/path/final.pdf -o /absolute/path/pdf-qa
```

Inspect the contact sheet and each page named in `warnings`. Sparse cover/divider pages may be intentional; unexplained low-content pages, orphan lines, clipping, overlaps, or missing glyphs are blockers. Repair the document and rerun this command before delivery.

## Tech Stack

| Library | Purpose | License |
|---------|---------|---------|
| reportlab | PDF generation | BSD |
| matplotlib | Chart images | PSF |
