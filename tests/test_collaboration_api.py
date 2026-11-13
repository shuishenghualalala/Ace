"""HTTP projection/control tests; authentication is stubbed, never live acceptance."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.collaboration.runtime.store import ExecutionStore
from crew.gateway.routers import collaboration_execution


def test_api_scopes_read_and_control_to_authenticated_owner(tmp_path, monkeypatch):
    path = str(tmp_path / 'state.db')
    store = ExecutionStore(path)
    store.admit('alice', {'task': {'task_id': 't', 'session_id': 's', 'goal': 'review'},
        'collaboration_plan_id': 'p', 'nodes': [{'id': 'a', 'title': 'a', 'detail': 'a', 'assignee': 'leader'}],
        'edges': []}, require_planned=False)
    monkeypatch.setattr(collaboration_execution, 'account_from_request',
        lambda request: SimpleNamespace(owner_account_id=request.headers.get('x-test-owner', 'alice')))
    app = FastAPI()
    app.include_router(collaboration_execution.create_collaboration_execution_router(SimpleNamespace(
        config=SimpleNamespace(db_path=path, sqlite_wal=True),
        team=SimpleNamespace(_collaboration_execution_store=store))))
    with TestClient(app) as client:
        assert client.get('/api/collaboration/executions/t').status_code == 200
        assert client.get('/api/collaboration/executions/t', headers={'x-test-owner': 'bob'}).status_code == 404
        assert client.get('/api/collaboration/sessions/s/executions', headers={'x-test-owner': 'bob'}).json()['executions'] == []
        assert client.post('/api/collaboration/executions/t/control', headers={'x-test-owner': 'bob'},
            json={'action': 'cancel', 'owner': 'alice'}).status_code == 404
        assert store.get('alice', 't')['state'] == 'running'
        assert client.post('/api/collaboration/executions/t/control', json={'action': 'pause'}).status_code == 200
        assert store.get('alice', 't')['state'] == 'paused'
        assert client.post('/api/collaboration/executions/t/control', json={'action': 'unsupported'}).status_code == 409
    store.close()
