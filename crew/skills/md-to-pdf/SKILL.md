---
name: md-to-pdf
description: Convert Markdown documents to PDF format. Use when the user needs to
  transform .md files into .pdf, including documentation export, report generation,
  or document sharing. Supports multiple conversion methods from simple to advanced
  formatting needs.
metadata:
  zh_name: Markdown转PDF
  zh_description: 将 Markdown 文档转换为 PDF 格式，适用于文档导出、报告生成或文档共享场景，支持多种转换方式。
  query_examples:
  - 把这个 Markdown 文件转成 PDF
  - 如何把 .md 文档导出为 PDF
  - 帮我把文档转换成 PDF 格式
  skillCategoryName: 通用办公
---
# Markdown to PDF Conversion

Convert Markdown documents to PDF with various formatting options.

## Quick Start

### Method 1: Pandoc (Recommended for Complex Documents)

**Requirements**: pandoc + LaTeX

```bash
# Ubuntu/Debian
sudo apt-get install pandoc texlive-latex-base texlive-fonts-recommended

# macOS
brew install pandoc basictex
```

**Basic conversion**:
```bash
pandoc input.md -o output.pdf
```

**With Chinese support**:
```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex -V CJKmainfont="Noto Sans CJK SC"
```

**With custom CSS (via HTML)**:
```bash
pandoc input.md -o output.pdf --css=style.css
```

### Method 2: Python Script (No LaTeX Required)

Use the bundled script for lightweight conversion:

```bash
python scripts/md2pdf.py input.md output.pdf
```

**Features**:
- No LaTeX installation needed
- Basic markdown support
- Faster for simple documents

### Method 3: Playwright/Chromium (Best Rendering)

For pixel-perfect control:

```bash
# Convert to HTML first
pandoc input.md -o temp.html --standalone

# Print to PDF with Chromium
npx playwright-core chromium --headless --print-to-pdf=output.pdf temp.html
```

## Method Selection Guide

| Need | Method | Why |
|------|--------|-----|
| Chinese/Asian languages | Pandoc + xelatex | Font support |
| Math equations | Pandoc + LaTeX | KaTeX/MathJax rendering |
| Code highlighting | Pandoc | Pygments integration |
| Quick & simple | Python script | Fast, no deps |
| Custom styling | Playwright | Full CSS control |
| Batch processing | Python script | Easy to automate |

## Common Options

### Pandoc Options

```bash
# Table of contents
pandoc input.md -o output.pdf --toc

# Page size
pandoc input.md -o output.pdf -V geometry:margin=1in

# Template
pandoc input.md -o output.pdf --template=eisvogel

# Syntax highlighting
pandoc input.md -o output.pdf --highlight-style=tango
```

### Python Script Usage

```bash
# Basic
python scripts/md2pdf.py document.md

# Custom output
python scripts/md2pdf.py document.md reports/weekly.pdf
```

## Troubleshooting

**Chinese characters showing as squares?**
- Use xelatex engine with CJK font
- Install fonts: `sudo apt-get install fonts-noto-cjk`

**LaTeX not found?**
- Install: `sudo apt-get install texlive-full` (large) or `texlive-latex-base` (minimal)

**Images not appearing?**
- Use absolute paths or ensure images are in working directory
- Check image format (PNG/JPG work best)

**Code blocks formatting issues?**
- Use fenced code blocks with language tags
- Add `--listings` flag for LaTeX-style code

## Advanced: Custom Templates

Create a custom LaTeX template for consistent branding:

```yaml
# frontmatter.md
---
title: "My Report"
author: "Your Name"
date: "\\today"
geometry: margin=2cm
fontsize: 11pt
---
```

```bash
pandoc frontmatter.md input.md -o output.pdf --template=custom.tex
```