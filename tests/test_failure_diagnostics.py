"""Failure diagnostics expose bounded facts, never supplied exception contents."""
import json
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from shopee_affiliate import direct, http_transport, signed_client
from shopee_service import repository, worker


def test_response_metadata_distinguishes_http_from_api_rejection(monkeypatch):
    for status, payload, code in [(403, {}, None), (200, {'code': 90309999}, 90309999)]:
        monkeypatch.setattr(http_transport, 'request_product', Mock(return_value=
            http_transport.HttpResponse(status, json.dumps(payload).encode())))
        with pytest.raises(direct.DirectVerificationError) as caught:
            signed_client._fetch_validated('123', {}, 'synthetic=1')
        assert caught.value.http_status == status
        assert caught.value.response_received is True
        assert caught.value.api_code == code


@pytest.mark.parametrize('value', ['secret', True, 10**100, -10**100, None])
def test_error_metadata_is_numeric_and_bounded(value):
    error = direct.DirectError('secret', http_status=value, api_code=value)
    assert error.http_status is None and error.api_code is None


def test_worker_logs_api_failure_without_exception_contents(caplog):
    repo = Mock()
    work = repository.Reservation('123', 'primary', 1, 'private-token', 1, {}, 'secret-cookie', None)
    repo.claim_job.return_value = work
    error = direct.DirectVerificationError('SENSITIVE_SENTINEL', http_status=200, api_code=90309999)
    worker.process_one(repo, fetch=Mock(side_effect=error))
    assert 'phase=job_fetch category=verification http_status=200 api_code=90309999' in caplog.text
    assert 'SENSITIVE_SENTINEL' not in caplog.text and 'secret-cookie' not in caplog.text
    repo.fail_job.assert_called_once_with(work, 'verification', response_received=False)


def test_diagnostic_logger_defends_against_mutated_metadata(caplog):
    error = direct.DirectError('SENSITIVE_SENTINEL')
    error.category = 'SENSITIVE_SENTINEL'
    error.http_status = 'SENSITIVE_SENTINEL'
    error.api_code = {'secret': 'SENSITIVE_SENTINEL'}
    worker._log_failure('job_fetch', error)
    assert 'category=invalid_response http_status=None api_code=None' in caplog.text
    assert 'SENSITIVE_SENTINEL' not in caplog.text


def test_verification_logs_normalization_failure_separately(caplog):
    repo = Mock()
    repo.claim_verification.return_value = Mock()
    worker.verify_account(repo, 'primary', '123', fetch=Mock(return_value={'product': None}))
    assert 'phase=verification_result category=invalid_response' in caplog.text


def test_job_reservation_failure_is_identified_without_network(caplog, monkeypatch):
    repo = repository.Repository('unused', Mock())
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [{'alias': 'primary'}, {'item_id': '123', 'attempts': 0}]
    @contextmanager
    def transaction():
        yield conn
    monkeypatch.setattr(repo, '_tx', transaction)
    monkeypatch.setattr(repo, '_maintenance', Mock())
    monkeypatch.setattr(repo, '_unavailable', Mock())
    monkeypatch.setattr(repo, '_reserve', Mock(side_effect=ValueError('SENSITIVE_SENTINEL')))
    monkeypatch.setattr(repo, '_incident', Mock())
    assert repo.claim_job(reserve=Mock()) is None
    assert 'phase=job_reservation category=signing' in caplog.text
    assert 'SENSITIVE_SENTINEL' not in caplog.text


def test_verification_reservation_logs_then_preserves_error(caplog, monkeypatch):
    from datetime import UTC, datetime, timedelta
    repo = repository.Repository('unused', Mock())
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {
        'lease_token': None, 'next_request_at': datetime.now(UTC) - timedelta(seconds=1),
        'status': 'pending',
    }
    @contextmanager
    def transaction():
        yield conn
    monkeypatch.setattr(repo, '_tx', transaction)
    monkeypatch.setattr(repo, '_maintenance', Mock())
    monkeypatch.setattr(repo, '_reserve', Mock(side_effect=ValueError('SENSITIVE_SENTINEL')))
    with pytest.raises(ValueError):
        repo.claim_verification('primary', '123', reserve=Mock())
    assert 'phase=verification_reservation category=signing' in caplog.text
    assert 'SENSITIVE_SENTINEL' not in caplog.text


@pytest.mark.parametrize('code', [{'private': 'SENSITIVE_SENTINEL'}, 'SENSITIVE_SENTINEL', True, 10**100])
def test_untrusted_api_code_is_not_attached(monkeypatch, code):
    monkeypatch.setattr(http_transport, 'request_product', Mock(return_value=
        http_transport.HttpResponse(200, json.dumps({'code': code}).encode())))
    with pytest.raises(direct.DirectError) as caught:
        signed_client._fetch_validated('123', {}, 'synthetic=1')
    assert caught.value.http_status == 200
    assert caught.value.api_code is None
    assert 'SENSITIVE_SENTINEL' not in str(caught.value)
