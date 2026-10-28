from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from shopee_affiliate.direct import DirectAuthenticationError, DirectTransientError
from shopee_service.repository import Reservation
from shopee_service.worker import process_one, run_loop, verify_account


def work():
    return Reservation('123', 'primary', 1, 'token', 1, {'private': 'secret'}, 'cookie=secret', None)


def test_success_is_allowlisted_and_persisted():
    repo = Mock()
    repo.claim_job.return_value = work()
    fetch = Mock(return_value={'product':{'item_id':'123','name':'Test','affiliate_link':'private'},'raw':{'secret':1}})
    assert process_one(repo, fetch=fetch)
    saved = repo.complete_job.call_args.args[1]
    assert saved['item_id'] == '123' and saved['cacheHit'] is False
    assert 'affiliate_link' not in saved and 'raw' not in saved
    datetime.fromisoformat(saved['fetchedAt'])


def test_no_work_and_typed_failures_do_not_leak():
    repo = Mock()
    repo.claim_job.return_value = None
    assert process_one(repo, fetch=Mock()) is False
    repo.claim_job.return_value = work()
    error = DirectTransientError('never log this secret')
    assert process_one(repo, fetch=Mock(side_effect=error))
    repo.fail_job.assert_called_once_with(work(), 'transient')


def test_invalid_output_and_unexpected_exception_fail_safely():
    repo = Mock()
    repo.claim_job.return_value = work()
    assert process_one(repo, fetch=Mock(return_value={'product':{'item_id': False}}))
    assert repo.fail_job.call_args.args[1] == 'invalid_response'
    assert process_one(repo, fetch=Mock(side_effect=RuntimeError('secret')))
    assert repo.fail_job.call_args.args[1] == 'invalid_response'


def test_verify_never_stores_product():
    repo = Mock()
    repo.claim_verification.return_value = work()
    repo.complete_verification.return_value = True
    assert verify_account(repo, 'primary', '123', fetch=Mock(return_value={'product':{'item_id':'123'}}))
    repo.complete_job.assert_not_called()
    repo.claim_verification.return_value = None
    assert verify_account(repo,'primary','123',fetch=Mock()) is False
    repo.claim_verification.return_value = work()
    assert verify_account(repo,'primary','123',fetch=Mock(side_effect=DirectAuthenticationError('secret'))) is False
    repo.fail_verification.assert_called_with(work(),'authentication')


def test_loop_cleanup_and_graceful_stop():
    stop = Mock()
    stop.is_set.side_effect = [False, True]
    repo = Mock()
    repo.claim_job.return_value = None
    settings = SimpleNamespace(discord_webhook=None)
    run_loop(repo,settings,stop)
    repo.cleanup.assert_called_once()
    stop.wait.assert_called_once_with(1)


def test_verify_invalid_result_releases_with_safe_category():
    repo = Mock()
    repo.claim_verification.return_value = work()
    assert verify_account(repo,'primary','123',fetch=Mock(side_effect=RuntimeError('private'))) is False
    repo.fail_verification.assert_called_with(work(),'invalid_response')


def test_loop_dispatches_alert_and_recovers_from_db_outage(monkeypatch):
    from shopee_service import worker
    stop = Mock()
    stop.is_set.side_effect = [False, False, True]
    repo = Mock()
    repo.claim_job.side_effect = [RuntimeError('private'), None]
    sender = Mock()
    monkeypatch.setattr(worker,'dispatch_one',sender)
    settings = SimpleNamespace(discord_webhook='secret')
    run_loop(repo,settings,stop)
    stop.wait.assert_any_call(5)
    sender.assert_called_once_with(repo,'secret')


def test_main_validates_then_installs_stop_handlers(monkeypatch):
    from shopee_service import worker
    settings = SimpleNamespace(database_url='private',secret_key='private',min_interval_seconds=6,lease_seconds=90)
    monkeypatch.setattr(worker.Settings,'from_env',Mock(return_value=settings))
    monkeypatch.setattr(worker,'Cipher',Mock())
    repo = Mock()
    repo.health.return_value = True
    monkeypatch.setattr(worker,'Repository',Mock(return_value=repo))
    signals = Mock()
    monkeypatch.setattr(worker.signal,'signal',signals)
    loop = Mock()
    monkeypatch.setattr(worker,'run_loop',loop)
    assert worker.main() == 0
    assert signals.call_count == 2
    stop = loop.call_args.args[2]
    assert not stop.is_set()
    signals.call_args.args[1](None,None)
    assert stop.is_set()
    repo.health.return_value = False
    assert worker.main() == 1


def test_worker_does_not_dispatch_stale_reservation():
    repo = Mock()
    repo.claim_job.return_value = work()
    repo.claim_verification.return_value = work()
    repo.reservation_current.return_value = False
    fetch = Mock()
    assert process_one(repo,fetch=fetch)
    assert verify_account(repo,'primary','123',fetch=fetch) is False
    fetch.assert_not_called()
