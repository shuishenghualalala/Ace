# PDF Processing Route

Process existing PDFs using Python CLI tools (pikepdf + pdfplumber).

---

## Step 0: Install Dependencies

```bash
pip install pikepdf pdfplumber
```

---

## Command Reference

```
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" <command> <subcommand> [options]
```

| Command | Description |
|---------|-------------|
| `form info <pdf>` | View form fields |
| `form fill <pdf> -o <out> -d <json>` | Fill form fields |
| `extract text <pdf> [-p pages]` | Extract text |
| `extract table <pdf> [-p pages]` | Extract tables |
| `extract image <pdf> -o <dir>` | Extract images |
| `pages merge <pdf>... -o <out>` | Merge PDFs |
| `pages split <pdf> -o <dir>` | Split into single pages |
| `pages rotate <pdf> <90|180|270> -o <out>` | Rotate pages |
| `pages crop <pdf> <l,b,r,t> -o <out>` | Crop pages |
| `meta get <pdf>` | Read metadata |
| `meta set <pdf> -o <out> -d <json>` | Set metadata |

## Output Format

All commands output JSON:
```json
// Success
{"status": "success", "data": {...}}

// Error (to stderr)
{"status": "error", "error": "ErrorType", "message": "Description", "hint": "Suggestion"}
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Argument error |
| 2 | File not found |
| 3 | PDF parse error |
| 4 | Operation failed |

---

## Form Filling Workflow

**Step 1: Check form fields**
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" form info input.pdf
```

Output example:
```json
{
  "status": "success",
  "data": {
    "has_fields": true,
    "count": 5,
    "fields": [
      {"id": "name", "type": "text", "page": 1},
      {"id": "agree", "type": "checkbox", "states": ["/Yes", "/Off"], "checked_value": "/Yes", "page": 1},
      {"id": "country", "type": "dropdown", "options": [{"value": "US", "label": "US"}, {"value": "CN", "label": "CN"}], "page": 1}
    ]
  }
}
```

**Step 2: Fill form**
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" form fill input.pdf -o output.pdf -d '{"name": "John", "agree": "true", "country": "US"}'
```

### Field Value Rules

| Field Type | Value Format | Example |
|------------|--------------|---------|
| text | Any string | `"name": "John Doe"` |
| checkbox | `"true"` or `"false"` | `"agree": "true"` |
| radio | Option value from `options` | `"gender": "/Choice1"` |
| dropdown | Option value from `options` | `"country": "US"` |

**Important**: For checkbox fields, use `"true"` or `"false"` as string values. The script automatically converts them to the correct PDF values (`/Yes`, `/On`, `/Off`, etc.).

---

## Text and Table Extraction

**Extract text**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" extract text document.pdf
python3 "$PDF_SKILL_DIR/scripts/pdf.py" extract text document.pdf -p 1-3    # Pages 1-3 only
python3 "$PDF_SKILL_DIR/scripts/pdf.py" extract text document.pdf -p 1,3,5  # Specific pages
```

**Extract tables**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" extract table document.pdf
```

Output includes structured table data:
```json
{
  "total_pages": 10,
  "extracted_pages": 10,
  "total_tables": 3,
  "tables": [
    {
      "page": 1,
      "table_index": 0,
      "rows": 5,
      "cols": 3,
      "data": [["Header1", "Header2", "Header3"], ["A", "B", "C"], ...]
    }
  ]
}
```

---

## Page Operations

**Merge PDFs**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" pages merge a.pdf b.pdf c.pdf -o merged.pdf
```

**Split PDF**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" pages split document.pdf -o ./output_dir/
```

**Rotate pages**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" pages rotate document.pdf 90 -o rotated.pdf
python3 "$PDF_SKILL_DIR/scripts/pdf.py" pages rotate document.pdf 180 -o rotated.pdf -p 1-3  # Specific pages
```

**Crop pages**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" pages crop document.pdf 50,50,550,750 -o cropped.pdf
```
Box format: `left,bottom,right,top` in points (1 inch = 72 points).

---

## Metadata Operations

**Read metadata**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" meta get document.pdf
```

**Set metadata**:
```bash
PDF_SKILL_DIR="${KIMI_PDF_SKILL_DIR:-$(pwd)/.agents/skills/pdf}"
python3 "$PDF_SKILL_DIR/scripts/pdf.py" meta set document.pdf -o output.pdf -d '{"Title": "My Document", "Author": "John Doe"}'
```

Supported fields: `Title`, `Author`, `Subject`, `Keywords`, `Creator`, `Producer`

---

## Script Reference

| Script | Purpose |
|--------|---------|
| `pdf.py` | Unified CLI entry point for all PDF processing |
| `cmd_form.py` | Form info and fill operations |
| `cmd_extract.py` | Text, table, image extraction |
| `cmd_pages.py` | Merge, split, rotate, crop |
| `cmd_meta.py` | Metadata read/write |

## Tech Stack

| Library | Purpose | License |
|---------|---------|---------|
| pikepdf | Form filling, page operations, metadata | MPL-2.0 |
| pdfplumber | Text and table extraction | MIT |

---

## Important Notes

### Password-Protected PDFs
**Not supported.** The CLI commands do not support encrypted PDFs. If user provides a password-protected PDF, inform them that this feature is not available and suggest they decrypt the PDF first using other tools.

### Large File Handling
| File Size | Expected Behavior |
|-----------|-------------------|
| < 50 MB | Normal processing |
| 50-200 MB | May be slow, 1-2 minutes |
| > 200 MB | Consider splitting first, or increase timeout |

**Memory usage**: Roughly 2-3x file size. For a 100MB PDF, expect ~300MB RAM usage.

### Error Recovery
If a command fails mid-operation:
- **Merge**: Partial output may exist, delete and retry
- **Split**: Some pages may be written, check output directory
- **Form fill**: Original file unchanged (writes to new file)
