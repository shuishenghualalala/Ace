"""Transactional task/attempt journal. External calls always happen after commit.

JSON snapshots are intentionally small (planner limit: 12 units). All mutations
use BEGIN IMMEDIATE, including claims across independent SQLite connections.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


class ExecutionStore:
    def __init__(self, path: str, *, wal_enabled: bool = True) -> None:
        self._conn = connect_sqlite(path, wal_enabled=wal_enabled, row_factory=True)
        self._lock = threading.Lock()
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(lambda c: c.execute('''CREATE TABLE IF NOT EXISTS collab_executions (
            owner_id TEXT NOT NULL, task_id TEXT NOT NULL, session_id TEXT NOT NULL,
            snapshot TEXT NOT NULL, PRIMARY KEY(owner_id, task_id))'''))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _event(task: dict, kind: str, **data: Any) -> None:
        task['events'].append({'sequence': len(task['events']) + 1, 'kind': kind,
                               'at': time.time(), **data})
        task['updated_at'] = time.time()

    @staticmethod
    def _state(task: dict) -> None:
        if task['state'] in {'cancelled', 'paused'}:
            return
        states = {u['state'] for u in task['units']}
        if states == {'completed'}:
            task['state'] = 'completed'
        elif 'running' in states or 'pending' in states:
            task['state'] = 'running'
        elif 'unknown' in states:
            task['state'] = 'needs_reconciliation'
        elif 'completed' in states:
            task['state'] = 'partially_completed'
        else:
            task['state'] = 'failed'
        if task['state'] == 'completed':
            task['acceptance'] = 'pending'

    def _mutate(self, owner: str, task_id: str, operation: Callable) -> Any:
        if not owner:
            raise ValueError('缺少账户')
        def write(c: Any) -> Any:
            row = c.execute('SELECT snapshot FROM collab_executions WHERE owner_id=? AND task_id=?',
                            (owner, task_id)).fetchone()
            if row is None:
                raise ValueError('协作任务不存在')
            task = json.loads(row['snapshot'])
            result = operation(task)
            self._state(task)
            c.execute('UPDATE collab_executions SET snapshot=? WHERE owner_id=? AND task_id=?',
                      (json.dumps(task, ensure_ascii=False), owner, task_id))
            return copy.deepcopy(result)
        return self._writer.execute(write)

    def admit(self, owner: str, workflow: dict, *, require_planned: bool = True) -> dict:
        """Claim planning handoff AND persist execution in one database transaction.

        require_planned=False is for explicitly supplied, already validated plans
        (e.g. isolated acceptance fixtures), never an HTTP request option.
        """
        from crew.collaboration.planning.plan import validate_work_units
        task_info = workflow['task']
        task_id, plan_id = task_info['task_id'], workflow['collaboration_plan_id']
        if not owner or not task_id or not plan_id:
            raise ValueError('缺少任务、计划或账户')
        nodes = workflow['nodes']
        edges = workflow.get('edges', [])
        ids = {n['id'] for n in nodes}
        if len(ids) != len(nodes) or any(e['parent_id'] not in ids or e['child_id'] not in ids for e in edges):
            raise ValueError('无效或重复的工作单元/依赖')
        # IDs are server-generated hex values; validate topology using safe aliases.
        aliases = {n['id']: f'unit_{i}' for i, n in enumerate(nodes)}
        validate_work_units([{'key': aliases[n['id']], 'objective': n['title'],
                             'expected_outputs': n.get('expected_outputs') or ['工作成果'],
                             'completion_criteria': n.get('acceptance_criteria') or ['产出可审阅'],
                             'depends_on': [aliases[e['parent_id']] for e in edges if e['child_id'] == n['id']]}
                            for n in nodes])
        if any(not n.get('assignee') for n in nodes):
            raise ValueError('工作单元缺少负责人')
        task = {'task_id': task_id, 'plan_id': plan_id, 'session_id': task_info.get('session_id', ''),
                'workflow': copy.deepcopy(workflow), 'goal': task_info['goal'], 'team_snapshot': task_info.get('team_snapshot', {}),
                'inputs': task_info.get('inputs', []), 'state': 'running', 'acceptance': 'not_requested',
                'created_at': time.time(), 'updated_at': time.time(), 'events': [], 'attempts': [],
                'units': [{**copy.deepcopy(n), 'depends_on': [e['parent_id'] for e in edges if e['child_id'] == n['id']],
                           'state': 'pending', 'attempt_id': None, 'result': None, 'error': ''} for n in nodes]}
        def write(c: Any) -> dict:
            existing = c.execute('SELECT snapshot FROM collab_executions WHERE owner_id=? AND task_id=?', (owner, task_id)).fetchone()
            if existing:
                saved = json.loads(existing['snapshot'])
                if saved['plan_id'] != plan_id:
                    raise ValueError('执行计划版本冲突')
                return saved
            if require_planned:
                row = c.execute('SELECT state,plan,snapshot FROM collab_planning_tasks WHERE owner_id=? AND task_id=?', (owner, task_id)).fetchone()
                if not row or row['state'] != 'planned' or json.loads(row['plan'])['plan_id'] != plan_id:
                    raise ValueError('规划未提交、已交接或版本已变化')
                # Do not accept a substituted workflow with the same plan ID.
                from crew.collaboration.planning.handoff import workflow_from_preview
                source = json.loads(row['snapshot'])
                expected = workflow_from_preview({'state': row['state'], 'task': source, 'plan': json.loads(row['plan'])},
                    team_id=source['team']['team_id'], session_id=source['session_id'], goal=source['goal'],
                    team_updated_at=source['team']['team_updated_at'])
                if expected != workflow:
                    raise ValueError('交接内容与已提交计划不一致')
                c.execute("UPDATE collab_planning_tasks SET state='handoff_committed',updated_at=? WHERE owner_id=? AND task_id=?", (time.time(), owner, task_id))
            self._event(task, 'handoff_committed', plan_id=plan_id)
            c.execute('INSERT INTO collab_executions VALUES (?,?,?,?)',
                      (owner, task_id, task['session_id'], json.dumps(task, ensure_ascii=False)))
            return task
        return self._writer.execute(write)

    def get(self, owner: str, task_id: str) -> dict | None:
        if not owner:
            raise ValueError('缺少账户')
        with self._lock:
            row = self._conn.execute('SELECT snapshot FROM collab_executions WHERE owner_id=? AND task_id=?', (owner, task_id)).fetchone()
        return json.loads(row['snapshot']) if row else None

    def for_session(self, owner: str, session_id: str) -> list[dict]:
        if not owner:
            raise ValueError('缺少账户')
        with self._lock:
            rows = self._conn.execute('SELECT snapshot FROM collab_executions WHERE owner_id=? ORDER BY rowid', (owner,)).fetchall()
        return [t for r in rows if (t := json.loads(r['snapshot']))['session_id'].split('::turn::')[0] == session_id.split('::turn::')[0]]

    def claim(self, owner: str, task_id: str, *, concurrency: int = 2) -> dict | None:
        if not 1 <= concurrency <= 12:
            raise ValueError('并发上限必须在 1 到 12 之间')
        def op(task: dict) -> dict | None:
            if task['state'] != 'running':
                return None
            units = task['units']
            active = [u for u in units if u['state'] in {'running', 'unknown'}]
            if sum(u['state'] == 'running' for u in active) >= concurrency:
                return None
            completed = {u['id'] for u in units if u['state'] == 'completed'}
            occupied = {u['assignee'] for u in active}
            for u in units:
                if u['state'] != 'pending' or u['assignee'] in occupied or not set(u['depends_on']) <= completed:
                    continue
                # Shared side effects must declare resource keys; unknown workspace
                # safety is serialized by the adapter, never guessed by the scheduler.
                resources = set(u.get('resource_keys') or [])
                if any(resources.intersection(v.get('resource_keys') or []) for v in active):
                    continue
                attempt_id = uuid.uuid4().hex
                package = {'task_id': task_id, 'plan_id': task['plan_id'], 'unit_id': u['id'],
                           'attempt_id': attempt_id, 'member_id': u['assignee'], 'goal': task['goal'],
                           'objective': u['detail'] or u['title'], 'inputs': copy.deepcopy(task['inputs']),
                           'required_inputs': u.get('inputs', []), 'expected_outputs': u.get('expected_outputs', []),
                           'completion_criteria': u.get('acceptance_criteria', []),
                           'required_tools': u.get('required_tools', []),
                           'assignment_reason': u.get('assignment_reason', ''),
                           'upstream': [{'unit_id': v['id'], 'result': v['result']} for v in units if v['id'] in u['depends_on']]}
                attempt = {'attempt_id': attempt_id, 'unit_id': u['id'], 'planned_member': u['assignee'],
                           'actual_executor': None, 'state': 'dispatching', 'started_at': time.time(),
                           'ended_at': None, 'package': package, 'result': None, 'error': ''}
                task['attempts'].append(attempt)
                u.update(state='running', attempt_id=attempt_id, error='')
                self._event(task, 'dispatch_intent', unit_id=u['id'], attempt_id=attempt_id)
                return package
            return None
        return self._mutate(owner, task_id, op)

    def finish(self, owner: str, task_id: str, attempt_id: str, *, state: str, result: dict | None = None, error: str = '') -> None:
        if state not in {'completed', 'failed', 'unknown'}:
            raise ValueError('无效的执行结果状态')
        def op(task: dict) -> None:
            a = next((a for a in task['attempts'] if a['attempt_id'] == attempt_id), None)
            if not a:
                raise ValueError('执行尝试不存在')
            if a['state'] != 'dispatching':
                # Repeated completion may not overwrite the historical outcome.
                return
            u = next(u for u in task['units'] if u['id'] == a['unit_id'])
            if u['attempt_id'] != attempt_id:
                raise ValueError('过期的执行结果')
            if state == 'completed' and (not result or not str(result.get('text') or '').strip() or not result.get('executor')):
                raise ValueError('完成必须包含真实输出与执行者')
            a.update(state=state, result=result, error=error, ended_at=time.time(), actual_executor=(result or {}).get('executor'))
            u.update(state=state, result=result, error=error)
            self._event(task, 'attempt_' + state, unit_id=u['id'], attempt_id=attempt_id, error=error)
        self._mutate(owner, task_id, op)

    def settle(self, owner: str, task_id: str) -> None:
        def op(task: dict) -> None:
            bad = {u['id'] for u in task['units'] if u['state'] in {'failed', 'blocked', 'unknown', 'cancelled'}}
            changed = True
            while changed:
                changed = False
                for u in task['units']:
                    if u['state'] == 'pending' and set(u['depends_on']).intersection(bad):
                        u.update(state='blocked', error='上游未完成；可在处理上游后恢复')
                        bad.add(u['id'])
                        self._event(task, 'dependency_blocked', unit_id=u['id'])
                        changed = True
        self._mutate(owner, task_id, op)

    def control(self, owner: str, task_id: str, action: str, *, unit_id: str = '', confirm_unknown: bool = False) -> None:
        def op(task: dict) -> None:
            if action in {'pause', 'cancel'}:
                task['state'] = 'paused' if action == 'pause' else 'cancelled'
                if action == 'cancel':
                    for u in task['units']:
                        if u['state'] in {'pending', 'blocked'}:
                            u['state'] = 'cancelled'
            elif action == 'resume':
                if task['state'] != 'paused':
                    raise ValueError('只能恢复已暂停任务')
                task['state'] = 'running'
            elif action == 'retry':
                if task['state'] in {'running', 'cancelled', 'paused'}:
                    raise ValueError('请先等待本轮结束；取消任务不能重试')
                u = next((u for u in task['units'] if u['id'] == unit_id), None)
                if not u or u['state'] not in {'failed', 'unknown'}:
                    raise ValueError('该工作不能重试')
                if u['state'] == 'unknown' and not confirm_unknown:
                    raise ValueError('外部调用结果未知；核实副作用后显式确认才能重试')
                u.update(state='pending', error='', attempt_id=None)
                for v in task['units']:
                    if v['state'] == 'blocked':
                        v.update(state='pending', error='')
                task['state'] = 'running'
            else:
                raise ValueError('无效操作')
            self._event(task, action, unit_id=unit_id, confirmed_unknown=confirm_unknown)
        self._mutate(owner, task_id, op)

    def recover(self, owner: str, task_id: str) -> None:
        """Call only after establishing that the previous runner has stopped."""
        def op(task: dict) -> None:
            for a in task['attempts']:
                if a['state'] == 'dispatching':
                    a.update(state='unknown', error='执行进程中断，外部结果待核实', ended_at=time.time())
                    u = next(u for u in task['units'] if u['id'] == a['unit_id'])
                    u.update(state='unknown', error=a['error'])
                    self._event(task, 'recovery_unknown', attempt_id=a['attempt_id'], unit_id=u['id'])
        self._mutate(owner, task_id, op)
        self.settle(owner, task_id)
