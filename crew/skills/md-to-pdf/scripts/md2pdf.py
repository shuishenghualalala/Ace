#!/usr/bin/env python3
"""
Markdown to PDF converter - Chinese-friendly version
Multiple fallback methods for maximum compatibility
"""

import sys
import os
import subprocess
from pathlib import Path

# Default CSS for Chinese documents
CHINESE_CSS = """
@page {
    size: A4;
    margin: 2.5cm 2cm;
}
body {
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "SimHei", sans-serif;
    font-size: 11pt;
    line-height: 1.8;
    color: #333;
}
h1 {
    font-size: 20pt;
    font-weight: bold;
    text-align: center;
    margin-bottom: 0.5em;
    color: #1a1a1a;
}
h2 {
    font-size: 14pt;
    font-weight: bold;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
    color: #1a1a1a;
    border-bottom: 1px solid #ddd;
    padding-bottom: 0.3em;
}
h3 {
    font-size: 12pt;
    font-weight: bold;
    margin-top: 1.2em;
    margin-bottom: 0.4em;
    color: #333;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 1em 0;
    font-size: 10pt;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background-color: #f5f5f5;
    font-weight: bold;
}
tr:nth-child(even) {
    background-color: #fafafa;
}
code {
    background-color: #f4f4f4;
    padding: 2px 6px;
    border-radius: 3px;
    font-family: "Consolas", "Monaco", monospace;
    font-size: 0.9em;
}
pre {
    background-color: #f8f8f8;
    padding: 1em;
    border-radius: 5px;
    overflow-x: auto;
    border-left: 3px solid #ddd;
}
blockquote {
    border-left: 4px solid #ddd;
    margin: 1em 0;
    padding-left: 1em;
    color: #666;
}
hr {
    border: none;
    border-top: 1px solid #ddd;
    margin: 2em 0;
}
"""

