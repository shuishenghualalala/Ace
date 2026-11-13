import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from crew.collaboration.runtime.scheduler import Scheduler
from crew.collaboration.runtime.store import ExecutionStore


def workflow():
    return {'task': {'task_id': 'task', 'session_id': 'session', 'goal': 'review', 'inputs': [{'kind': 'knowledge_base', 'id': 'kb'}]},
            'collaboration_plan_id': 'plan',
            'nodes': [{'id': k, 'title': k, 'detail': k, 'assignee': m} for k, m in [('a', 'kimi'), ('b', 'hermes'), ('c', 'leader')]],
            'edges': [{'parent_id': k, 'child_id': 'c'} for k in ['a', 'b']]}


@pytest.fixture
def store(tmp_path):
    s = ExecutionStore(str(tmp_path / 'state.db'))
    s.admit('owner', workflow(), require_planned=False)
    yield s
    s.close()


@pytest.mark.asyncio
async def test_parallel_real_dependency_packages(store):
    started, finished = [], []
    gate = asyncio.Event()
    async def execute(p):
        started.append(p['unit_id'])
        if p['unit_id'] in ['a', 'b']:
            if len(started) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)
            assert not p['upstream']
        else:
            assert set(finished) == {'a', 'b'}
            assert len(p['upstream']) == 2
        assert p['inputs'][0]['id'] == 'kb'
        finished.append(p['unit_id'])
        return {'text': p['unit_id'] + ' result', 'executor': p['member_id'], 'validation': 'structural_only'}
    task = await Scheduler(store, execute).run('owner', 'task')
    assert task['state'] == 'completed'
    assert task['acceptance'] == 'pending'
    assert len(task['attempts']) == 3
    assert task['events'][0]['kind'] == 'handoff_committed'


@pytest.mark.asyncio
async def test_failed_branch_does_not_block_independent_work_and_retry_keeps_history(store):
    async def execute(p):
        if p['unit_id'] == 'a':
            raise RuntimeError('executor offline')
        return {'text': 'done', 'executor': p['member_id']}
    t = await Scheduler(store, execute).run('owner', 'task')
    assert t['state'] == 'partially_completed'
    assert [u['state'] for u in t['units']] == ['failed', 'completed', 'blocked']
    store.control('owner', 'task', 'retry', unit_id='a')
    async def success(p):
        if p['unit_id'] == 'a':
            assert p['previous_attempt']['error'] == 'executor offline'
        return {'text': 'done', 'executor': p['member_id']}
    t = await Scheduler(store, success).run('owner', 'task')
    assert t['state'] == 'completed'
    assert len(t['attempts']) == 4
    assert t['attempts'][0]['state'] == 'failed'


def test_owner_isolation_and_atomic_claim_across_connections(store, tmp_path):
    second = ExecutionStore(str(tmp_path / 'state.db'))
    assert store.get('other', 'task') is None
    with pytest.raises(ValueError):
        store.claim('other', 'task')
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda s: s.claim('owner', 'task', concurrency=1), [store, second]))
    assert sum(c is not None for c in claims) == 1
    second.close()


def test_crash_unknown_not_redispatched_and_late_result_fenced(store):
    p = store.claim('owner', 'task')
    store.recover('owner', 'task')
    assert store.get('owner', 'task')['units'][0]['state'] == 'unknown'
    store.finish('owner', 'task', p['attempt_id'], state='completed', result={'text': 'late', 'executor': 'kimi'})
    assert store.get('owner', 'task')['units'][0]['result'] is None
    # Unrelated member can still run; interrupted member never auto-replays.
    assert store.claim('owner', 'task')['unit_id'] == 'b'
    assert store.claim('owner', 'task') is None


@pytest.mark.asyncio
async def test_empty_output_is_failure_not_success(store):
    async def execute(p):
        return {'text': '', 'executor': 'x'}
    t = await Scheduler(store, execute).run('owner', 'task')
    assert t['state'] == 'failed'
    assert not any(u['state'] == 'completed' for u in t['units'])


def test_pause_cancel_no_new_calls(store):
    store.control('owner', 'task', 'pause')
    assert store.claim('owner', 'task') is None
    store.control('owner', 'task', 'resume')
    p = store.claim('owner', 'task')
    store.control('owner', 'task', 'cancel')
    assert store.claim('owner', 'task') is None
    store.finish('owner', 'task', p['attempt_id'], state='completed', result={'text': 'already sent', 'executor': 'kimi'})
    assert store.get('owner', 'task')['state'] == 'cancelled'
    assert store.get('owner', 'task')['units'][0]['state'] == 'completed'


