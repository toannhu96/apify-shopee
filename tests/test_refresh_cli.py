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


def run_proxy():
    return cli.main(['set-proxy-stdin', 'primary', '--expected-version', '2',
                     '--item-id', '24995347301'])


def setup_proxy(monkeypatch, raw=None):
    repo = setup(monkeypatch, raw=raw if raw is not None else
                 b'{"proxy_url":"https://synthetic-user:synthetic-password@proxy.invalid:443"}')
    calls = []
    def replace_proxy(alias, proxy_url, **kwargs):
        calls.append((alias, proxy_url, kwargs))
        return 3
    monkeypatch.setattr(repo, 'replace_proxy', replace_proxy, raising=False)
    return repo, calls


def test_private_proxy_handoff_uses_atomic_repository_helper_and_verifies_once(monkeypatch, capsys):
    repo, calls = setup_proxy(monkeypatch)
    verifies = []
    def verify(*args):
        verifies.append(args)
        repo.status = 'healthy'
        return True
    monkeypatch.setattr(cli, '_verify', verify)
    assert run_proxy() == 0
    assert calls == [('primary', 'https://synthetic-user:synthetic-password@proxy.invalid:443',
                      {'expected_version': 2})]
    assert not repo.calls
    assert verifies == [(repo, 'primary', '24995347301')]
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == {
        'alias': 'primary', 'version': 3, 'status': 'healthy', 'verified': True}
    assert 'synthetic-password' not in output.out


@pytest.mark.parametrize('raw', [
    b'[]', b'{}', b'{"proxy_url":"https://proxy.invalid","extra":1}',
    b'{"proxy_url":"https://proxy.invalid","proxy_url":"https://other.invalid"}',
    b'{"proxy_url":NaN}', b'\xff', b'x' * 16385,
    b'{"proxy_url":null}', b'{"proxy_url":[]}',
    b'{"proxy_url":"file:///secret"}', b'{"proxy_url":"http://"}',
    b'{"proxy_url":"https://proxy.invalid:0"}',
    b'{"proxy_url":"https://proxy.invalid:99999"}',
    b'{"proxy_url":"https://proxy.invalid/path"}',
    b'{"proxy_url":"https://proxy.invalid?secret=1"}',
    b'{"proxy_url":"https://proxy.invalid#secret"}',
    b'{"proxy_url":" https://proxy.invalid"}',
    b'{"proxy_url":"https://proxy.invalid/\\u0000"}',
    b'{"proxy_url":"https://proxy.invalid/\\u007f"}',
    b'{"proxy_url":"https://pr\\u00f6xy.invalid"}',
    b'{"proxy_url":"https://user:password%0d%0a@proxy.invalid"}',
    json.dumps({'proxy_url': 'https://' + 'u' * 4075 + ':p@proxy.invalid'}).encode(),
    json.dumps({'proxy_url': 'https://' + 'u' * 8192 + ':secret@proxy.invalid'}).encode(),
])
def test_invalid_proxy_stdin_never_mutates_verifies_or_leaks(monkeypatch, capsys, raw):
    repo, calls = setup_proxy(monkeypatch, raw)
    monkeypatch.setattr(cli, '_verify', lambda *args: pytest.fail('Must not verify'))
    assert run_proxy() == 1
    assert not calls and not repo.calls
    output = capsys.readouterr()
    assert not output.out and output.err == 'Proxy handoff is invalid or exceeds the size limit\n'


@pytest.mark.parametrize('exception', ['conflict', 'missing', 'disabled'])
def test_proxy_cas_rejection_never_verifies_or_leaks(monkeypatch, capsys, exception):
    from shopee_service.repository import Conflict, Missing
    repo, _ = setup_proxy(monkeypatch)
    def reject(*args, **kwargs):
        raise (Missing if exception == 'missing' else Conflict)('synthetic-password')
    monkeypatch.setattr(repo, 'replace_proxy', reject)
    monkeypatch.setattr(cli, '_verify', lambda *args: pytest.fail('Must not verify'))
    assert run_proxy() == 1
    output = capsys.readouterr()
    assert not output.out and 'synthetic-password' not in output.err


def test_proxy_cooldown_does_not_verify(monkeypatch, capsys):
    _repo, calls = setup_proxy(monkeypatch)
    monkeypatch.setattr(cli, '_wait_for_verification', lambda *args: False)
    monkeypatch.setattr(cli, '_verify', lambda *args: pytest.fail('Must not verify'))
    assert run_proxy() == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)['verified'] is False


