"""Actor -> HTTP API -> real PostgreSQL -> worker; only external effects mocked."""
import asyncio
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg.conninfo import make_conninfo

from shopee_actor.backend import BackendClient, Settings
from shopee_actor.engine import Control, execute
from shopee_service.api import create_app
from shopee_service.repository import Repository
from shopee_service.secrets import Cipher
from shopee_service.worker import process_one


@pytest.fixture
def managed(monkeypatch):
    dsn = os.environ.get('SH_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set SH_TEST_DATABASE_URL to a disposable PostgreSQL database')
    schema = 'e2e_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
    repo = Repository(make_conninfo(dsn, options=f'-c search_path={schema}'), Cipher(Fernet.generate_key()))
    repo.migrate()
    repo.replace_account('test', {'cookie_source': 'session=synthetic', 'signing_state': {'count': 1}})
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET status='healthy'")
    def reserve(_item, state):
        return {'synthetic': 'reserved'}, {**state, 'count': state['count'] + 1}
    monkeypatch.setattr('shopee_affiliate.session_signer.reserve_request', reserve)
    yield repo
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA {schema} CASCADE')


class Storage:
    """In-memory stand-in only for Apify storage and billing APIs."""
    def __init__(self):
        self.state, self.summary = None, None
        self.rows, self.charges = [], []
        self.budget = 1000

    async def load_state(self):
        return self.state

    async def save_state(self, state):
        self.state = state

    async def summarize(self, summary):
        self.summary = summary

    async def existing_result_ids(self):
        return {r['resultId'] for r in self.rows}

    async def push(self, row):
        self.rows.append(row)

    def remaining_budget(self):
        return self.budget

    async def charge(self, key):
        self.charges.append(key)
        self.budget -= 1
        return True


def fetch(item, **_kwargs):
    return {'product': {'item_id': item, 'shop_id': '42', 'name': 'Synthetic product',
                        'price_vnd': 48500, 'default_commission_rate_pct': 22.5,
                        'affiliate_link': 'must-not-leak'}, 'raw': {'private': 'must-not-leak'}}


def run_actor(repo, items, *, port=None, run_id='e2erun', control=None):
    port = port or Storage()
    app = create_app(repository=repo, api_token='a' * 40, rate_limit=10000)
    async def worker_step(_seconds):
        with repo._tx() as conn:
            conn.execute('UPDATE accounts SET next_request_at=now()')
        process_one(repo, fetch=fetch)
        await asyncio.sleep(0)
    async def run():
        async with BackendClient(Settings('https://service.test', 'a' * 40),
                transport=httpx.ASGITransport(app=app), sleep=lambda _: asyncio.sleep(0)) as backend:
            summary = await execute(port, backend, product_ids=items, run_id=run_id,
                                    started_at=datetime.now(UTC), control=control or Control(),
                                    sleep=worker_step)
            return summary
    return port, asyncio.run(run())


def test_worker_delivery_billing_and_cached_second_run(managed):
    first, summary = run_actor(managed, ['123', '456'])
    assert summary['status'] == 'succeeded'
    assert len(first.rows) == len(first.charges) == 2
    assert all(not r['cacheHit'] for r in first.rows)
    assert 'must-not-leak' not in str(first.rows)
    second, summary = run_actor(managed, ['123', '789'], run_id='second')
    assert summary['status'] == 'succeeded'
    assert [r['cacheHit'] for r in second.rows] == [True, False]
    assert len(second.charges) == 2
    with managed._tx() as conn:
        assert conn.execute('SELECT count(*) AS n FROM deliveries WHERE phase=\'charged\'').fetchone()['n'] == 4


def test_restarted_actor_uses_real_ledger_without_duplicate_charge(managed):
    port, _ = run_actor(managed, ['123'])
    _, summary = run_actor(managed, ['123'], port=port)
    assert summary['status'] == 'succeeded'
    assert len(port.rows) == len(port.charges) == 1


def test_all_cached_results_are_delivered_across_api_pages(managed):
    ids = [str(i) for i in range(1, 102)]
    # Populate via the worker and real storage rather than hand-constructed rows.
    _first, summary = run_actor(managed, ids, run_id='populate')
    assert summary['status'] == 'succeeded'
    second, summary = run_actor(managed, ids, run_id='cached')
    assert summary['status'] == 'succeeded'
    assert len(second.rows) == len(second.charges) == 101
    assert all(row['cacheHit'] for row in second.rows)


def test_abort_reason_is_accepted_by_real_api(managed):
    from datetime import timedelta
    managed.submit_run('e2erun', ['123'], datetime.now(UTC) + timedelta(hours=1))
    control = Control()
    control.stop('abort')
    port, summary = run_actor(managed, ['123'], control=control)
    assert summary['reason'] == 'aborted'
    assert not port.rows and not port.charges
    assert managed.get_run('e2erun')['counts']['unfinished'] == 1


def test_real_sdk_local_entrypoint_against_http_server(managed, tmp_path):
    """Exercise actual SDK lifecycle/storage and network transport without billing."""
    import json
    from datetime import timedelta

    managed.submit_run('warmup', ['123'], datetime.now(UTC) + timedelta(hours=1))
    assert process_one(managed, fetch=fetch)
    # A separate server uses the same encrypted test database; no real credentials.
    key = Fernet.generate_key().decode()
    source_root = str(Path(__file__).resolve().parents[1])
    server_code = (
        'import os\nfrom shopee_service.api import create_app\n'
        'from shopee_service.repository import Repository\n'
        'from shopee_service.secrets import Cipher\n'
        'app = create_app(repository=Repository(os.environ["SMOKE_DB"],'
        'Cipher(os.environ["SMOKE_KEY"])), api_token="a"*40)\n'
    )
    (tmp_path / 'smoke_server.py').write_text(server_code)
    (tmp_path / 'INPUT.json').write_text(json.dumps({'productIds': ['123']}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port_number = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(('APIFY_', 'ACTOR_', 'CRAWLEE_', 'SH_'))}
    env.update({'PYTHONPATH': source_root, 'SMOKE_DB': managed.database_url, 'SMOKE_KEY': key})
    with (tmp_path / 'server.log').open('w') as log:
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'smoke_server:app',
            '--host', '127.0.0.1', '--port', str(port_number), '--no-access-log'],
            cwd=tmp_path, env=env, stdout=log, stderr=log)
        try:
            origin = f'http://127.0.0.1:{port_number}'
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get(origin + '/health', timeout=0.2).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail('Local API did not become ready')
            env.update({'SH_BACKEND_URL': origin, 'SH_BACKEND_TOKEN': 'a' * 40,
                        'SH_BACKEND_ALLOW_HTTP': '1', 'APIFY_IS_AT_HOME': '0'})
            completed = subprocess.run([sys.executable, '-m', 'shopee_actor'], check=False, cwd=tmp_path,
                env=env, capture_output=True, text=True, timeout=40)
            assert completed.returncode == 0, 'Actual SDK entrypoint failed; inspect temporary local test artifacts'
            summaries = list(tmp_path.rglob('SUMMARY'))
            assert len(summaries) == 1
            summary = json.loads(summaries[0].read_text())
            assert summary['deliveredCount'] == 1
            assert summary['billingMode'] == 'unbilled-test'
            assert 'must-not-leak' not in completed.stdout + completed.stderr
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