def test_bad_graph_and_member_resource_slots(tmp_path):
    s = ExecutionStore(str(tmp_path / 'x.db'))
    w = workflow()
    w['edges'].append({'parent_id': 'c', 'child_id': 'a'})
    with pytest.raises(ValueError):
        s.admit('owner', w, require_planned=False)
    w = workflow()
    w['nodes'][1]['assignee'] = 'kimi'
    s.admit('owner', w, require_planned=False)
    assert s.claim('owner', 'task')['unit_id'] == 'a'
    assert s.claim('owner', 'task') is None
    s.close()


def test_handoff_and_persistence_failure_never_dispatch(store, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(store._writer, 'execute', broken)
    with pytest.raises(OSError):
        store.claim('owner', 'task')
    assert not store.get('owner', 'task')['attempts']


@pytest.mark.asyncio
async def test_transactional_handoff_cannot_substitute_plan(tmp_path):
    from types import SimpleNamespace
    import json
    from crew.collaboration.planning.service import PlanningService
    from crew.collaboration.planning.store import PlanningStore
    from crew.collaboration.planning.handoff import workflow_from_preview
    path = str(tmp_path / 'atomic.db')
    planning = PlanningStore(path)
    class Provider:
        calls = 0
        async def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(text=json.dumps({'work_units': [{'key': 'review', 'objective': 'review A',
                    'expected_outputs': ['evidence'], 'completion_criteria': ['sources']}]}))
            return SimpleNamespace(text=json.dumps({'assignments': [{'key': 'review', 'matches': [
                {'member_id': 'a', 'kind': 'inferred', 'reference': '', 'reason': 'reviewer'}]}]}))
    preview = await PlanningService(planning).create(owner='owner', request_id='r', session_id='s', goal='review A',
        team={'id': 't', 'updated_at': 1, 'members': [{'agent_id': 'a'}]}, provider=Provider())
    w = workflow_from_preview(preview, team_id='t', session_id='s', goal='review A', team_updated_at=1)
    s = ExecutionStore(path)
    import copy
    bad = copy.deepcopy(w)
    bad['nodes'][0]['detail'] = 'unapproved task'
    with pytest.raises(ValueError, match='不一致'):
        s.admit('owner', bad)
    assert planning.get('owner', w['task']['task_id'])['state'] == 'planned'
    a = s.admit('owner', w)
    assert planning.get('owner', a['task_id'])['state'] == 'handoff_committed'
    assert s.admit('owner', w)['task_id'] == a['task_id']
    assert len(s.for_session('owner', 's')) == 1
    s.close()


@pytest.mark.asyncio
async def test_cancel_inflight_is_unknown_and_never_replayed(store):
    entered = asyncio.Event()
    async def execute(p):
        entered.set()
        await asyncio.Event().wait()
    runner = asyncio.create_task(Scheduler(store, execute, concurrency=1).run('owner', 'task'))
    await entered.wait()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert store.get('owner', 'task')['attempts'][0]['state'] == 'unknown'


def test_projection_uses_journal_not_stale_kanban(store):
    from crew.collaboration.runtime.projection import project_tasks
    p = store.claim('owner', 'task')
    store.finish('owner', 'task', p['attempt_id'], state='failed', error='model unavailable')
    old = [{'id': 'stale', 'session_id': 'session', 'status': 'completed'}]
    result = project_tasks([store.get('owner', 'task')], old)
    assert len(result) == 3
    assert result[0]['status'] == 'failed'
    assert result[0]['progress']['execution_attempts'][0]['error'] == 'model unavailable'
    attempt = store.get('owner', 'task')['attempts'][0]
    assert result[0]['started_at'] == attempt['started_at']
    assert result[0]['finished_at'] == attempt['ended_at']
    assert result[1]['started_at'] == 0  # no fabricated duration for unstarted work


@pytest.mark.asyncio
async def test_adapter_runs_actual_leader_not_control_fallback():
    from types import SimpleNamespace
    from crew.core.envelope import Envelope, ResponseChunk
    from crew.team.collaboration_runtime import TeamExecutionAdapter
    class Leader:
        async def run(self, env):
            assert 'upstream' in env.query
            yield ResponseChunk.final(env.request_id, '')
    host = SimpleNamespace(_team_member_display_name=lambda *a: 'Crew',
                           _recorded_team_internal_chunk=lambda *a, **kw: kw,
                           _leader_runtime_stream_chunk=lambda *a: None)
    node = SimpleNamespace(assignee='leader', title='汇总', node_id='c', metadata={})
    adapter = TeamExecutionAdapter(host, Envelope.of('review', session_id='s', user_id='owner'),
        SimpleNamespace(leader=Leader(), leader_spec=SimpleNamespace(member_id='lead')),
        SimpleNamespace(nodes={'c': node}, edges=[]), '', lambda _: None)
    with pytest.raises(ValueError, match='没有返回成果'):
        await adapter.execute({'unit_id': 'c', 'attempt_id': 'att', 'upstream': []})


@pytest.mark.asyncio
async def test_adapter_persists_actual_stream_and_tools_for_history(store):
    from types import SimpleNamespace
    from crew.core.envelope import Envelope, ResponseChunk
    from crew.team.collaboration_runtime import TeamExecutionAdapter
    from crew.team.history_projection import project_collaboration_history

    package = store.claim('owner', 'task')
    async def delegate(_session, **kwargs):
        relay = kwargs['on_child_chunk']
        relay('kimi', ResponseChunk.thinking_event('r', '先检查材料'))
        relay('kimi', ResponseChunk.tool_event('r', 'read', 'start', tool_call_id='read-1', args='{"path":"source.md"}'))
        relay('kimi', ResponseChunk.tool_event('r', 'read', 'result', detail='读取完成', tool_call_id='read-1'))
        relay('kimi', ResponseChunk.delta('r', '正在整理证据'))
        return {'ok': True, 'status': 'completed', 'output': '最终结果', 'member': 'kimi', 'task_id': 'child'}

    host = SimpleNamespace(_collaboration_execution_store=store, _team_member_display_name=lambda *_: 'kimi',
                           _recorded_team_internal_chunk=lambda *_a, **kw: kw, _team_internal_chunk=lambda *_a, **kw: kw,
                           _team_delegate_cwd=lambda *_a, **_kw: '', request_delegate=delegate)
    node = SimpleNamespace(node_id='a', assignee='kimi', title='审阅材料', metadata={}, delegate_task_id='')
    adapter = TeamExecutionAdapter(host, Envelope.of('review', session_id='session', user_id='owner'),
        SimpleNamespace(teammates={'kimi': SimpleNamespace(tool_filter=set())}),
        SimpleNamespace(nodes={'a': node}, edges=[], goal='review'), '', lambda _: None)
    result = await adapter.execute(package)
    store.finish('owner', 'task', package['attempt_id'], state='completed', result=result)
    history = project_collaboration_history([store.get('owner', 'task')], [], {})
    reply = next(item for item in history if item['event_type'] == 'team_submit')
    assert reply['content'] == '最终结果'
    assert reply['thinking'] == '先检查材料'
    assert reply['process_text'] == '正在整理证据'
    assert reply['tool_calls'][0]['id'] == 'read-1'
    assert reply['tool_calls'][0]['result'] == '读取完成'


@pytest.mark.asyncio
async def test_adapter_keeps_recorded_process_when_execution_fails(store):
    from types import SimpleNamespace
    from crew.core.envelope import Envelope, ResponseChunk
    from crew.team.collaboration_runtime import TeamExecutionAdapter
    from crew.team.history_projection import project_collaboration_history

    package = store.claim('owner', 'task')

    async def delegate(_session, **kwargs):
        kwargs['on_child_chunk']('kimi', ResponseChunk.thinking_event('r', '检查材料'))
        raise RuntimeError('执行中断')

    host = SimpleNamespace(_collaboration_execution_store=store, _team_member_display_name=lambda *_: 'kimi',
                           _recorded_team_internal_chunk=lambda *_a, **kw: kw, _team_internal_chunk=lambda *_a, **kw: kw,
                           _team_delegate_cwd=lambda *_a, **_kw: '', request_delegate=delegate)
    node = SimpleNamespace(node_id='a', assignee='kimi', title='审阅材料', metadata={}, delegate_task_id='')
    adapter = TeamExecutionAdapter(host, Envelope.of('review', session_id='session', user_id='owner'),
        SimpleNamespace(teammates={'kimi': SimpleNamespace(tool_filter=set())}),
        SimpleNamespace(nodes={'a': node}, edges=[], goal='review'), '', lambda _: None)
    with pytest.raises(RuntimeError, match='执行中断'):
        await adapter.execute(package)
    store.finish('owner', 'task', package['attempt_id'], state='failed', error='执行中断')
    history = project_collaboration_history([store.get('owner', 'task')], [], {})
    reply = next(item for item in history if item['event_type'] == 'team_submit')
    assert reply['thinking'] == '检查材料'
    assert '执行中断' in reply['content']


def test_unknown_retry_requires_confirmation_and_history_preserved(store):
    a = store.claim('owner', 'task')
    b = store.claim('owner', 'task')
    store.finish('owner', 'task', b['attempt_id'], state='completed', result={'text': 'ok', 'executor': 'hermes'})
    store.recover('owner', 'task')
    with pytest.raises(ValueError, match='核实'):
        store.control('owner', 'task', 'retry', unit_id='a')
    store.control('owner', 'task', 'retry', unit_id='a', confirm_unknown=True)
    new = store.claim('owner', 'task')
    assert new['attempt_id'] != a['attempt_id']
    assert store.get('owner', 'task')['attempts'][0]['state'] == 'unknown'


def test_evidence_contract_rejects_fabricated_quote_and_official_ranking():
    from crew.collaboration.review import DIMENSIONS, validate_review
    sources = {'A1.md': '## 摘要\n这是八个字以上的实际来源原文。'}
    report = {'fictional': True, 'human_decision_required': True, 'ranking': ['A'],
        'projects': [{'id': k, 'dimensions': [{'dimension': d, 'claim': 'c', 'analysis': 'a',
          'uncertainty': 'u', 'strength': '弱', 'sources': [{'file': 'A1.md', 'section': '摘要',
          'quote': '这是模型编造的八个字以上摘录。'}]} for d in DIMENSIONS]} for k in 'ABC'],
        'questions': ['a', 'b', 'c']}
    errors = validate_review(report, sources)
    assert any('排名' in e for e in errors)
    assert any('并非' in e for e in errors)
    assert any('其他项目' in e for e in errors)


def test_cancelled_and_completed_are_not_reopened_by_pause(store):
    store.control('owner', 'task', 'cancel')
    with pytest.raises(ValueError, match='不能再次'):
        store.control('owner', 'task', 'pause')
    assert store.get('owner', 'task')['state'] == 'cancelled'


def test_unknown_with_independent_pending_can_be_explicitly_retried(store):
    store.claim('owner', 'task', concurrency=1)
    store.recover('owner', 'task')
    store.control('owner', 'task', 'retry', unit_id='a', confirm_unknown=True)
    assert store.claim('owner', 'task')['unit_id'] == 'a'


@pytest.mark.parametrize('report', [None, [], {'projects': [None, None, None]},
    {'projects': [{'id': []}, {'id': 'B'}, {'id': 'C'}]},
    {'projects': [{'id': c, 'dimensions': [None] * 4} for c in 'ABC']}])
def test_review_contract_reports_malformed_data_without_crashing(report):
    from crew.collaboration.review import validate_review
    assert validate_review(report, {})


def test_review_plan_requires_real_join_not_prose_only():
    from crew.collaboration.review import validate_review_plan
    from crew.collaboration.planning.plan import PlanningError
    units = [{'key': k, 'depends_on': deps} for k, deps in [
        ('sources', []), ('tech', ['sources']), ('biz', ['sources']), ('final', ['tech', 'biz'])]]
    validate_review_plan(units)
    units[-1]['depends_on'] = []
    with pytest.raises(PlanningError, match='depends_on'):
        validate_review_plan(units)
    units[-1]['depends_on'] = ['tech', 'biz']
    for u in units:
        u['unit_id'] = u['key']
    assignments = [{'unit_id': u['key'], 'member_id': 'lead'} for u in units]
    with pytest.raises(PlanningError, match='两个不同成员'):
        validate_review_plan(units, assignments=assignments, leader_id='lead')


@pytest.mark.asyncio
async def test_rejected_evidence_is_auditable_but_blocks_dependents(store):
    from crew.collaboration.runtime.scheduler import RejectedResult
    async def execute(package):
        if package['unit_id'] == 'a':
            raise RejectedResult('引用不符', {'text': '模型实际无效输出', 'executor': 'kimi', 'validation': 'rejected'})
        return {'text': '有效输出', 'executor': package['member_id']}
    task = await Scheduler(store, execute).run('owner', 'task')
    assert task['state'] == 'partially_completed'
    assert task['attempts'][0]['result']['text'] == '模型实际无效输出'
    assert task['attempts'][0]['state'] == 'failed'
    assert task['units'][-1]['state'] == 'blocked'
