"""Operator HTTP handoff with synthetic credentials and no external network."""
import json
from pathlib import Path
from secrets import token_urlsafe

import pytest
from fastapi.testclient import TestClient

from shopee_service import cli, worker
from shopee_service.api import create_app
from shopee_service.repository import Conflict, Missing

TOKEN = token_urlsafe(32)
ADMIN = token_urlsafe(32)
AUTH = {'Authorization': 'Bearer ' + ADMIN}
URL = '/v1/admin/accounts/primary/session'


def payload():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    headers = {**fixture['before_headers'], 'af-ac-enc-sz-token': 'synthetic-token',
               'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21'}
    return {'expected_version': 2, 'item_id': '24995347301',
            'cookie_source': 'session=PRIVATE-COOKIE', 'headers': headers}


class Repo:
    def __init__(self):
        self.calls = []
        self.version, self.status = 2, 'authentication'

    def list_accounts(self):
        return [{'alias': 'primary', 'version': self.version, 'status': self.status,
                 'credentials': 'PRIVATE-CREDENTIALS'}]

    def replace_account(self, alias, credentials, **options):
        if options['expected_version'] != self.version or self.status == 'disabled':
            raise Conflict('PRIVATE-CREDENTIALS')
        self.calls.append((alias, credentials, options))
        self.version += 1
        self.status = 'pending'
        return self.version


@pytest.fixture
def setup(monkeypatch):
    from shopee_affiliate import session_signer
    monkeypatch.setattr(session_signer.time, 'time_ns', lambda: 1800000000000000000)
    repo = Repo()
    def verify(repository, alias, item_id, *, expected_version):
        assert (repository, alias, item_id, expected_version) == (repo, 'primary', '24995347301', 3)
        repo.status = 'healthy'
        return True
    monkeypatch.setattr(worker, 'verify_account', verify)
    with TestClient(create_app(repository=repo, api_token=TOKEN, session_refresh_token=ADMIN)) as client:
        yield repo, client


def test_separate_auth_and_disabled_by_default(setup):
    repo, client = setup
    for auth in ({}, {'Authorization': 'Bearer ' + TOKEN}):
        response = client.get('/v1/admin/accounts/primary', headers=auth)
        assert response.status_code == 401 and response.headers['cache-control'] == 'no-store'
        assert client.post(URL, headers=auth, json=payload()).status_code == 401
    assert client.get('/v1/runs/run', headers=AUTH).status_code == 401
    with TestClient(create_app(repository=repo, api_token=TOKEN)) as disabled:
        assert disabled.post(URL, headers=AUTH, json=payload()).status_code == 401
    assert not repo.calls


def test_refresh_preserves_proxy_cas_and_verifies(setup, caplog):
    repo, client = setup
    before = client.get('/v1/admin/accounts/primary', headers=AUTH)
    assert before.json() == {'alias': 'primary', 'version': 2, 'status': 'authentication'}
    response = client.post(URL, headers=AUTH, json=payload())
    assert response.status_code == 200
    assert response.json() == {'alias': 'primary', 'version': 3, 'status': 'healthy', 'verified': True}
    assert repo.calls[0][2] == {'require_existing': True, 'expected_version': 2,
                              'preserve_proxy': True, 'require_enabled': True}
    assert repo.calls[0][1]['cookie_source'] == 'session=PRIVATE-COOKIE'
    assert 'PRIVATE' not in response.text + before.text + caplog.text
    assert response.headers['cache-control'] == 'no-store'
    assert before.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('change', [
    {'expected_version': True}, {'expected_version': 0}, {'expected_version': '2'},
    {'item_id': 123}, {'item_id': '01'}, {'proxy_url': 'https://PRIVATE'},
    {'cookie_source': []}, {'headers': {}}, {'signing_mode': 'PRIVATE'},
])
def test_invalid_payload_never_mutates_or_leaks(setup, change):
    repo, client = setup
    response = client.post(URL, headers=AUTH, json={**payload(), **change})
    assert response.status_code == 422
    assert not repo.calls and 'PRIVATE' not in response.text


@pytest.mark.parametrize('raw', [b'[]', b'{}', b'\xff', b'{"headers":NaN}',
    b'{"expected_version":2,"expected_version":3}', b'{"x":1e999}'])
def test_strict_json(setup, raw):
    repo, client = setup
    assert client.post(URL, headers=AUTH, content=raw).status_code == 422
    assert not repo.calls


def test_bounded_body_including_false_content_length(setup):
    repo, client = setup
    for headers in (AUTH, {**AUTH, 'Content-Length': '1'}):
        assert client.post(URL, headers=headers, content=b'x' * (2 * 1024 * 1024 + 1)).status_code == 413
    assert client.post(URL, headers={**AUTH, 'Content-Encoding': 'gzip'}, content=b'x').status_code == 415
    assert not repo.calls


def test_conflicts_missing_and_failures_are_safe(setup, monkeypatch):
    repo, client = setup
    repo.status = 'disabled'
    assert client.post(URL, headers=AUTH, json=payload()).status_code == 409
    repo.status = 'authentication'
    assert client.post(URL, headers=AUTH, json={**payload(), 'expected_version': 1}).status_code == 409
    assert client.get('/v1/admin/accounts/missing', headers=AUTH).status_code == 404
    for error, status in ((Missing('PRIVATE'), 404), (RuntimeError('PRIVATE'), 503)):
        def fail(*args, error=error, **kwargs):
            raise error
        monkeypatch.setattr(repo, 'replace_account', fail)
        response = client.post(URL, headers=AUTH, json=payload())
        assert response.status_code == status and 'PRIVATE' not in response.text


def test_unverified_and_concurrent_replacement_never_report_success(setup, monkeypatch):
    repo, client = setup
    monkeypatch.setattr(cli, '_wait_for_verification', lambda *args: False)
    response = client.post(URL, headers=AUTH, json=payload())
    assert response.json()['verified'] is False and repo.status == 'pending'
    monkeypatch.setattr(cli, '_wait_for_verification', lambda *args: True)
    def raced(*args, **kwargs):
        repo.version += 1
        repo.status = 'healthy'
        return True
    monkeypatch.setattr(worker, 'verify_account', raced)
    response = client.post(URL, headers=AUTH, json={**payload(), 'expected_version': 3})
    assert response.json()['verified'] is False


def test_refresh_has_separate_rate_limit(setup):
    repo, client = setup
    for _ in range(5):
        assert client.post(URL, headers=AUTH, json={}).status_code == 422
    response = client.post(URL, headers=AUTH, json={})
    assert response.status_code == 429 and 'Retry-After' in response.headers
    assert client.get('/v1/admin/accounts/primary', headers=AUTH).status_code == 200
    assert not repo.calls
