"""Disposable PostgreSQL integration tests; no external scraping or secrets."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

psycopg = pytest.importorskip('psycopg')
from cryptography.fernet import Fernet
from psycopg.conninfo import make_conninfo

from shopee_service.repository import Conflict, Missing, Repository
from shopee_service.secrets import Cipher


@pytest.fixture
def repo():
    dsn = os.environ.get('SH_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('SH_TEST_DATABASE_URL is required for PostgreSQL integration tests')
    schema = 'test_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
    repository = Repository(make_conninfo(dsn, options=f'-c search_path={schema}'), Cipher(Fernet.generate_key()))
    repository.migrate()
    yield repository
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA {schema} CASCADE')


def deadline():
    return datetime.now(UTC) + timedelta(hours=1)


def test_standalone_seed_reservations_persist_and_keep_original_seed(repo, monkeypatch):
    import json
    from pathlib import Path

    from shopee_affiliate import managed_signing, sap_signer
    headers = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())['before_headers']
    headers = {**headers, 'af-ac-enc-sz-token': 'synthetic-token', 'user-agent': 'Synthetic fixture',
               'x-sz-sdk-version': '1.12.21'}
    state = managed_signing.initialize(headers, mode='standalone-seed')
    calls = []
    def sign(item, seed):
        calls.append((item, dict(seed)))
        return dict(seed)
    monkeypatch.setattr(sap_signer, 'refresh_headers', sign)
    repo.replace_account('primary', {'cookie_source': 'session=synthetic', 'signing_state': state})
    first = repo.claim_verification('primary', '123')
    with repo._tx() as conn:
        stored = conn.execute("SELECT credentials FROM accounts WHERE alias='primary'").fetchone()
    reserved = repo.cipher.decrypt(stored['credentials'])['signing_state']
    assert reserved['sequence'] == 1 and reserved['seed_headers'] == state['seed_headers']
    assert repo.complete_verification(first, response_received=True)
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET next_request_at=now() WHERE alias='primary'")
    repo.submit_run('standalone-second', ['456'], deadline())
    second = repo.claim_job()
    assert second and [item for item, _ in calls] == ['123', '456']
    assert all(seed == state['seed_headers'] for _, seed in calls)
    assert repo.complete_job(second, result(), response_received=True)
    with repo._tx() as conn:
        stored = conn.execute("SELECT credentials FROM accounts WHERE alias='primary'").fetchone()
    completed = repo.cipher.decrypt(stored['credentials'])['signing_state']
    assert completed['sequence'] == 2 and completed['response_count'] == 2
    assert completed['seed_headers'] == state['seed_headers']


def test_proxy_replacement_preserves_session_and_fences_old_work(repo):
    account(repo)
    repo.submit_run('proxy-change', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    with repo._tx() as conn:
        before = conn.execute("SELECT * FROM accounts WHERE alias='primary'").fetchone()
    credentials = repo.cipher.decrypt(before['credentials'])
    proxy = 'https://synthetic:password@proxy.example:8443'
    assert repo.replace_proxy('primary', proxy, expected_version=1) == 2
    with repo._tx() as conn:
        after = conn.execute("SELECT * FROM accounts WHERE alias='primary'").fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE item_id='123'").fetchone()
    assert repo.cipher.decrypt(after['credentials']) == {**credentials, 'proxy_url': proxy}
    assert proxy not in after['credentials']
    assert after['status'] == 'pending' and after['lease_token'] is None
    assert after['next_request_at'] >= before['lease_until']
    assert job['status'] == 'pending' and job['available_at'] >= before['lease_until']
    assert repo.complete_job(work, result()) is False


def test_proxy_replacement_rejects_stale_disabled_missing_and_invalid(repo):
    account(repo)
    proxy = 'https://proxy.example:8443'
    with pytest.raises(Conflict):
        repo.replace_proxy('primary', proxy, expected_version=2)
    with pytest.raises(Missing):
        repo.replace_proxy('missing', proxy, expected_version=1)
    for version in (None, 0, -1, True, '1'):
        with pytest.raises(ValueError):
            repo.replace_proxy('primary', proxy, expected_version=version)
    for invalid in (None, '', 'file:///tmp/proxy', 'https://proxy.example/path'):
        with pytest.raises(ValueError):
            repo.replace_proxy('primary', invalid, expected_version=1)
    repo.disable_account('primary')
    with pytest.raises(Conflict):
        repo.replace_proxy('primary', proxy, expected_version=1)
    assert repo.list_accounts()[0]['version'] == 1


def test_proxy_replacement_invalidates_cached_product(repo):
    account(repo)
    repo.submit_run('proxy-cache', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    assert repo.complete_job(work, result())
    assert repo.replace_proxy('primary', 'https://proxy.example:8443', expected_version=1) == 2
    with repo._tx() as conn:
        assert conn.execute('SELECT count(*) AS n FROM product_cache').fetchone()['n'] == 0


def test_concurrent_proxy_changes_only_one_wins(repo):
    from concurrent.futures import ThreadPoolExecutor
    account(repo)
    def update(index):
        try:
            return repo.replace_proxy('primary', f'https://proxy{index}.example:8443', expected_version=1)
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, [1, 2]))
    assert sorted(outcomes, key=str) == [2, 'conflict']


def account(repo):
    repo.replace_account('primary', {'cookie_source': 'session=synthetic', 'signing_state': {'counter': 1}, 'proxy_url': None})
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET status='healthy' WHERE alias='primary'")


def reserve(_item, state):
    return {'synthetic': 'secret'}, {**state, 'counter': state['counter'] + 1}


def result(item='123'):
    return {'item_id': item, 'name': 'Fixture', 'schemaVersion': 1, 'fetchedAt': datetime.now(UTC).isoformat(), 'cacheHit': False}


def test_submit_idempotency_and_immutable_inputs(repo):
    account(repo)
    end = deadline()
    first = repo.submit_run('run1', ['123', '456'], end)
    assert first['done'] is False
    assert repo.submit_run('run1', ['123', '456'], end)['runId'] == 'run1'
    with pytest.raises(Conflict):
        repo.submit_run('run1', ['999'], end)
    with pytest.raises(Conflict):
        repo.submit_run('run1', ['123', '456'], end + timedelta(seconds=1))


def test_no_accounts_fails_without_waiting(repo):
    response = repo.submit_run('run1', ['123'], deadline())
    assert response['done']
    assert response['results'][0]['errorCode'] == 'session_unavailable'


def test_claim_persists_state_and_fences_stale_completion(repo):
    account(repo)
    repo.submit_run('run1', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    assert work.item_id == '123'
    assert repo.claim_job(reserve=reserve) is None
    with repo._tx() as conn:
        row = conn.execute("SELECT credentials FROM accounts WHERE alias='primary'").fetchone()
    assert repo.cipher.decrypt(row['credentials'])['signing_state']['counter'] == 2
    repo.replace_account('primary', {'cookie_source': 'new=synthetic', 'signing_state': {'counter': 1}, 'proxy_url': None})
    assert repo.complete_job(work, result()) is False


def test_cache_coalescing_pagination_and_renewal_invalidates(repo):
    account(repo)
    repo.submit_run('run1', ['123'], deadline())
    repo.submit_run('run2', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    assert repo.complete_job(work, result())
    for run in ['run1', 'run2']:
        snap = repo.get_run(run)
        assert snap['done'] and snap['counts']['succeeded'] == 1
        assert snap['results'][0]['sequence'] == 1
        assert repo.get_run(run, cursor=1)['results'] == []
    cached = repo.submit_run('run3', ['123'], deadline())
    assert cached['results'][0]['result']['cacheHit'] is True
    repo.replace_account('primary', {'cookie_source': 'new=synthetic', 'signing_state': {'counter': 1}, 'proxy_url': None})
    response = repo.submit_run('run4', ['123'], deadline())
    assert response['results'][0]['errorCode'] == 'session_unavailable'


def test_deadline_and_cancel_retains_completed_results(repo):
    account(repo)
    repo.submit_run('run1', ['123', '456'], deadline())
    work = repo.claim_job(reserve=reserve)
    repo.complete_job(work, result())
    snap = repo.cancel_run('run1', 'cancelled')
    assert snap['done'] and snap['counts'] == {'succeeded': 1, 'failed': 0, 'unfinished': 1, 'pending': 0}
    assert snap['results'][0]['status'] == 'succeeded'
    repo.submit_run('run2', ['789'], deadline())
    with repo._tx() as conn:
        conn.execute("UPDATE runs SET deadline_at=now()-interval '1 second' WHERE run_id='run2'")
    assert repo.get_run('run2')['results'][0]['errorCode'] == 'deadline_exceeded'


def test_account_failures_alert_once_and_transient_retry_bound(repo):
    account(repo)
    repo.submit_run('run1', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    assert repo.fail_job(work, 'transient')
    with repo._tx() as conn:
        conn.execute('UPDATE jobs SET available_at=now()')
        conn.execute('UPDATE accounts SET next_request_at=now()')
    work2 = repo.claim_job(reserve=reserve)
    assert work2.attempt == 2
    repo.fail_job(work2, 'transient')
    assert repo.get_run('run1')['results'][0]['errorCode'] == 'transient'
    repo.submit_run('run2', ['456'], deadline())
    with repo._tx() as conn:
        conn.execute('UPDATE accounts SET next_request_at=now()')
    work3 = repo.claim_job(reserve=reserve)
    repo.fail_job(work3, 'authentication')
    alert = repo.claim_alert()
    assert 'primary' in str(alert['payload'])
    assert 'synthetic' not in str(alert['payload'])
    assert repo.claim_alert() is None
    repo.finish_alert(alert['id'], alert['lease_token'])
    assert repo.claim_alert() is None
    assert repo.list_accounts()[0]['status'] == 'authentication'


def test_billing_ledger_is_monotonic_success_only(repo):
    account(repo)
    repo.submit_run('run1', ['123'], deadline())
    with pytest.raises(Conflict):
        repo.record_delivery('run1', '123', 'delivering', 'run1:123')
    work = repo.claim_job(reserve=reserve)
    repo.complete_job(work, result())
    for phase in ('delivering', 'delivered', 'charging', 'charged'):
        row = repo.record_delivery('run1', '123', phase, 'run1:123')
        assert row['phase'] == phase
        assert repo.record_delivery('run1', '123', phase, 'run1:123')['phase'] == phase
    with pytest.raises(Conflict):
        repo.record_delivery('run1', '123', 'charging', 'run1:123')
    with pytest.raises(Conflict):
        repo.record_delivery('run1', '123', 'charged', 'different')
    assert repo.get_deliveries('run1')['deliveries'][0]['phase'] == 'charged'
    with pytest.raises(Missing):
        repo.get_run('absent')


def test_cached_first_item_does_not_complete_run_before_fresh_item(repo):
    account(repo)
    repo.submit_run('warm', ['123'], deadline())
    repo.complete_job(repo.claim_job(reserve=reserve), result())
    snapshot = repo.submit_run('mixed', ['123', '456'], deadline())
    assert snapshot['status'] == 'active' and snapshot['counts']['pending'] == 1
    with repo._tx() as conn:
        conn.execute('UPDATE accounts SET next_request_at=now()')
    fresh = repo.claim_job(reserve=reserve)
    assert fresh.item_id == '456'
    repo.complete_job(fresh, result('456'))
    assert repo.get_run('mixed')['done']


def test_expired_cache_requires_refetch_and_account_cooldown_is_respected(repo):
    account(repo)
    repo.submit_run('warm', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    repo.complete_job(work, result())
    with repo._tx() as conn:
        conn.execute("UPDATE product_cache SET expires_at=now()-interval '1 second'")
    assert repo.submit_run('fresh', ['123'], deadline())['done'] is False
    assert repo.claim_job(reserve=reserve) is None
    with repo._tx() as conn:
        conn.execute('UPDATE accounts SET next_request_at=now()')
    work = repo.claim_job(reserve=reserve)
    repo.fail_job(work, 'rate_limit')
    assert repo.list_accounts()[0]['status'] == 'rate_limit'
    assert repo.claim_job(reserve=reserve) is None
    assert repo.get_run('fresh')['done'] is False
    with repo._tx() as conn:
        conn.execute('UPDATE accounts SET next_request_at=now()')
    assert repo.claim_job(reserve=reserve) is not None


def test_expired_leases_retry_once_and_fence_original_worker(repo):
    account(repo)
    repo.submit_run('run1', ['123'], deadline())
    first = repo.claim_job(reserve=reserve)
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET lease_until=now()-interval '1 second',next_request_at=now()")
        conn.execute("UPDATE jobs SET lease_until=now()-interval '1 second'")
    second = repo.claim_job(reserve=reserve)
    assert second.attempt == 2 and second.token != first.token
    assert repo.complete_job(first, result()) is False
    assert repo.fail_job(first, 'authentication') is False
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET lease_until=now()-interval '1 second',next_request_at=now()")
        conn.execute("UPDATE jobs SET lease_until=now()-interval '1 second'")
    assert repo.claim_job(reserve=reserve) is None
    assert repo.get_run('run1')['results'][0]['errorCode'] == 'transient'


def test_database_lock_serializes_concurrent_claims(repo):
    from concurrent.futures import ThreadPoolExecutor
    account(repo)
    repo.submit_run('run1', ['123', '456'], deadline())
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: repo.claim_job(reserve=reserve), range(2)))
    assert sum(claim is not None for claim in claims) == 1


def test_verified_product_is_cached_with_original_fetch_time_and_session_version(repo):
    repo.replace_account('primary', {'cookie_source': 'synthetic=1', 'signing_state': {'counter': 1}})
    work = repo.claim_verification('primary', '123', reserve=reserve)
    verified = result()
    assert repo.complete_verification(work, verified)
    snapshot = repo.submit_run('verified', ['123'], deadline())
    assert snapshot['done'] and snapshot['counts']['succeeded'] == 1
    assert snapshot['results'][0]['result'] == {**verified, 'cacheHit': True}
    assert repo.claim_job(reserve=reserve) is None
    with repo._tx() as conn:
        cached = conn.execute('SELECT * FROM product_cache WHERE item_id=%s', ('123',)).fetchone()
        assert cached['account_version'] == work.account_version
        assert 899 <= (cached['expires_at'] - cached['created_at']).total_seconds() <= 900
    repo.replace_account('primary', {'cookie_source': 'new=synthetic', 'signing_state': {'counter': 1}})
    replaced = repo.submit_run('replaced', ['123'], deadline())
    assert replaced['results'][0]['errorCode'] == 'session_unavailable'


@pytest.mark.parametrize('invalidated', ['expired', 'replaced', 'disabled'])
def test_stale_verification_cannot_cache_product(repo, invalidated):
    credentials = {'cookie_source': 'synthetic=1', 'signing_state': {'counter': 1}}
    repo.replace_account('primary', credentials)
    work = repo.claim_verification('primary', '123', reserve=reserve)
    if invalidated == 'replaced':
        repo.replace_account('primary', credentials)
    elif invalidated == 'disabled':
        repo.disable_account('primary')
    else:
        with repo._tx() as conn:
            conn.execute("UPDATE accounts SET lease_until=now()-interval '1 second'")
    assert repo.complete_verification(work, result()) is False
    with repo._tx() as conn:
        assert conn.execute('SELECT count(*) AS n FROM product_cache').fetchone()['n'] == 0


def test_failed_verification_does_not_create_cached_product(repo):
    repo.replace_account('primary', {'cookie_source': 'synthetic=1', 'signing_state': {'counter': 1}})
    work = repo.claim_verification('primary', '123', reserve=reserve)
    assert repo.fail_verification(work, 'verification')
    with repo._tx() as conn:
        assert conn.execute('SELECT count(*) AS n FROM product_cache').fetchone()['n'] == 0
    assert repo.submit_run('failed', ['123'], deadline())['results'][0]['errorCode'] == 'session_unavailable'


def test_pending_account_verification_fencing_and_recovery_alert(repo):
    version = repo.replace_account('primary', {'cookie_source':'synthetic=1', 'signing_state':{'counter':1},'proxy_url':None})
    assert version == 1
    work = repo.claim_verification('primary', '123', reserve=reserve)
    assert work.verification
    assert repo.claim_verification('primary', '123', reserve=reserve) is None
    assert repo.complete_verification(work)
    assert repo.list_accounts()[0]['status'] == 'healthy'
    assert repo.complete_verification(work) is False
    alert = repo.claim_alert()
    assert 'validated' in alert['payload']['content']
    repo.finish_alert(alert['id'], alert['lease_token'], error='delivery_failed', retry_after=1)
    assert repo.claim_alert() is None
    with repo._tx() as conn:
        conn.execute('UPDATE alert_outbox SET available_at=now()')
    retry = repo.claim_alert()
    assert retry['attempts'] == 2
    repo.finish_alert(retry['id'],retry['lease_token'],error='delivery_rejected')
    assert repo.claim_alert() is None
    repo.disable_account('primary')
    with repo._tx() as conn:
        conn.execute('UPDATE accounts SET next_request_at=now()')
    with pytest.raises(Conflict):
        repo.claim_verification('primary','123',reserve=reserve)
    with pytest.raises(Missing):
        repo.disable_account('absent')
    with pytest.raises(Missing):
        repo.claim_verification('absent','123',reserve=reserve)


def test_failed_verification_and_corrupt_signer_pause_account(repo):
    account(repo)
    work = repo.claim_verification('primary','123',reserve=reserve)
    assert repo.fail_verification(work,'authentication')
    assert repo.fail_verification(work,'authentication') is False
    assert repo.list_accounts()[0]['status'] == 'authentication'
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET status='healthy',next_request_at=now()")
    repo.submit_run('run1',['123'],deadline())
    def corrupt(_item,_state):
        raise ValueError('sensitive data')
    assert repo.claim_job(reserve=corrupt) is None
    assert repo.list_accounts()[0]['status'] == 'verification'


def test_disable_fences_inflight_and_retention_never_deletes_active(repo):
    account(repo)
    repo.submit_run('active',['123'],deadline())
    work = repo.claim_job(reserve=reserve)
    repo.disable_account('primary')
    assert repo.complete_job(work,result()) is False
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET status='healthy'")
        conn.execute("UPDATE runs SET created_at=now()-interval '8 days'")
    repo.cleanup()
    assert repo.get_run('active')['done'] is False
    repo.cancel_run('active')
    with repo._tx() as conn:
        conn.execute("UPDATE runs SET completed_at=now()-interval '8 days'")
    repo.cleanup()
    with pytest.raises(Missing):
        repo.get_run('active')
    assert repo.health()


@pytest.mark.parametrize('ids,end', [([],None), (['123','123'],None), (['bad'],None), (['123'],datetime(2026,1,1)), (['123'],datetime.now(UTC)-timedelta(seconds=1))])
def test_repository_rejects_bad_boundaries(repo,ids,end):
    with pytest.raises(ValueError):
        repo.submit_run('run1', ids, end or deadline())


def test_error_paths_for_delivery_and_pagination(repo):
    with pytest.raises(ValueError):
        repo.get_run('../invalid')
    with pytest.raises(ValueError):
        repo.get_run('run1',limit=0)
    with pytest.raises(Missing):
        repo.cancel_run('missing')
    with pytest.raises(Missing):
        repo.get_deliveries('missing')
    with pytest.raises(ValueError):
        repo.record_delivery('run1','123','invalid','id')
    with pytest.raises(ValueError):
        repo.record_delivery('run1','123','delivering','\n')
    with pytest.raises(Missing):
        repo.record_delivery('run1','123','delivering','id')


@pytest.mark.parametrize('code', ['transient','unavailable','invalid_response'])
def test_verification_non_auth_failures_do_not_request_session_refresh(repo,code):
    account(repo)
    work = repo.claim_verification('primary','123',reserve=reserve)
    assert repo.fail_verification(work, code)
    assert repo.list_accounts()[0]['status'] == 'healthy'
    assert repo.claim_alert() is None


def test_predispatch_guard_rejects_stale_near_expiry_and_cancelled_work(repo):
    account(repo)
    repo.submit_run('run1',['123'],deadline())
    work = repo.claim_job(reserve=reserve)
    assert repo.reservation_current(work)
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET lease_until=now()+interval '10 seconds'")
    assert repo.reservation_current(work) is False
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET lease_until=now()+interval '90 seconds'")
    repo.cancel_run('run1')
    assert repo.reservation_current(work) is False


def test_create_only_and_replace_only_are_atomic_and_preserve_credentials(repo):
    original = {'cookie_source':'original=synthetic','signing_state':{'counter':1},'proxy_url':None}
    replacement = {'cookie_source':'replacement=synthetic','signing_state':{'counter':9},'proxy_url':None}
    assert repo.replace_account('primary',original,create_only=True) == 1
    with pytest.raises(Conflict):
        repo.replace_account('primary',replacement,create_only=True)
    with pytest.raises(Missing):
        repo.replace_account('typo',replacement,require_existing=True)
    with repo._tx() as conn:
        current = conn.execute("SELECT * FROM accounts WHERE alias='primary'").fetchone()
        assert current['version'] == 1
        assert repo.cipher.decrypt(current['credentials']) == original
        assert conn.execute("SELECT 1 FROM accounts WHERE alias='typo'").fetchone() is None
    assert repo.replace_account('primary',replacement,require_existing=True) == 2


def test_simultaneous_account_creates_cannot_replace_first_session(repo):
    from concurrent.futures import ThreadPoolExecutor
    def create(index):
        try:
            return repo.replace_account('primary',{'cookie_source':f'synthetic={index}','signing_state':{'counter':index},'proxy_url':None},create_only=True)
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create,[1,2]))
    assert sorted(results,key=str) == [1,'conflict']
    assert repo.list_accounts()[0]['version'] == 1


def test_alert_time_matches_persisted_account_transition(repo):
    account(repo)
    work = repo.claim_verification('primary','123',reserve=reserve)
    assert repo.complete_verification(work)
    alert = repo.claim_alert()
    timestamp = repo.list_accounts()[0]['updated_at'].astimezone(UTC).isoformat()
    assert 'Time: ' + timestamp in alert['payload']['content']


@pytest.mark.parametrize('verification', [False, True])
@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('received', [False, True])
def test_response_history_commits_once_with_fenced_outcome(repo, monkeypatch, verification, failure, received):
    from unittest.mock import Mock

    from shopee_affiliate import session_signer
    record = Mock(side_effect=lambda state: {**state, 'completed': state.get('completed', 0) + 1})
    monkeypatch.setattr(session_signer, 'record_response', record)
    account(repo)
    if verification:
        work = repo.claim_verification('primary', '123', reserve=reserve)
        complete = lambda: (repo.fail_verification(work, 'verification', response_received=received)
                           if failure else repo.complete_verification(work, response_received=received))
    else:
        repo.submit_run('response-test', ['123'], deadline())
        work = repo.claim_job(reserve=reserve)
        complete = lambda: (repo.fail_job(work, 'verification', response_received=received)
                           if failure else repo.complete_job(work, result(), response_received=received))
    assert complete() is not False
    assert complete() is False
    with repo._tx() as conn:
        stored = conn.execute('SELECT credentials FROM accounts WHERE alias=%s', ('primary',)).fetchone()
    state = repo.cipher.decrypt(stored['credentials'])['signing_state']
    assert state.get('completed', 0) == int(received)
    assert record.call_count == int(received)


def test_response_history_and_job_result_roll_back_together(repo, monkeypatch):
    from shopee_affiliate import session_signer
    monkeypatch.setattr(session_signer, 'record_response', lambda state: {**state, 'completed': 1})
    account(repo)
    repo.submit_run('response-rollback', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    monkeypatch.setattr(repo, '_fanout', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('fixture')))
    with pytest.raises(RuntimeError):
        repo.complete_job(work, result(), response_received=True, response_cookie_source='session=rotated-secret')
    with repo._tx() as conn:
        stored = conn.execute('SELECT credentials,lease_token FROM accounts WHERE alias=%s', ('primary',)).fetchone()
    assert 'completed' not in repo.cipher.decrypt(stored['credentials'])['signing_state']
    assert repo.cipher.decrypt(stored['credentials'])['cookie_source'] == 'session=synthetic'
    assert stored['lease_token'] == work.token


def test_stale_response_cannot_advance_replacement_session(repo, monkeypatch):
    from unittest.mock import Mock

    from shopee_affiliate import session_signer
    record = Mock()
    monkeypatch.setattr(session_signer, 'record_response', record)
    account(repo)
    repo.submit_run('response-stale', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    repo.replace_account('primary', {'cookie_source': 'new=synthetic', 'signing_state': {'counter': 1}})
    assert repo.complete_job(work, result(), response_received=True) is False
    assert repo.fail_job(work, 'verification', response_received=True) is False
    record.assert_not_called()


@pytest.mark.parametrize('verification', [False, True])
@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('received', [False, True])
def test_response_cookies_commit_with_counters_once(repo, monkeypatch, verification, failure, received):
    import json

    from shopee_affiliate import session_signer
    monkeypatch.setattr(session_signer, 'record_response', lambda state: {**state, 'completed': 1})
    account(repo)
    if verification:
        work = repo.claim_verification('primary', '123', reserve=reserve)
    else:
        repo.submit_run('cookie-response', ['123'], deadline())
        work = repo.claim_job(reserve=reserve)
    kwargs = {'response_received': received, 'response_cookie_source': 'session=rotated-secret'}
    if verification:
        finish = lambda: (repo.fail_verification(work, 'invalid_response', **kwargs)
                          if failure else repo.complete_verification(work, **kwargs))
    else:
        finish = lambda: (repo.fail_job(work, 'invalid_response', **kwargs)
                          if failure else repo.complete_job(work, result(), **kwargs))
    assert finish() is True
    assert finish() is False
    with repo._tx() as conn:
        stored = conn.execute("SELECT credentials FROM accounts WHERE alias='primary'").fetchone()
        conn.execute('UPDATE accounts SET next_request_at=now()')
    credentials = repo.cipher.decrypt(stored['credentials'])
    assert credentials['signing_state'] == {'counter': 2, **({'completed': 1} if received else {})}
    if received:
        assert json.loads(credentials['cookie_source'])[0]['value'] == 'rotated-secret'
    else:
        assert credentials['cookie_source'] == 'session=synthetic'
    next_work = repo.claim_verification('primary', '456', reserve=reserve)
    assert next_work.cookie_source == credentials['cookie_source']


@pytest.mark.parametrize('source', ['[]', 'bad-secret\r\nheader=1'])
def test_empty_cookie_deletion_is_valid_but_malformed_source_rolls_back(repo, monkeypatch, source):
    from shopee_affiliate import session_signer
    monkeypatch.setattr(session_signer, 'record_response', lambda state: {**state, 'completed': 1})
    account(repo)
    repo.submit_run('cookie-validation', ['123'], deadline())
    work = repo.claim_job(reserve=reserve)
    if source == '[]':
        assert repo.complete_job(work, result(), response_received=True, response_cookie_source=source)
    else:
        with pytest.raises(ValueError) as error:
            repo.complete_job(work, result(), response_received=True, response_cookie_source=source)
        assert 'bad-secret' not in str(error.value)
    with repo._tx() as conn:
        stored = conn.execute("SELECT credentials,lease_token FROM accounts WHERE alias='primary'").fetchone()
    credentials = repo.cipher.decrypt(stored['credentials'])
    if source == '[]':
        assert credentials['cookie_source'] == '[]'
        assert credentials['signing_state']['completed'] == 1
    else:
        assert credentials['cookie_source'] == 'session=synthetic'
        assert 'completed' not in credentials['signing_state']
        assert stored['lease_token'] == work.token


@pytest.mark.parametrize('verification', [False, True])
def test_stale_cookie_response_cannot_replace_new_session(repo, monkeypatch, verification):
    from shopee_affiliate import session_signer
    monkeypatch.setattr(session_signer, 'record_response', lambda state: pytest.fail('Stale work must not record a response'))
    account(repo)
    if verification:
        work = repo.claim_verification('primary', '123', reserve=reserve)
    else:
        repo.submit_run('cookie-stale', ['123'], deadline())
        work = repo.claim_job(reserve=reserve)
    replacement = {'cookie_source': 'new=synthetic', 'signing_state': {'counter': 9}}
    repo.replace_account('primary', replacement)
    complete = repo.complete_verification if verification else repo.complete_job
    fail = repo.fail_verification if verification else repo.fail_job
    for method, argument in [(complete, result()), (fail, 'verification')]:
        assert method(work, argument, response_received=True, response_cookie_source='stale=secret') is False
    with repo._tx() as conn:
        stored = conn.execute("SELECT credentials FROM accounts WHERE alias='primary'").fetchone()
    assert repo.cipher.decrypt(stored['credentials']) == replacement


def test_account_refresh_compare_and_swap_preserves_proxy_and_rejects_stale_capture(repo):
    original = {'cookie_source': 'old=synthetic', 'signing_state': {'counter': 1}, 'proxy_url': 'http://synthetic.test:8080'}
    fresh = {'cookie_source': 'fresh=synthetic', 'signing_state': {'counter': 9}}
    repo.replace_account('primary', original)
    assert repo.replace_account('primary', fresh, expected_version=1, preserve_proxy=True) == 2
    with pytest.raises(Conflict):
        repo.replace_account('primary', original, expected_version=1, preserve_proxy=True)
    with pytest.raises(Missing):
        repo.replace_account('absent', fresh, expected_version=1, preserve_proxy=True)
    for invalid in (0, -1, True, 1.5, '1'):
        with pytest.raises(ValueError):
            repo.replace_account('primary', original, expected_version=invalid)
    with repo._tx() as conn:
        stored = conn.execute("SELECT version,credentials FROM accounts WHERE alias='primary'").fetchone()
    assert stored['version'] == 2
    assert repo.cipher.decrypt(stored['credentials']) == {**fresh, 'proxy_url': original['proxy_url']}


def test_simultaneous_refreshes_cannot_overwrite_winning_session(repo):
    from concurrent.futures import ThreadPoolExecutor
    account(repo)

    def refresh(index):
        try:
            return repo.replace_account('primary', {'cookie_source': f'synthetic={index}',
                'signing_state': {'counter': index}}, expected_version=1, preserve_proxy=True)
        except Conflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(refresh, [7, 8]))
    assert sorted(outcomes, key=str) == [2, 'conflict']
    winner = 7 if outcomes[0] == 2 else 8
    with repo._tx() as conn:
        stored = conn.execute("SELECT credentials FROM accounts WHERE alias='primary'").fetchone()
    assert repo.cipher.decrypt(stored['credentials'])['signing_state']['counter'] == winner


def test_refresh_does_not_reenable_account_disabled_after_capture(repo):
    account(repo)
    captured_version = repo.list_accounts()[0]['version']
    repo.disable_account('primary')
    fresh = {'cookie_source': 'fresh=synthetic', 'signing_state': {'counter': 9}}
    with pytest.raises(Conflict):
        repo.replace_account('primary', fresh, expected_version=captured_version,
                             preserve_proxy=True, require_enabled=True)
    with repo._tx() as conn:
        stored = conn.execute("SELECT version,status,credentials FROM accounts WHERE alias='primary'").fetchone()
    assert stored['status'] == 'disabled' and stored['version'] == captured_version
    assert repo.cipher.decrypt(stored['credentials'])['cookie_source'] == 'session=synthetic'
    # Explicit manual replacement retains its existing reactivation semantics.
    assert repo.replace_account('primary', fresh, expected_version=captured_version) == captured_version + 1


def test_concurrent_disable_wins_over_automatic_refresh(repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    account(repo)
    barrier = Barrier(2)

    def refresh():
        barrier.wait(timeout=5)
        try:
            return repo.replace_account('primary', {'cookie_source': 'fresh=synthetic',
                'signing_state': {'counter': 9}}, expected_version=1, require_enabled=True)
        except Conflict:
            return 'conflict'

    def disable():
        barrier.wait(timeout=5)
        repo.disable_account('primary')

    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh_future = executor.submit(refresh)
        disable_future = executor.submit(disable)
        assert refresh_future.result() in (2, 'conflict')
        disable_future.result()
    assert repo.list_accounts()[0]['status'] == 'disabled'
