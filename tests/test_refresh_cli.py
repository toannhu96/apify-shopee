"""Private stdin handoff: bounded secrets, compare-and-swap, verification gate."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shopee_service import cli


def seed():
    value = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    return {**value['before_headers'], 'af-ac-enc-sz-token': 'synthetic-token',
            'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21'}


class Repo:
    def __init__(self):
        self.calls = []
        self.version, self.status = 3, 'pending'

    def replace_account(self, alias, credentials, **kwargs):
        self.calls.append((alias, credentials, kwargs))
        return 3

    def list_accounts(self):
        return [{'alias': 'primary', 'version': self.version, 'status': self.status,
                 'credentials': 'DO-NOT-PRINT'}]


def setup(monkeypatch, raw=None, verified=True):
    from shopee_affiliate import session_signer
    monkeypatch.setattr(session_signer.time, 'time_ns', lambda: 1800000000000000000)
    repo = Repo()
    if raw is None:
        raw = json.dumps({'cookie_source': 'session=private-cookie', 'headers': seed()}).encode()
    monkeypatch.setattr(cli.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(cli, '_repository', lambda: repo)
    def verify(*args):
        assert args == (repo, 'primary', '24995347301')
        repo.status = 'healthy' if verified else 'pending'
        return verified
    monkeypatch.setattr(cli, '_verify', verify)
    return repo


def run():
    return cli.main(['refresh-session-stdin', 'primary', '--expected-version', '2',
                     '--item-id', '24995347301'])


def test_private_handoff_initializes_and_verifies(monkeypatch, capsys):
    repo = setup(monkeypatch)
    assert run() == 0
    alias, credentials, kwargs = repo.calls[0]
    assert alias == 'primary'
    assert credentials['cookie_source'] == 'session=private-cookie'
    assert credentials['signing_state'] == cli._initialize(seed())
    assert kwargs == {'require_existing': True, 'expected_version': 2, 'preserve_proxy': True, 'require_enabled': True}
    assert json.loads(capsys.readouterr().out) == {
        'alias': 'primary', 'version': 3, 'status': 'healthy', 'verified': True}


@pytest.mark.parametrize('raw', [b'[]', b'{}', b'{"cookie_source":"secret","cookie_source":"other"}',
    b'{"headers":NaN}', b'\xff', b'x' * (2 * 1024 * 1024),
    b'{"cookie_source": [], "headers":{}}'], ids=['list', 'empty', 'duplicate', 'nan', 'encoding', 'oversize', 'shape'])
def test_invalid_input_never_mutates_or_leaks(monkeypatch, capsys, raw):
    repo = setup(monkeypatch, raw=raw)
    assert run() == 1
    assert not repo.calls
    output = capsys.readouterr()
    assert not output.out and 'secret' not in output.err


def test_pending_verification_reports_safe_state(monkeypatch, capsys):
    setup(monkeypatch, verified=False)
    assert run() == 0
    assert json.loads(capsys.readouterr().out)['verified'] is False


def test_replacement_conflict_never_verifies(monkeypatch, capsys):
    from shopee_service.repository import Conflict
    repo = setup(monkeypatch)
    def conflict(*args, **kwargs):
        raise Conflict('credential-secret')
    monkeypatch.setattr(repo, 'replace_account', conflict)
    monkeypatch.setattr(cli, '_verify', lambda *args: pytest.fail('Must not verify'))
    assert run() == 1
    assert 'credential-secret' not in capsys.readouterr().err


def test_concurrent_replacement_cannot_report_verified(monkeypatch, capsys):
    repo = setup(monkeypatch)
    repo.version = 4
    assert run() == 0
    assert json.loads(capsys.readouterr().out)['verified'] is False


@pytest.mark.parametrize('version', ['0', '-1', '1.5', 'secret'])
def test_version_required_positive(version):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(['refresh-session-stdin', 'primary', '--expected-version', version,
                                 '--item-id', '123'])


def test_waits_for_existing_pacing_without_dispatch(monkeypatch):
    from datetime import UTC, datetime, timedelta
    repo = Repo()
    rows = iter([
        [{'alias': 'primary', 'version': 3, 'status': 'pending',
          'next_request_at': datetime.now(UTC) + timedelta(seconds=5)}],
        [{'alias': 'primary', 'version': 3, 'status': 'pending'}],
    ])
    monkeypatch.setattr(repo, 'list_accounts', lambda: next(rows))
    waits = []
    monkeypatch.setattr(cli.time, 'sleep', waits.append)
    assert cli._wait_for_verification(repo, 'primary', 3)
    assert waits == [1]


def test_long_cooldown_stays_pending(monkeypatch):
    from datetime import UTC, datetime, timedelta
    repo = Repo()
    monkeypatch.setattr(repo, 'list_accounts', lambda: [
        {'alias': 'primary', 'version': 3, 'status': 'pending',
         'next_request_at': datetime.now(UTC) + timedelta(hours=1)}])
    assert not cli._wait_for_verification(repo, 'primary', 3)
