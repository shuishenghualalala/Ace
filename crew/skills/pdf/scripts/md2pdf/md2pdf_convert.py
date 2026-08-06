#!/usr/bin/env python3
"""
Markdown → PDF conversion pipeline (standard footnotes, pure Python).

Pipeline: load → footnote preprocessing (parse + URL dedup → superscript anchors
          + References section) → markdown2 (HTML) → wrap with CSS → xhtml2pdf → PDF

Pure-Python stack (markdown2 + xhtml2pdf, the latter built on ReportLab); no
Pandoc / typst / headless browser / TeX required. Citations render as in-text
superscript links into a consolidated "References" section at the document end.

This is the normalization exit for swarm-assembled Markdown. For one-off PDFs the
user asks you to write, author natively via the ReportLab route instead.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import markdown2
from citation_md import convert_citations_for_pdf
from xhtml2pdf import pisa

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

MD_EXTRAS = [
    'tables', 'fenced-code-blocks', 'header-ids', 'strike',
    'cuddled-lists', 'code-friendly',
]

# CJK .ttf candidates (xhtml2pdf handles .ttf most reliably; .ttc is a fallback).
_TTF_CANDIDATES = [
    '/Library/Fonts/Arial Unicode.ttf',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/truetype/arphic/uming.ttc',
    'C:\\Windows\\Fonts\\msyh.ttc',
    'C:\\Windows\\Fonts\\simsun.ttc',
    '/System/Library/Fonts/STHeiti Light.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
]


def find_cjk_font(explicit: str = None) -> str:
    """Locate a CJK-capable font file. Order: explicit arg, env, candidates, fc-list."""
    for cand in (
        explicit,
        os.environ.get('DAIMON_CJK_FONT_REGULAR'),
        os.environ.get('KIMI_CJK_FONT'),
    ):
        if cand and Path(cand).exists():
            return cand

    for cand in _TTF_CANDIDATES:
        if Path(cand).exists():
            return cand

    # fc-list (Linux/macOS with fontconfig): prefer .ttf over .ttc
    try:
        out = subprocess.run(['fc-list', ':lang=zh', 'file'],
                             capture_output=True, text=True, timeout=10).stdout
        paths = [ln.split(':', 1)[0].strip() for ln in out.splitlines() if ln.strip()]
        ttf = [p for p in paths if p.lower().endswith('.ttf') and Path(p).exists()]
        if ttf:
            return ttf[0]
        ttc = [p for p in paths if p.lower().endswith('.ttc') and Path(p).exists()]
        if ttc:
            return ttc[0]
    except Exception:
        pass

    return ''


def _build_css(font_path: str) -> str:
    """CSS template: page setup, CJK font, headings, tables, code, references."""
    font_face = ''
    body_family = 'sans-serif'
    if font_path:
        uri = Path(font_path).as_uri()
        font_face = (
            '@font-face { font-family: "CJK"; '
            f'src: url("{uri}"); }}\n'
        )
        body_family = '"CJK", sans-serif'
    # When a CJK font is registered, use it for code too so CJK comments inside
    # code blocks render (a bare monospace font has no CJK glyphs → tofu). This
    # trades fixed-width for CJK correctness — the right call for CJK documents.
    code_family = '"CJK", monospace' if font_path else 'monospace'
    return f"""
{font_face}
@page {{ size: A4; margin: 2.2cm 2cm; }}
body {{ font-family: {body_family}; font-size: 11pt; line-height: 1.55; color: #1a1a1a; }}
h1 {{ font-size: 20pt; margin: 0 0 12pt; }}
h2 {{ font-size: 15pt; margin: 16pt 0 8pt; border-bottom: 1px solid #ccc; padding-bottom: 3pt; }}
h3 {{ font-size: 12.5pt; margin: 12pt 0 6pt; }}
p {{ margin: 0 0 8pt; text-align: justify; }}
a {{ color: #1a5fb4; text-decoration: none; }}
sup.cite a {{ color: #1a5fb4; }}
table {{ border-collapse: collapse; margin: 8pt 0; width: 100%; }}
th, td {{ border: 1px solid #999; padding: 4pt 6pt; font-size: 10pt; }}
th {{ background: #f0f0f0; }}
pre {{ background: #f6f8fa; border: 1px solid #e1e4e8; padding: 8pt; font-size: 9.5pt; font-family: {code_family}; }}
code {{ font-family: {code_family}; font-size: 9.5pt; }}
h2#references {{ margin-top: 20pt; }}
ol.references {{ font-size: 9.5pt; line-height: 1.4; }}
ol.references li {{ margin-bottom: 4pt; }}
""".strip()


def _link_callback_factory(base_dir: Path):
    """Resolve relative image/resource URIs against the Markdown's directory."""
    def link_callback(uri, rel):
        if uri.startswith('file:'):
            from urllib.parse import unquote, urlparse
            return unquote(urlparse(uri).path)
        if uri.startswith(('http://', 'https://', 'data:')):
            return uri
        p = Path(uri)
        if p.is_absolute() and p.exists():
            return str(p)
        cand = base_dir / uri
        if cand.exists():
            return str(cand)
        return uri
    return link_callback


def convert_md_to_pdf(md_path: str, output_path: str = None, font: str = None,
                      references_heading: str = "References") -> str:
    """Full Markdown → PDF pipeline. Returns the output PDF path."""
    md_path = Path(md_path)
    if not md_path.exists():
        raise FileNotFoundError(f"Markdown file not found: {md_path}")

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = md_path.with_suffix('.pdf')

    # 1. Load (strict UTF-8)
    markdown_content = md_path.read_text(encoding='utf-8')

    # 2. Citation preprocessing: parse footnotes → superscript anchors + References
    transformed, n_cite, warnings = convert_citations_for_pdf(
        markdown_content, references_heading=references_heading)
    for w in warnings:
        logger.warning(w)
    logger.info(f"{n_cite} unique citation(s) after URL dedup")

    # 3. Markdown → HTML
    body_html = markdown2.markdown(transformed, extras=MD_EXTRAS)

    # 4. Wrap with CSS (CJK font auto-detected)
    font_path = find_cjk_font(font)
    if font_path:
        logger.info(f"CJK font: {font_path}")
    else:
        logger.warning("No CJK font found; CJK text may not render. "
                       "Repair the Daimon runtime, set DAIMON_CJK_FONT_REGULAR, or pass --font.")
    full_html = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<style>{_build_css(font_path)}</style></head>'
        f'<body>{body_html}</body></html>'
    )

    # 5. xhtml2pdf → PDF
    with open(output_path, 'wb') as f:
        result = pisa.CreatePDF(
            full_html, dest=f, encoding='utf-8',
            link_callback=_link_callback_factory(md_path.parent),
        )
    if result.err:
        raise RuntimeError(f"xhtml2pdf reported {result.err} error(s)")

    logger.info(f"Done: {output_path}")
    return str(output_path)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Markdown (standard footnotes) → PDF with citations')
    parser.add_argument('md_file', help='Path to Markdown file')
    parser.add_argument('-o', '--output', help='Output PDF path (default: alongside MD)')
    parser.add_argument('--font', help='Path to a CJK-capable .ttf font (overrides auto-detect)')
    parser.add_argument('--references-heading', default='References',
                        help='Heading for the consolidated references section (e.g. 参考文献)')

    args = parser.parse_args()
    try:
        out = convert_md_to_pdf(args.md_file, output_path=args.output, font=args.font,
                                references_heading=args.references_heading)
        print(f"\n{'='*60}\nConversion complete: {out}\n{'='*60}")
    except Exception as e:
        logger.error(f"Conversion failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