def test_proxy_output_rejects_unexpected_status(monkeypatch, capsys):
    repo, _ = setup_proxy(monkeypatch)
    repo.status = 'synthetic-password'
    assert run_proxy() == 0
    assert json.loads(capsys.readouterr().out) == {
        'alias': 'primary', 'version': 3, 'status': 'unavailable', 'verified': False}


@pytest.mark.parametrize('version', ['0', '-1', '1.5'])
def test_proxy_version_required_positive(version):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(['set-proxy-stdin', 'primary', '--expected-version', version,
                                 '--item-id', '123'])


@pytest.mark.parametrize('mode', [None, 'cumulative', 'standalone-seed'])
def test_refresh_signing_mode_parser(mode):
    args = ['refresh-session-stdin', 'primary', '--expected-version', '2', '--item-id', '123']
    parsed = cli._parser().parse_args(args + ([] if mode is None else ['--signing-mode', mode]))
    assert parsed.signing_mode == (mode or 'cumulative')


@pytest.mark.parametrize('command, mode', [('refresh-session-stdin', 'unknown'),
    ('set-proxy-stdin', 'standalone-seed')])
def test_signing_mode_is_scoped_and_validated(command, mode):
    with pytest.raises(SystemExit):
        cli._parser().parse_args([command, 'primary', '--expected-version', '2',
                                 '--item-id', '123', '--signing-mode', mode])


def test_explicit_standalone_seed_initializes_dispatcher_and_preserves_cas(monkeypatch, capsys):
    from shopee_affiliate import managed_signing
    repo = setup(monkeypatch)
    calls = []
    state = {'mode': 'standalone_seed', 'synthetic_state': True}
    def initialize(headers, *, mode):
        calls.append((headers, mode))
        return state
    monkeypatch.setattr(managed_signing, 'initialize', initialize)
    monkeypatch.setattr(cli, '_initialize', lambda *args: pytest.fail('Cumulative initialization called'))
    assert cli.main(['refresh-session-stdin', 'primary', '--expected-version', '2',
                     '--item-id', '24995347301', '--signing-mode', 'standalone-seed']) == 0
    assert calls == [({key.lower(): value for key, value in seed().items()}, 'standalone-seed')]
    assert repo.calls[0][1] == {'cookie_source': 'session=private-cookie', 'signing_state': state}
    assert repo.calls[0][2] == {'require_existing': True, 'expected_version': 2,
                              'preserve_proxy': True, 'require_enabled': True}
    assert json.loads(capsys.readouterr().out) == {
        'alias': 'primary', 'version': 3, 'status': 'healthy', 'verified': True}


def test_explicit_cumulative_mode_retains_existing_initialization(monkeypatch, capsys):
    repo = setup(monkeypatch)
    assert cli.main(['refresh-session-stdin', 'primary', '--expected-version', '2',
                     '--item-id', '24995347301', '--signing-mode', 'cumulative']) == 0
    assert repo.calls[0][1]['signing_state'] == cli._initialize(seed())
    assert json.loads(capsys.readouterr().out)['verified'] is True


def test_programmatic_invalid_signing_mode_is_redacted(monkeypatch):
    repo = setup(monkeypatch)
    with pytest.raises(cli.OperatorError, match='Session handoff is invalid') as caught:
        cli._stdin_credentials(mode='secret-invalid-mode')
    assert 'secret' not in str(caught.value)
    assert not repo.calls


def test_standalone_initialization_failure_does_not_mutate_or_leak(monkeypatch, capsys):
    from shopee_affiliate import managed_signing
    repo = setup(monkeypatch)
    def initialize(*args, **kwargs):
        raise ValueError('PRIVATE-SIGNING-SECRET')
    monkeypatch.setattr(managed_signing, 'initialize', initialize)
    monkeypatch.setattr(cli, '_verify', lambda *args: pytest.fail('Must not verify'))
    assert cli.main(['refresh-session-stdin', 'primary', '--expected-version', '2',
                     '--item-id', '24995347301', '--signing-mode', 'standalone-seed']) == 1
    assert not repo.calls
    output = capsys.readouterr()
    assert not output.out
    assert output.err == 'Session handoff is invalid or exceeds the size limit\n'


def test_standalone_cli_import_preserves_original_seed_without_synthetic_response(monkeypatch):
    setup(monkeypatch)
    credentials = cli._stdin_credentials(mode='standalone-seed')
    state = credentials['signing_state']
    assert state['mode'] == 'standalone_seed'
    assert state['seed_headers'] == {key.lower(): value for key, value in seed().items()}
    assert state['sequence'] == state['response_count'] == 0
    assert state['last_headers'] is None
