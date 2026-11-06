"""Synthetic audit unit tests, explicitly not real-model acceptance evidence."""
import copy
import hashlib

import pytest

from crew.collaboration.review import DIMENSIONS
from scripts.audit_review_execution import audit


@pytest.fixture
def task():
    content = '## 测试小节\n这是一段仅供单元测试使用的虚构原文。'
    report = {'fictional': True, 'human_decision_required': True, 'projects': [
        {'id': project, 'dimensions': [
            {'dimension': d, 'claim': '测试材料并没有提供实际运行证据。',
             'analysis': '仅能证明文字存在而不能证明实际效果。',
             'uncertainty': '仍然需要提供真实运行记录以供核实。',
             'strength': '弱', 'sources': [{'file': '00-测试.md', 'section': '测试小节',
                                         'quote': '这是一段仅供单元测试使用的虚构原文。'}]}
            for d in DIMENSIONS]} for project in 'ABC'],
        'questions': ['哪些记录可供核实？', '实际效果如何验证？', '付费假设如何验证？']}
    units, attempts = [], []
    for id, member, deps, start, end in [
        ('root', 'leader', [], 1, 2), ('tech', 'tech', ['root'], 3, 6),
        ('biz', 'biz', ['root'], 4, 7), ('final', 'leader', ['tech', 'biz'], 8, 9),
    ]:
        result = {'text': 'synthetic unit test', 'executor': member}
        if id == 'final':
            result['structured_report'] = report
        attempt = {'unit_id': id, 'attempt_id': id + '-attempt', 'planned_member': member,
                   'actual_executor': member, 'state': 'completed', 'started_at': start,
                   'ended_at': end, 'result': copy.deepcopy(result),
                   'package': {'upstream': [{'unit_id': u['id'], 'result': u['result']}
                                            for u in units if u['id'] in deps]}}
        attempts.append(attempt)
        units.append({'id': id, 'assignee': member, 'depends_on': deps, 'state': 'completed',
                      'attempt_id': attempt['attempt_id'], 'result': result})
    return {'task_id': 'synthetic-test', 'state': 'completed', 'acceptance': 'pending',
            'units': units, 'attempts': attempts, 'inputs': [{'access': 'read_snapshot',
             'sources': [{'title': '00-测试.md', 'content': content,
                          'sha256': hashlib.sha256(content.encode()).hexdigest()}]}]}


def test_audit_accepts_consistent_record_but_never_guarantees_semantics(task):
    result = audit(task)
    assert result['passed']
    assert result['parallel_overlap_seconds'] == 2
    assert result['citation_count'] == 12
    assert result['semantic_accuracy_guaranteed'] is False


@pytest.mark.parametrize('fault', ['hash', 'identity', 'upstream', 'overlap', 'result', 'incomplete'])
def test_audit_rejects_inconsistent_evidence(task, fault):
    if fault == 'hash':
        task['inputs'][0]['sources'][0]['content'] += '篡改'
    elif fault == 'identity':
        task['attempts'][-1]['actual_executor'] = 'someone-else'
    elif fault == 'upstream':
        task['attempts'][-1]['package']['upstream'] = []
    elif fault == 'overlap':
        task['attempts'][2]['started_at'] = 6.5
    elif fault == 'result':
        task['units'][-1]['result']['text'] = 'not the delivered result'
    elif fault == 'incomplete':
        task['state'] = 'partially_completed'
    assert not audit(task)['passed']