def check_command(cmd):
    """Check if a command is available"""
    try:
        subprocess.run([cmd, '--version'], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def method_pandoc_xelatex(md_path, pdf_path):
    """Method 1: Pandoc + xelatex (best for Chinese)"""
    if not check_command('pandoc'):
        return False, "pandoc not found"
    if not check_command('xelatex'):
        return False, "xelatex not found"

    try:
        cmd = [
            'pandoc', str(md_path), '-o', str(pdf_path),
            '--pdf-engine=xelatex',
            '-V', 'CJKmainfont=Noto Sans CJK SC',
            '-V', 'geometry:margin=2.5cm',
            '--toc'
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, "pandoc+xelatex"
    except subprocess.CalledProcessError as e:
        return False, f"pandoc failed: {e.stderr}"

def method_pandoc_wkhtmltopdf(md_path, pdf_path):
    """Method 2: Pandoc + wkhtmltopdf (good Chinese support)"""
    if not check_command('pandoc'):
        return False, "pandoc not found"
    if not check_command('wkhtmltopdf'):
        return False, "wkhtmltopdf not found"

    html_path = md_path.with_suffix('.temp.html')
    css_path = md_path.with_suffix('.temp.css')

    try:
        # Convert MD to HTML with table of contents
        cmd_md2html = [
            'pandoc', str(md_path), '-o', str(html_path),
            '--standalone', '--toc',
            '--metadata', f'title={md_path.stem}'
        ]
        subprocess.run(cmd_md2html, check=True, capture_output=True)

        # Create CSS file
        with open(css_path, 'w', encoding='utf-8') as f:
            f.write(CHINESE_CSS)

        # Convert HTML to PDF
        cmd_html2pdf = [
            'wkhtmltopdf',
            '--enable-local-file-access',
            '--encoding', 'utf-8',
            '--page-size', 'A4',
            '--margin-top', '20mm',
            '--margin-bottom', '20mm',
            '--margin-left', '15mm',
            '--margin-right', '15mm',
            '--footer-center', '[page]/[topage]',
            '--footer-font-size', '9',
            str(html_path), str(pdf_path)
        ]
        subprocess.run(cmd_html2pdf, check=True, capture_output=True)

        return True, "pandoc+wkhtmltopdf"
    except subprocess.CalledProcessError:
        return False, "wkhtmltopdf failed"
    finally:
        # Always cleanup temp files
        html_path.unlink(missing_ok=True)
        css_path.unlink(missing_ok=True)

def method_weasyprint(md_path, pdf_path):
    """Method 3: WeasyPrint (pure Python, but needs dependencies)"""
    try:
        from weasyprint import HTML, CSS
        import markdown
    except ImportError:
        return False, "weasyprint or markdown not installed"

    try:
        # Read markdown
        with open(md_path, 'r', encoding='utf-8') as f:
            md_content = f.read()

        # Convert to HTML
        html_content = markdown.markdown(
            md_content,
            extensions=['tables', 'fenced_code', 'toc', 'nl2br']
        )

        # Wrap with proper HTML structure
        full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{md_path.stem}</title>
<style>{CHINESE_CSS}</style>
</head>
<body>
{html_content}
</body>
</html>"""

        # Convert to PDF
        HTML(string=full_html).write_pdf(str(pdf_path))
        return True, "weasyprint"
    except Exception as e:
        return False, f"weasyprint failed: {str(e)}"

def method_md2pdf(md_path, pdf_path):
    """Method 4: md2pdf library (fallback)"""
    try:
        from md2pdf.core import md2pdf as md2pdf_convert
    except ImportError:
        return False, "md2pdf not installed"

    try:
        # Create temp CSS file
        css_path = md_path.with_suffix('.temp.css')
        with open(css_path, 'w', encoding='utf-8') as f:
            f.write(CHINESE_CSS)

        with open(md_path, 'r', encoding='utf-8') as f:
            md_content = f.read()

        md2pdf_convert(
            pdf_file_path=str(pdf_path),
            md_content=md_content,
            css_file_path=str(css_path),
            base_url=str(md_path.parent)
        )

        css_path.unlink(missing_ok=True)
        return True, "md2pdf"
    except Exception as e:
        css_path = md_path.with_suffix('.temp.css')
        css_path.unlink(missing_ok=True)
        return False, f"md2pdf failed: {str(e)}"

def convert_md_to_pdf(md_path, pdf_path=None):
    """
    Convert Markdown to PDF using best available method

    Priority:
    1. pandoc + xelatex (best Chinese support)
    2. pandoc + wkhtmltopdf (good alternative)
    3. weasyprint (pure Python)
    4. md2pdf (fallback)
    """
    md_file = Path(md_path)
    if not md_file.exists():
        print(f"❌ Error: File not found: {md_path}")
        sys.exit(1)

    if pdf_path is None:
        pdf_path = md_file.with_suffix('.pdf')
    else:
        pdf_path = Path(pdf_path)

    # Try methods in order
    methods = [
        method_pandoc_xelatex,
        method_pandoc_wkhtmltopdf,
        method_weasyprint,
        method_md2pdf,
    ]

    for method in methods:
        success, msg = method(md_file, pdf_path)
        if success:
            print(f"✅ Converted: {md_path} → {pdf_path} ({msg})")
            return str(pdf_path)

    # All methods failed
    print("❌ Error: Could not convert Markdown to PDF")
    print("\nTried the following methods (all failed):")
    print("1. pandoc + xelatex - Install: brew install pandoc basictex")
    print("2. pandoc + wkhtmltopdf - Install: brew install pandoc wkhtmltopdf")
    print("3. weasyprint - Install: pip install weasyprint markdown")
    print("4. md2pdf - Install: pip install md2pdf")
    print("\n💡 Recommendation: Install pandoc for best results:")
    print("   macOS: brew install pandoc basictex")
    print("   Ubuntu: sudo apt-get install pandoc texlive-xetex fonts-noto-cjk")
    sys.exit(1)

def main():
    if len(sys.argv) < 2:
        print("Usage: python md2pdf.py <input.md> [output.pdf]")
        print("")
        print("Converts Markdown to PDF with Chinese support")
        print("")
        print("Auto-detects available tools in this order:")
        print("  1. pandoc + xelatex (best quality)")
        print("  2. pandoc + wkhtmltopdf")
        print("  3. weasyprint")
        print("  4. md2pdf")
        sys.exit(1)

    md_path = sys.argv[1]
    pdf_path = sys.argv[2] if len(sys.argv) > 2 else None

    convert_md_to_pdf(md_path, pdf_path)

if __name__ == "__main__":
    main()
