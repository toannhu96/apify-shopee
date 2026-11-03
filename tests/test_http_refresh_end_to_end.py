"""Browser handoff -> HTTP transport -> FastAPI -> real encrypted PostgreSQL."""
import json
import os
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from shopee_affiliate import session_signer
from shopee_service import worker
from shopee_service.api import create_app
from shopee_service.browser_capture import Capture
from shopee_service.browser_refresh import HttpRemote, RefreshError, refresh_once
from shopee_service.repository import Repository
from shopee_service.secrets import Cipher

TOKEN = token_urlsafe(32)
PROXY = 'http://synthetic-user:synthetic-password@proxy.example:8080'
COOKIE = 'session=synthetic-new-browser-cookie'


@pytest.fixture
def managed_refresh(monkeypatch):
    dsn = os.environ.get('SH_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set SH_TEST_DATABASE_URL to a disposable PostgreSQL database')
    schema = 'http_refresh_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
    try:
        repo = Repository(make_conninfo(dsn, options=f'-c search_path={schema}'), Cipher(Fernet.generate_key()))
        repo.migrate()
        fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
        headers = {**fixture['before_headers'], 'af-ac-enc-sz-token': 'synthetic-token',
                   'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21'}
        monkeypatch.setattr(session_signer.time, 'time_ns', lambda: 1800000000000000000)
        state = session_signer.record_response(session_signer.initialize_session(headers))
        repo.replace_account('primary', {'cookie_source': 'session=synthetic-old-cookie',
                                         'signing_state': state, 'proxy_url': PROXY})
        with repo._tx() as conn:
            conn.execute("UPDATE accounts SET status='authentication'")
        yield repo, headers
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA {schema} CASCADE')


def api_bridge(client, events):
    def dispatch(request):
        events.append((request.method, request.url.path))
        response = client.request(request.method, request.url.path, content=request.content,
                                  headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))
    return HttpRemote('https://service.example', TOKEN, transport=httpx.MockTransport(dispatch))


def install_fetch(monkeypatch):
    calls = []
    original = worker.verify_account
    def fetch(item, **kwargs):
        calls.append({'item': item, **kwargs})
        return {'product': {'item_id': item, 'shop_id': '42', 'name': 'Synthetic product',
                            'price_vnd': 48500, 'default_commission_rate_pct': 22.5}}
    def verify(repo, alias, item, **kwargs):
        return original(repo, alias, item, fetch=fetch, **kwargs)
    monkeypatch.setattr(worker, 'verify_account', verify)
    return calls


def stored_account(repo):
    with repo._tx() as conn:
        return conn.execute("SELECT * FROM accounts WHERE alias='primary'").fetchone()


def test_browser_session_activates_encrypted_account_and_preserves_proxy(managed_refresh, monkeypatch):
    repo, headers = managed_refresh
    calls = install_fetch(monkeypatch)
    events = []
    app = create_app(repository=repo, api_token='a' * 40, session_refresh_token=TOKEN)
    with TestClient(app) as client:
        remote = api_bridge(client, events)
        result = refresh_once(remote, 'primary', '123', 'http://localhost:9222',
                              capture=lambda *_: Capture(COOKIE, headers))
        assert result == {'alias': 'primary', 'version': 2, 'status': 'healthy', 'verified': True}
        assert remote.account('primary') == {key: result[key] for key in ('alias', 'version', 'status')}
    assert events[:2] == [('GET', '/v1/admin/accounts/primary'), ('POST', '/v1/admin/accounts/primary/session')]
    assert len(calls) == 1 and calls[0]['cookie_source'] == COOKIE and calls[0]['proxy_url'] == PROXY
    row = stored_account(repo)
    assert row['version'] == 2 and row['status'] == 'healthy' and row['lease_token'] is None
    assert COOKIE not in row['credentials'] and PROXY not in row['credentials']
    credentials = repo.cipher.decrypt(row['credentials'])
    assert credentials['cookie_source'] == COOKIE and credentials['proxy_url'] == PROXY
    assert credentials['signing_state']
    assert COOKIE not in json.dumps(result) and PROXY not in json.dumps(result)
    with repo._tx() as conn:
        cache = conn.execute("SELECT account_version FROM product_cache WHERE item_id='123'").fetchone()
    assert cache['account_version'] == 2


def test_stale_capture_is_rejected_before_upstream_request(managed_refresh, monkeypatch):
    repo, headers = managed_refresh
    calls = install_fetch(monkeypatch)
    original = repo.cipher.decrypt(stored_account(repo)['credentials'])
    events = []
    def capture(*_):
        repo.replace_account('primary', {**original, 'cookie_source': 'session=synthetic-concurrent'},
                             expected_version=1, require_existing=True)
        return Capture(COOKIE, headers)
    app = create_app(repository=repo, api_token='a' * 40, session_refresh_token=TOKEN)
    with TestClient(app) as client, pytest.raises(RefreshError, match='HTTP 409'):
        refresh_once(api_bridge(client, events), 'primary', '123', 'http://localhost:9222', capture=capture)
    assert not calls
    assert len(events) == 2
    row = stored_account(repo)
    assert row['version'] == 2 and row['status'] == 'pending'
    assert repo.cipher.decrypt(row['credentials'])['cookie_source'] == 'session=synthetic-concurrent'


def test_replacement_during_verification_does_not_fetch_wrong_version(managed_refresh, monkeypatch):
    repo, headers = managed_refresh
    original_verify = worker.verify_account
    def racing_verify(repo, alias, item, **kwargs):
        assert kwargs['expected_version'] == 2
        replacement = repo.cipher.decrypt(stored_account(repo)['credentials'])
        repo.replace_account(alias, {**replacement, 'cookie_source': 'session=synthetic-newer'},
                             expected_version=2, require_existing=True)
        return original_verify(repo, alias, item, fetch=lambda *_args, **_kwargs: pytest.fail('stale verification fetched'),
                               **kwargs)
    monkeypatch.setattr(worker, 'verify_account', racing_verify)
    app = create_app(repository=repo, api_token='a' * 40, session_refresh_token=TOKEN)
    with TestClient(app) as client, pytest.raises(RefreshError, match='healthy verification'):
        refresh_once(api_bridge(client, []), 'primary', '123', 'http://localhost:9222',
                     capture=lambda *_: Capture(COOKIE, headers))
    row = stored_account(repo)
    assert row['version'] == 3 and row['status'] == 'pending' and row['lease_token'] is None
    assert repo.cipher.decrypt(row['credentials'])['cookie_source'] == 'session=synthetic-newer'
