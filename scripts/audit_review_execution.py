#!/usr/bin/env python3
"""Offline audit of real execution evidence. Does not call or simulate a model."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from crew.collaboration.review import validate_review, validate_review_plan


def audit(task: dict) -> dict:
    errors = []
    if task['state'] != 'completed':
        errors.append('任务未完成')
    units = task['units']
    try:
        validate_review_plan(
            [{'unit_id': u['id'], 'depends_on': u['depends_on']} for u in units],
            assignments=[{'unit_id': u['id'], 'member_id': u['assignee']} for u in units],
            leader_id='leader')
    except ValueError as exc:
        errors.append(str(exc))
    latest = {}
    for attempt in task['attempts']:
        latest[attempt['unit_id']] = attempt
    for unit in units:
        attempt = latest.get(unit['id'])
        if not attempt or attempt['state'] != 'completed' or not attempt.get('actual_executor'):
            errors.append(f"节点没有真实完成记录：{unit['id']}")
            continue
        if (attempt['attempt_id'] != unit.get('attempt_id') or
                attempt['actual_executor'] != unit['assignee'] or
                attempt['planned_member'] != unit['assignee']):
            errors.append(f"节点/尝试/执行者身份不一致：{unit['id']}")
        if not attempt.get('ended_at') or attempt['ended_at'] < attempt['started_at']:
            errors.append(f"完成时间不合法：{unit['id']}")
        if not unit.get('result') or unit['result'] != attempt.get('result'):
            errors.append(f"节点成果与最新尝试不一致：{unit['id']}")
        upstream_ids = {u['unit_id'] for u in attempt['package']['upstream']}
        if upstream_ids != set(unit['depends_on']):
            errors.append(f"工作包的上游不匹配：{unit['id']}")
        for dep in unit['depends_on']:
            parent = latest.get(dep)
            if not parent or not parent.get('ended_at') or parent['ended_at'] > attempt['started_at']:
                errors.append(f"节点在上游完成前启动：{unit['id']}")
            passed = next((u for u in attempt['package']['upstream'] if u['unit_id'] == dep), None)
            if parent and passed and passed.get('result') != parent.get('result'):
                errors.append(f"工作包未收到上游实际成果：{unit['id']}")
    source_items = [s for ref in task['inputs'] if ref.get('access') == 'read_snapshot' for s in ref['sources']]
    sources = {s['title']: s['content'] for s in source_items}
    if len(source_items) != len(sources):
        errors.append('重复来源标题，不能无歧义核对引文')
    for source in source_items:
        if hashlib.sha256(source['content'].encode()).hexdigest() != source['sha256']:
            errors.append('快照内容哈希不匹配：' + source['title'])
    reports = [(u.get('result') or {}).get('structured_report') for u in units]
    reports = [r for r in reports if r is not None]
    if len(reports) != 1:
        errors.append('未恰好产生一份结构化最终评审')
        report = {}
    else:
        report = reports[0]
        errors.extend(validate_review(report, sources))
    reviewers = [latest[u['id']] for u in units if len(u['depends_on']) == 1 and u['id'] in latest]
    overlap = 0
    if len(reviewers) == 2 and all(a.get('ended_at') for a in reviewers):
        overlap = max(0, min(a['ended_at'] for a in reviewers) - max(a['started_at'] for a in reviewers))
    if overlap <= 0:
        errors.append('未证实两个独立审阅实际重叠执行')
    return {'task_id': task['task_id'], 'state': task['state'], 'acceptance': task['acceptance'],
            'source_count': len(sources), 'unit_count': len(units), 'attempt_count': len(task['attempts']),
            'parallel_overlap_seconds': round(overlap, 3),
            'citation_count': sum(len(row['sources']) for p in report.get('projects', []) for row in p['dimensions']),
            'errors': errors, 'passed': not errors, 'semantic_accuracy_guaranteed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('execution', type=Path)
    args = parser.parse_args()
    data = json.loads(args.execution.read_text())
    if isinstance(data, list):
        data = data[-1]
    if 'execution' in data:
        data = data['execution']
    result = audit(data)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
