"""Shared desktop/web task-board projection from the authoritative journal."""
from __future__ import annotations


def project_tasks(executions: list[dict], legacy: list[dict]) -> list[dict]:
    turns = {t['session_id'] for t in executions}
    result = [i for i in legacy if str((i.get('progress') or {}).get('turn_session_id') or i.get('session_id')) not in turns]
    statuses = {'running': 'running', 'completed': 'completed', 'unknown': 'blocked',
                'pending': 'pending', 'blocked': 'blocked', 'failed': 'failed', 'cancelled': 'cancelled'}
    for task in executions:
        for u in task['units']:
            attempts = [a for a in task['attempts'] if a['unit_id'] == u['id']]
            progress = {'source': 'team_plan', 'plan_node_id': u['id'], 'turn_session_id': task['session_id'],
                        'turn_title': task['goal'], 'assignee': u['assignee'], 'full_title': u['title'],
                        'parent_ids': u['depends_on'], 'depends_on': u['depends_on'],
                        'collaboration_task_id': task['task_id'], 'collaboration_plan_id': task['plan_id'],
                        'execution_state': u['state'], 'acceptance': task['acceptance'],
                        'assignment_source': u.get('assignment_source', ''), 'assignment_reason': u.get('assignment_reason', ''),
                        'execution_attempts': [{k: a.get(k) for k in ('attempt_id', 'planned_member', 'actual_executor', 'state', 'started_at', 'ended_at', 'error')} for a in attempts]}
            result.append({'id': u['id'], 'task_id': u['id'], 'session_id': task['session_id'], 'kind': 'team',
                           'title': u['title'], 'assignee': u['assignee'], 'status': statuses[u['state']],
                           'result': (u['result'] or {}).get('text', ''), 'error': u['error'], 'progress': progress,
                           'created_at': task['created_at'], 'updated_at': task['updated_at']})
    return result
