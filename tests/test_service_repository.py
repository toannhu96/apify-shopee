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
    timestamp = repo.list_accounts()[0]['updated_at'].isoformat()
    assert 'Time: ' + timestamp in alert['payload']['content']
