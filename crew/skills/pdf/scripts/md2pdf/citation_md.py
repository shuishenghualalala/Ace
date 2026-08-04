#!/usr/bin/env python3
"""
Standard Markdown footnote → citation preprocessing (PDF variant).

Input  : standard Markdown footnotes
             body        [^id]
             definition  [^id]: Title. Date. URL
Output : Markdown (with inline HTML) where
             - each body [^id] becomes a superscript anchor link
               <sup ...><a href="#ref-N">[N]</a></sup>
             - the footnote definition block is stripped
             - a consolidated References section (HTML <ol>) is appended,
               numbered by appearance after URL dedup, with anchors #ref-N.

Numbering rule (same as the docx variant):
  - Deduplicate by URL; footnotes sharing a URL collapse to one number.
  - Display numbers N=1,2,3... by first body appearance.
  - The id is only a handle; the URL is the dedup key. Definitions without a
    URL fall back to dedup-by-id.

Robustness:
  - Footnote markers inside fenced code blocks (``` / ~~~) and inline code
    (`...`) are ignored.
  - URLs used as the dedup key are stripped of trailing punctuation.

CLI:
  python3 citation_md.py --check <file.md>   # citation-health report

No citation.jsonl, no global indices.
"""
import html
import logging
import re
import sys

logger = logging.getLogger(__name__)

_RE_DEF = re.compile(r'^\[\^([^\]\s]+)\]:\s?(.*)$')
_RE_REF = re.compile(r'\[\^([^\]\s]+)\](?!:)')
_RE_URL = re.compile(r'https?://\S+')
_RE_CODE_SPAN = re.compile(r'```.*?```|~~~.*?~~~|``.*?``|`[^`\n]*`', re.DOTALL)
_RE_FENCE = re.compile(r'^\s*(```|~~~)')

_URL_TRAILING = '.,;:!?)]}>"\'、，。；：）】》”’'


def _clean_url(url: str) -> str:
    return url.rstrip(_URL_TRAILING)


def _extract_url(text: str) -> str:
    m = _RE_URL.search(text)
    return _clean_url(m.group(0)) if m else ''


def _parse_definitions(lines: list[str]) -> tuple[dict[str, str], set]:
    """Parse footnote definitions; skip lines inside fenced code blocks."""
    defs: dict[str, str] = {}
    strip_idx: set = set()

    i = 0
    n = len(lines)
    in_fence = False
    while i < n:
        if _RE_FENCE.match(lines[i]):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        m = _RE_DEF.match(lines[i])
        if not m:
            i += 1
            continue
        fid, first = m.group(1), m.group(2)
        parts = [first.strip()]
        strip_idx.add(i)
        j = i + 1
        while j < n:
            line = lines[j]
            if line.strip() == '':
                k = j + 1
                while k < n and lines[k].strip() == '':
                    k += 1
                if k < n and (lines[k].startswith('    ') or lines[k].startswith('\t')):
                    strip_idx.update(range(j, k))
                    j = k
                    continue
                break
            if line.startswith('    ') or line.startswith('\t'):
                parts.append(line.strip())
                strip_idx.add(j)
                j += 1
                continue
            break
        text = ' '.join(p for p in parts if p).strip()
        if fid in defs:
            logger.warning(f"Duplicate footnote definition id [^{fid}], keeping first")
        else:
            defs[fid] = text
        i = j

    return defs, strip_idx


