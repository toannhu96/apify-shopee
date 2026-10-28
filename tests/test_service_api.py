"""API boundary tests use a fake repository and never network."""
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe

from fastapi.testclient import TestClient

from shopee_service.api import create_app
from shopee_service.repository import Conflict, Missing

TOKEN = token_urlsafe(32)
AUTH = {'Authorization': 'Bearer ' + TOKEN}


class FakeRepository:
    def __init__(self):
        self.calls = []
    def submit_run(self, run, ids, deadline):
        self.calls.append((run, ids, deadline))
        if run == 'conflict':
            raise Conflict('private=secret')
        return {'runId': run, 'done': False}
    def get_run(self, run, **kwargs):
        if run == 'missing':
            raise Missing('private=secret')
        if run == 'broken':
            raise RuntimeError('password=secret')
        return {'runId': run, 'kwargs': kwargs}
    def cancel_run(self, run, reason):
        return {'runId': run, 'reason': reason}
    def get_deliveries(self, run):
        return {'deliveries': []}
    def record_delivery(self, run, item, phase, result):
        return {'itemId': item, 'phase': phase, 'resultId': result}
    def health(self):
        return True


def test_auth_validation_and_idempotent_contract():
    repo = FakeRepository()
    with TestClient(create_app(repository=repo, api_token=TOKEN)) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/v1/runs/run').status_code == 401
        assert client.get('/v1/runs/run', headers={'Authorization':'Bearer nope'}).status_code == 401
        body = {'runId':'run1','productIds':['123'],'deadlineAt':(datetime.now(UTC)+timedelta(hours=1)).isoformat()}
        assert client.post('/v1/runs',headers=AUTH,json=body).status_code == 200
        assert repo.calls[0][1] == ['123']
        for change in ({'productIds':[123]}, {'productIds':['123']*1001}, {'extra':'secret'}, {'runId':'../bad'}):
            response = client.post('/v1/runs',headers=AUTH,json={**body,**change})
            assert response.status_code == 422
            assert 'secret' not in response.text
        assert client.get('/v1/runs/missing',headers=AUTH).status_code == 404
        assert client.post('/v1/runs',headers=AUTH,json={**body,'runId':'conflict'}).status_code == 409
        response = client.get('/v1/runs/broken',headers=AUTH)
        assert response.status_code == 503 and 'secret' not in response.text
        assert client.get('/v1/runs/run1?cursor=-1',headers=AUTH).status_code == 422
        assert client.get('/v1/runs/run1/deliveries',headers=AUTH).json() == {'deliveries': []}
        payload = {'phase':'delivering','resultId':'run1:123'}
        assert client.post('/v1/runs/run1/deliveries/123',headers=AUTH,json=payload).json()['phase'] == 'delivering'
        assert client.post('/v1/runs/run1/cancel',headers=AUTH,json={'reason':'cancelled'}).status_code == 200


def test_body_and_request_limits():
    with TestClient(create_app(repository=FakeRepository(),api_token=TOKEN, rate_limit=2)) as client:
        assert client.post('/v1/runs', headers=AUTH, content='x'*70000).status_code == 413
        assert client.get('/v1/runs/run',headers=AUTH).status_code == 200
        assert client.get('/v1/runs/run',headers=AUTH).status_code == 200
        assert client.get('/v1/runs/run',headers=AUTH).status_code == 429


def test_rate_limit_tells_caller_actual_window():
    with TestClient(create_app(repository=FakeRepository(),api_token=TOKEN,rate_limit=1)) as client:
        assert client.get('/v1/runs/run',headers=AUTH).status_code == 200
        response = client.get('/v1/runs/run',headers=AUTH)
        assert response.status_code == 429
        assert 59 <= int(response.headers['Retry-After']) <= 61
