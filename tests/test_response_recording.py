"""HTTP completion updates signing history; transport failures never do."""
from unittest.mock import Mock

import pytest

from shopee_affiliate.direct import DirectTransientError, DirectVerificationError
from shopee_service.repository import Reservation
from shopee_service.worker import process_one, verify_account


def work():
    return Reservation('123', 'primary', 1, 'token', 1, {}, 'synthetic=1', None)


@pytest.mark.parametrize('verification', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'api_error', 'timeout', 'normalization'])
def test_completion_fact_is_forwarded(verification, outcome):
    repo = Mock()
    repo.claim_job.return_value = repo.claim_verification.return_value = work()
    fetch = Mock(return_value={'product': {'item_id': '123'}})
    if outcome == 'api_error':
        fetch.side_effect = DirectVerificationError('private', http_status=200,
            api_code=90309999, response_received=True)
    elif outcome == 'timeout':
        fetch.side_effect = DirectTransientError('private')
    elif outcome == 'normalization':
        fetch.return_value = {'product': None}
    if verification:
        verify_account(repo, 'primary', '123', fetch=fetch)
    else:
        process_one(repo, fetch=fetch)
    method = ('complete_' if outcome == 'success' else 'fail_') + ('verification' if verification else 'job')
    assert getattr(repo, method).call_args.kwargs['response_received'] is (outcome != 'timeout')


def test_cli_import_records_the_captured_success_response(monkeypatch):
    from shopee_affiliate import session_signer
    from shopee_service import cli
    initial = {'synthetic': 'pending-response'}
    completed = {'synthetic': 'completed-response'}
    initialize = Mock(return_value=initial)
    record = Mock(return_value=completed)
    monkeypatch.setattr(session_signer, 'initialize_session', initialize)
    monkeypatch.setattr(session_signer, 'record_response', record)
    headers = {'synthetic': 'accepted-request'}
    assert cli._initialize(headers) is completed
    initialize.assert_called_once_with(headers)
    record.assert_called_once_with(initial)
