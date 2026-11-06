"""Evidence contract for project-review deliverables; no model-generated scores."""
from __future__ import annotations

import re

DIMENSIONS = ('需求与差异化', 'AI实际帮助', '商业假设与验证', '材料完整与可复核')


def validate_review_plan(units: list[dict], *, assignments: list[dict] | None = None,
                         leader_id: str = '') -> None:
    """Opt-in scenario contract, not a silent topology repair or generic heuristic."""
    from crew.collaboration.planning.plan import PlanningError
    message = ('project_review_v1 要求恰好4个单元：一个来源核对根节点→两个独立并行审阅'
               '（各自 depends_on 只含来源节点）→一个综合节点（depends_on 必须包含两个审阅节点）。'
               '不能只在 objective 文字里声称等待而遗漏 depends_on。')
    id_field = 'unit_id' if assignments is not None else 'key'
    deps = {u[id_field]: set(u['depends_on']) for u in units}
    roots = [k for k, v in deps.items() if not v]
    if len(units) != 4 or len(roots) != 1:
        raise PlanningError(message)
    root = roots[0]
    reviewers = [k for k, v in deps.items() if v == {root}]
    sinks = [k for k, v in deps.items() if v == set(reviewers) and k not in reviewers]
    if len(reviewers) != 2 or len(sinks) != 1:
        raise PlanningError(message)
    if assignments is not None:
        members = {a['unit_id']: a['member_id'] for a in assignments}
        if (members[root] != leader_id or members[sinks[0]] != leader_id or
                len({members[k] for k in reviewers}) != 2 or
                any(members[k] == leader_id for k in reviewers)):
            raise PlanningError('评审契约要求 Leader 做来源核对和综合；两个不同成员独立审阅，不能由 Leader 冒充所有成员')


def validate_review(report: dict, sources: dict[str, str]) -> list[str]:
    if not isinstance(report, dict):
        return ['评审成果必须为 JSON 对象']
    errors = []
    if report.get('fictional') is not True:
        errors.append('必须明确所有项目为虚构演示')
    if report.get('human_decision_required') is not True:
        errors.append('必须保留人工最终判断')
    if any(key in report for key in ('ranking', 'total_score', 'admission', 'winner')):
        errors.append('不得自动排名、加权评分或录取')
    cards = report.get('projects')
    if (not isinstance(cards, list) or len(cards) != 3 or
            any(not isinstance(c, dict) or not isinstance(c.get('id'), str) for c in cards) or
            {c['id'] for c in cards} != {'A', 'B', 'C'}):
        return errors + ['必须恰好覆盖 A/B/C 三个项目']
    for card in cards:
        rows = card.get('dimensions', [])
        if (not isinstance(rows, list) or len(rows) != 4 or
                any(not isinstance(r, dict) or not isinstance(r.get('dimension'), str) for r in rows) or
                {r['dimension'] for r in rows} != set(DIMENSIONS)):
            errors.append(f"{card['id']} 缺少四维证据卡")
            continue
        for row in rows:
            for field in ('claim', 'analysis', 'uncertainty'):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    errors.append(f"{card['id']} 缺少 {field}")
            if row.get('strength') not in ('强', '中', '弱', '未提供'):
                errors.append('证据强弱无效')
            citations = row.get('sources')
            if not isinstance(citations, list) or not citations:
                errors.append('每项判断必须引用实际来源（缺证据也须指明检查的材料）')
                continue
            for ref in citations:
                if not isinstance(ref, dict):
                    errors.append('引用格式无效')
                    continue
                source, section, quote = ref.get('file'), ref.get('section'), ref.get('quote')
                if not isinstance(source, str) or not isinstance(section, str):
                    errors.append('来源文件名与小节必须为字符串')
                    continue
                body = sources.get(source, '')
                heading = re.search(r'^##[ \t]+' + re.escape(section) + r'[ \t]*$', body, re.MULTILINE) if section else None
                if not body or not heading or not isinstance(quote, str) or len(quote.strip()) < 8:
                    errors.append(f'来源/小节/摘录无效：{source}')
                    continue
                section_body = re.split(r'^##[ \t]+', body[heading.end():], maxsplit=1, flags=re.MULTILINE)[0]
                if quote not in section_body:
                    errors.append(f'引文并非来源小节原文：{source} / {section}')
                if source[0] not in {card['id'], '0'}:
                    errors.append(f"{card['id']} 引用了其他项目来源")
    questions = report.get('questions')
    if not isinstance(questions, list) or len(questions) != 3 or any(not isinstance(q, str) or not q.strip() for q in questions):
        errors.append('须给出三个关键追问')
    return errors


def render_review(report: dict) -> str:
    lines = ['# 三个虚构项目预审底稿', '', '> 新编虚构演示材料；不是正式评分。真实模型生成，仍待人工复核。', '']
    lines += ['## 横向比较', '', '| 项目 | 需求与差异化 | AI实际帮助 | 商业假设与验证 | 材料完整与可复核 |', '|---|---|---|---|---|']
    def cell(text):
        return str(text).replace('|', '／').replace('\n', '<br>')
    for p in report['projects']:
        by = {r['dimension']: r for r in p['dimensions']}
        lines.append('| ' + p['id'] + ' | ' + ' | '.join(cell(by[d]['analysis']) for d in DIMENSIONS) + ' |')
    for p in report['projects']:
        lines += ['', f"## 项目 {p['id']}", '', '| 维度 | 材料记载 | 来源与原文 | 分析判断 | 待确认 | 证据强弱 |', '|---|---|---|---|---|---|']
        for r in p['dimensions']:
            refs = '; '.join(f"{s['file']} §{s['section']}：「{s['quote']}」" for s in r['sources'])
            lines.append('| ' + ' | '.join(map(cell, [r['dimension'], r['claim'], refs, r['analysis'], r['uncertainty'], r['strength']])) + ' |')
    lines += ['', '## 会前最值得追问的三个问题', *[f'{i}. {q}' for i, q in enumerate(report['questions'], 1)], '', '自动校验仅核对结构、维度覆盖与逐字引用；不构成语义正确性保证。']
    return '\n'.join(lines)


def review_output_instruction() -> str:
    import json
    schema = {'fictional': True, 'human_decision_required': True,
              'projects': [{'id': 'A', 'dimensions': [{'dimension': d, 'claim': '材料记载',
                  'sources': [{'file': '实际文件名', 'section': '实际二级标题', 'quote': '连续原文至少8字'}],
                  'analysis': '分析判断', 'uncertainty': '具体待确认', 'strength': '弱'} for d in DIMENSIONS]}],
              'questions': ['关键追问1', '关键追问2', '关键追问3']}
    return ('最终交付只输出 JSON，projects 必须恰好包含 A、B、C 三个项目，每个恰好四维；'
            '引用实际提供的来源文件名、小节与连续逐字原文。不要输出模板占位词。\n' + json.dumps(schema, ensure_ascii=False))