def _map_outside_code(text: str, fn):
    out = []
    last = 0
    for m in _RE_CODE_SPAN.finditer(text):
        out.append(fn(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(text[last:]))
    return ''.join(out)


def _noncode_segments(text: str) -> list[str]:
    segs = []
    last = 0
    for m in _RE_CODE_SPAN.finditer(text):
        segs.append(text[last:m.start()])
        last = m.end()
    segs.append(text[last:])
    return segs


def _linkify(text: str) -> str:
    """HTML-escape a definition string, turning the URL into a clickable link."""
    m = _RE_URL.search(text)
    if not m:
        return html.escape(text)
    url = _clean_url(m.group(0))
    before = html.escape(text[:m.start()])
    after = html.escape(text[m.start() + len(url):])
    safe_url = html.escape(url, quote=True)
    return f'{before}<a href="{safe_url}">{html.escape(url)}</a>{after}'


def _resolve(markdown_content: str):
    lines = markdown_content.split('\n')
    defs, strip_idx = _parse_definitions(lines)
    id_url = {fid: _extract_url(text) for fid, text in defs.items()}
    body = '\n'.join(line for idx, line in enumerate(lines) if idx not in strip_idx)

    id_to_num: dict[str, int] = {}
    url_to_num: dict[str, int] = {}
    num_to_text: dict[int, str] = {}
    next_num = 1
    unresolved: set = set()

    def _assign(fid: str) -> int:
        nonlocal next_num
        if fid in id_to_num:
            return id_to_num[fid]
        url = id_url.get(fid, '')
        if url and url in url_to_num:
            num = url_to_num[url]
        else:
            num = next_num
            next_num += 1
            num_to_text[num] = defs[fid]
            if url:
                url_to_num[url] = num
        id_to_num[fid] = num
        return num

    for seg in _noncode_segments(body):
        for m in _RE_REF.finditer(seg):
            fid = m.group(1)
            if fid not in defs:
                unresolved.add(fid)
                continue
            _assign(fid)

    return {
        'body': body, 'defs': defs, 'id_url': id_url,
        'id_to_num': id_to_num, 'num_to_text': num_to_text, 'unresolved': unresolved,
    }


def convert_citations_for_pdf(
    markdown_content: str,
    references_heading: str = "References",
) -> tuple[str, int, list[str]]:
    """Convert standard md footnotes to superscript anchors + a References section.

    Returns (transformed_markdown_with_html, n_unique_citations, warnings).
    """
    warnings: list[str] = []
    r = _resolve(markdown_content)
    if not r['defs']:
        logger.info("No footnote definitions found; passing markdown through unchanged")
        return markdown_content, 0, warnings

    if r['unresolved']:
        warnings.append(
            f"{len(r['unresolved'])} footnote reference(s) without a definition, left as-is: "
            f"{sorted(r['unresolved'])[:20]}"
        )

    id_to_num, num_to_text = r['id_to_num'], r['num_to_text']

    def _sub(m):
        fid = m.group(1)
        if fid not in id_to_num:
            return m.group(0)
        num = id_to_num[fid]
        return f'<sup class="cite"><a href="#ref-{num}">[{num}]</a></sup>'

    converted_body = _map_outside_code(r['body'], lambda seg: _RE_REF.sub(_sub, seg))

    ref_items = [
        f'<li id="ref-{num}"><a name="ref-{num}"></a>{_linkify(num_to_text[num])}</li>'
        for num in sorted(num_to_text)
    ]
    references_html = (
        f'\n\n<h2 id="references">{html.escape(references_heading)}</h2>\n'
        f'<ol class="references">\n' + '\n'.join(ref_items) + '\n</ol>\n'
    )

    logger.info(
        f"Resolved {len(id_to_num)} footnote id(s) into {len(num_to_text)} unique "
        f"citation(s) after URL dedup"
    )
    return converted_body + references_html, len(num_to_text), warnings


def analyze(markdown_content: str) -> dict:
    """Citation-health report used by --check."""
    r = _resolve(markdown_content)
    defs_with_url = sum(1 for u in r['id_url'].values() if u)
    old_style = sorted(fid for fid in r['unresolved'] if re.fullmatch(r'\d+\^?', fid))
    return {
        'defs_total': len(r['defs']),
        'defs_with_url': defs_with_url,
        'unique_citations': len(r['num_to_text']),
        'resolved_refs': len(r['id_to_num']),
        'unresolved': sorted(r['unresolved']),
        'old_style_markers': old_style,
    }


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Standard Markdown footnote citation checker")
    p.add_argument('--check', metavar='FILE', required=True, help='Markdown file to check')
    args = p.parse_args(argv)

    rep = analyze(open(args.check, encoding='utf-8').read())
    print(f"definitions:            {rep['defs_total']}")
    print(f"  with URL:             {rep['defs_with_url']}")
    print(f"unique citations:       {rep['unique_citations']}")
    print(f"resolved references:    {rep['resolved_refs']}")
    print(f"unresolved references:  {len(rep['unresolved'])} {rep['unresolved'][:20]}")
    print(f"old-style [^N^] markers:{len(rep['old_style_markers'])} {rep['old_style_markers'][:20]}")
    problems = []
    if rep['unresolved']:
        problems.append("references without a matching definition")
    if rep['defs_total'] and rep['defs_with_url'] == 0:
        problems.append("no footnote definition contains a URL (URL dedup impossible)")
    if rep['old_style_markers'] and rep['unique_citations'] == 0:
        problems.append("document uses old-style [^N^] markers with no standard definitions")
    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    print("OK: citations are standard Markdown footnotes")
    return 0


if __name__ == '__main__':
    sys.exit(main())
