---
name: pdf
description: Create and process PDF files. Creation covers reports, papers, and documents
  with charts, tables, math formulas, and code listings. Processing covers text/table
  extraction, form filling, and page operations (merge, split, rotate, crop). Use
  when the user mentions PDF, .pdf, or asks to extract/merge/fill PDF files.
metadata:
  zh_name: PDF处理
  zh_description: 创建和处理PDF文件，支持生成报告、论文、图表、表格、公式和代码文档，处理包括文本提取、表单填写、页面合并、分割、旋转和裁剪等操作。
  query_examples:
  - 帮我创建一个包含图表的PDF报告
  - 从这份PDF里提取表格数据
  - 把这两个PDF合并成一个文件
  skillCategoryName: 通用办公
---
## Route Selection

| Route | Trigger | Route File |
|-------|---------|------------|
| **ReportLab** (default) | All PDF creation requests | `routes/reportlab.md` |
| **md2pdf** | Convert swarm-assembled Markdown to PDF (gated, see below) | `routes/md2pdf.md` |
| **Process** | Work with existing PDFs (extract, merge, fill forms, etc.) | `routes/process.md` |

**Output path:** write to the path the user requested. If its directory is not
writable (e.g. a container path like `/mnt/...` that does not exist here), fall
back to `./outputs/` under the current working directory, create it, and report
the final path. Never fail just because a hardcoded absolute directory is missing.

**When to use `md2pdf` (gate):** only for a multi-section Markdown report
assembled by a swarm writing skill — i.e. there is a `*.agent.final.md` or
several `*_sec{NN}.md` files produced by sub-agents, with standard Markdown
footnotes (`[^id]` + `[^id]: Title. Date. URL`) for citations. For a one-off PDF
the user asks you to write, author it natively via the **ReportLab** route;
hand-authored layout is cleaner than converted Markdown.

### MANDATORY: Read Route File Before Implementation

**Before implementation, you MUST:**
1. Determine the route (ReportLab / md2pdf / Process)
2. **Read the route file** (`routes/reportlab.md`, `routes/md2pdf.md`, or `routes/process.md`)
3. Only then proceed with implementation

This file (SKILL.md) contains constraints and principles. Route files contain **how-to details**.

When route files use `{skill_path}`, resolve it in Kimi CLI as:

```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
```

Then replace `{skill_path}` with `"$PDF_SKILL_DIR"` in commands.

### Decision Rules

| User Says | Route |
|-----------|-------|
| "Create a PDF", "Make a report", "Write a paper" | ReportLab |
| "Extract text from PDF", "Merge these PDFs", "Fill this form" | Process |

---

## Dependencies

| Route | Libraries |
|-------|-----------|
| **ReportLab** | `pip install reportlab matplotlib` |
| **md2pdf** | `pip install markdown2 xhtml2pdf` (pure Python, reuses ReportLab) |
| **Process** | `pip install pikepdf pdfplumber` |
| **Visual QA** | Managed `pypdfium2` + `Pillow` |

---

## Core Constraints (Must Follow)

### 1. Output Language
**Output language must match user's query language.**
- User writes in Chinese → PDF content in Chinese
- User writes in English → PDF content in English
- User explicitly specifies language → Follow user's specification

### 2. Word Count and Page Constraints

| User Request | Execution Standard |
|--------------|-------------------|
| Specific word count (e.g., "3000 words") | Within ±20% |
| Specific page count (e.g., "5 pages") | Exactly equal, last page may be partial |
| Word count range (e.g., "2000-3000 words") | Must fall within range |
| No explicit requirement | Infer by document type; prefer thorough over superficial |
| Minimum specified (e.g., "more than 5000 words") | No more than 2x |

**Prohibited**:
- Padding pages with excessive bullet lists
- Exceeding twice the user's requested length

**Special case — Resume/CV**: 1 page unless user specifies otherwise.

### 3. Citation Standards

#### Search Before Writing
**DO NOT fabricate information.** When content involves statistics, policies, research, or anything you are not certain about — **search first**.

#### Citations Must Be Real
All citations must have correct author/institution names, accurate titles, and verifiable year/source. **Fabricating references is prohibited.**

#### Citation Format

| Document Language | Format |
|-------------------|--------|
| Chinese | GB/T 7714 |
| English | APA |
| Mixed | Chinese refs → GB/T 7714, English refs → APA |

### 4. Outline Adherence

- **User provided outline**: Follow strictly; do not add/remove/reorder sections
- **No outline**: Standard structure by document type:
  - Academic: Introduction → Methods → Results → Discussion → Conclusion
  - Business: Executive Summary → Analysis → Recommendations
  - Technical: Overview → Principles → Usage → Examples → FAQ

### 5. Mandatory Visual QA for Created PDFs

After every PDF creation or layout-changing edit, render every page and create a contact sheet:

```bash
python "{skill_path}/scripts/pdf.py" inspect /absolute/path/final.pdf -o /absolute/path/pdf-qa
```

Review the contact sheet and every reported `low_content_page`. A warning may be intentional for a cover or divider, but it must be inspected rather than ignored. Fix unexplained blank/near-blank pages, orphan lines, clipping, overlaps, and broken glyphs, then rerun `inspect`. Do not claim visual success from file-open, page-count, or structural checks alone.

---

## Tech Stack

| Library | Purpose | License |
|---------|---------|---------|
| reportlab | PDF creation (layouts, tables, charts, fonts) | BSD |
| matplotlib | Chart images for PDF insertion | PSF |
| pikepdf | Form filling, page operations, metadata | MPL-2.0 |
| pdfplumber | Text and table extraction | MIT |
| pypdfium2 | Render every page for visual QA | Apache-2.0 / BSD-3-Clause |
| Pillow | Page metrics and contact-sheet assembly | MIT-CMU |
