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
