"""Credential handling and alert tests use synthetic data and mocked transports."""
import base64
import json
import logging
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from shopee_service.alerts import dispatch_one
from shopee_service.config import ConfigurationError, Settings
from shopee_service.secrets import Cipher, SecretError

KEY = base64.urlsafe_b64encode(b'0' * 32).decode()


def env(**changes):
    return {'SH_DATABASE_URL': 'postgresql://user:pass@db/test',
            'SH_API_TOKEN': 'a' * 32, 'SH_SECRET_KEY': KEY, **changes}


def test_settings_file_precedence_and_redacted_repr(tmp_path):
    token = tmp_path / 'token'
    token.write_text('b' * 32 + '\n')
    config = Settings.from_env(env(SH_API_TOKEN_FILE=str(token)))
    assert config.api_token == 'b' * 32
    assert config.min_interval_seconds == 6
    assert config.lease_seconds == 90
    assert 'bbbb' not in repr(config) and 'user:pass' not in repr(config)
    with pytest.raises(FrozenInstanceError):
        config.api_token = 'other'


@pytest.mark.parametrize('changes', [
    {'SH_DATABASE_URL': ''}, {'SH_API_TOKEN': 'small'}, {'SH_SECRET_KEY': 'broken'},
    {'SH_MIN_INTERVAL_SECONDS': '5'}, {'SH_MIN_INTERVAL_SECONDS': 'NaN'},
    {'SH_LEASE_SECONDS': '59'}, {'SH_DISCORD_WEBHOOK': 'https://evil.test/path'},
    {'SH_API_TOKEN_FILE': '/does-not-exist-private'},
])
def test_settings_reject_invalid_without_values(changes):
    with pytest.raises(ConfigurationError) as caught:
        Settings.from_env(env(**changes))
    assert 'does-not-exist-private' not in str(caught.value)
    assert 'user:pass' not in str(caught.value)


def test_cipher_roundtrip_randomized_and_authenticated():
    cipher = Cipher(KEY)
    secret = {'cookies': 'synthetic-cookie', 'state': {'count': 3}}
    first, second = cipher.encrypt(secret), cipher.encrypt(secret)
    assert first != second and 'synthetic-cookie' not in first
    assert cipher.decrypt(first) == secret
    with pytest.raises(SecretError):
        cipher.decrypt(first[:-4] + 'AAAA')
    assert secret == {'cookies': 'synthetic-cookie', 'state': {'count': 3}}


@pytest.mark.parametrize('value', [[], {'value': float('nan')}, {'secret': object()}])
def test_cipher_rejects_non_object_or_non_json(value):
    with pytest.raises(SecretError):
        Cipher(KEY).encrypt(value)


class FakeRepo:
    def __init__(self, alert=True):
        self.alert = {'id': 1, 'payload': {'content': 'Account alpha requires refresh'},
                      'attempts': 0, 'lease_token': 'lease'} if alert else None
        self.finished = []

    def claim_alert(self):
        return self.alert

    def finish_alert(self, *args, **kwargs):
        self.finished.append((args, kwargs))


class FakeClient:
    def __init__(self, status=200, payload=None, headers=None, error=None):
        self.status, self.payload, self.headers, self.error = status, payload, headers or {}, error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, headers=self.headers,
                               json=lambda: self.payload or {})


WEBHOOK = 'https://discord.com/api/webhooks/123/synthetic-token'


def test_alert_success_disables_mentions_and_waits_for_delivery():
    repo, client = FakeRepo(), FakeClient()
    assert dispatch_one(repo, WEBHOOK, client=client)
    assert client.calls[0][1]['json']['allowed_mentions'] == {'parse': []}
    assert client.calls[0][1]['params'] == {'wait': 'true'}
    assert repo.finished == [((1, 'lease'), {})]
    assert 'allowed_mentions' not in repo.alert['payload']


@pytest.mark.parametrize('status,payload,headers,delay', [
    (429, {'retry_after': 12.5}, {}, 12.5),
    (429, {}, {'Retry-After': '25'}, 25),
    (429, {'retry_after': 999999}, {}, 3600),
    (500, {}, {}, 30),
])
def test_alert_retry_is_bounded(status, payload, headers, delay):
    repo = FakeRepo()
    assert dispatch_one(repo, WEBHOOK, client=FakeClient(status, payload, headers))
    assert repo.finished[0][1]['retry_after'] == delay
    assert repo.finished[0][1]['error'] == ('rate_limited' if status == 429 else 'delivery_failed')


def test_alert_failure_does_not_leak_webhook():
    repo = FakeRepo()
    dispatch_one(repo, WEBHOOK, client=FakeClient(error=RuntimeError(WEBHOOK)))
    assert WEBHOOK not in str(repo.finished)


def test_httpx_internal_request_logging_does_not_expose_webhook(caplog):
    # HTTPX logs a successful request URL even when application code never logs it.
    from shopee_service.alerts import _install_log_filter

    _install_log_filter()
    with caplog.at_level(logging.INFO, logger='httpx'):
        logging.getLogger('httpx').info('HTTP Request: POST %s "HTTP/1.1 200 OK"', WEBHOOK)
        logging.getLogger('httpx').info('Ordinary transport message')
    assert 'synthetic-token' not in caplog.text
    assert 'Ordinary transport message' not in caplog.text


def test_alert_disabled_or_empty_does_not_consume():
    repo = FakeRepo()
    assert not dispatch_one(repo, None, client=FakeClient())
    assert not dispatch_one(FakeRepo(False), WEBHOOK, client=FakeClient())
    assert repo.finished == []


class CliRepo:
    def __init__(self):
        self.calls = []
        self.accounts = []

    def migrate(self):
        self.calls.append(('migrate',))

    def replace_account(self, alias, credentials, *, create_only=False, require_existing=False):
        self.last_mode = (create_only, require_existing)
        self.calls.append(('replace', alias, credentials))
        return 2

    def disable_account(self, alias):
        self.calls.append(('disable', alias))

    def list_accounts(self):
        return self.accounts


def cli_setup(monkeypatch):
    from shopee_service import cli
    repo = CliRepo()
    monkeypatch.setattr(cli, '_repository', lambda: repo)
    return cli, repo


def test_cli_init_disable_and_list(monkeypatch, capsys):
    cli, repo = cli_setup(monkeypatch)
    assert cli.main(['init-db']) == 0
    assert cli.main(['disable-account', 'alpha']) == 0
    repo.accounts = [{'alias': 'alpha', 'status': 'disabled', 'version': 2,
                      'credentials': 'never-print-this'}]
    assert cli.main(['list-accounts']) == 0
    output = capsys.readouterr().out
    assert 'alpha' in output and 'never-print-this' not in output
    assert repo.calls == [('migrate',), ('disable', 'alpha')]


def test_cli_replacement_reads_protected_files(monkeypatch, tmp_path, capsys):
    cli, repo = cli_setup(monkeypatch)
    cookie = tmp_path / 'cookie.json'
    state = tmp_path / 'state.json'
    cookie.write_text('session=synthetic-cookie')
    state.write_text('{}')
    cookie.chmod(0o600)
    state.chmod(0o600)
    monkeypatch.setattr(cli, '_seed', lambda path: {'synthetic': 'header'})
    monkeypatch.setattr(cli, '_initialize', lambda headers: {'counter': 1})
    assert cli.main(['replace-session', 'alpha', '--cookies-file', str(cookie),
                     '--state-file', str(state)]) == 0
    assert repo.last_mode == (False, True)
    credentials = repo.calls[0][2]
    assert credentials == {'cookie_source': 'session=synthetic-cookie',
                           'signing_state': {'counter': 1}, 'proxy_url': None}
    output = capsys.readouterr().out
    assert 'synthetic-cookie' not in output and 'pending' in output.lower()


def test_cli_rejects_world_readable_cookie(monkeypatch, tmp_path, capsys):
    cli, repo = cli_setup(monkeypatch)
    cookie = tmp_path / 'cookie'
    cookie.write_text('private-secret')
    cookie.chmod(0o644)
    assert cli.main(['add-account', 'alpha', '--cookies-file', str(cookie),
                     '--state-file', str(cookie)]) == 1
    assert repo.calls == []
    assert 'private-secret' not in capsys.readouterr().err


def test_cli_failure_is_sanitized(monkeypatch, capsys):
    cli, repo = cli_setup(monkeypatch)
    monkeypatch.setattr(repo, 'migrate', lambda: (_ for _ in ()).throw(RuntimeError('db-password')))
    assert cli.main(['init-db']) == 1
    assert 'db-password' not in capsys.readouterr().err


def test_cli_verify_delegates(monkeypatch):
    cli, repo = cli_setup(monkeypatch)
    calls = []
    monkeypatch.setattr(cli, '_verify', lambda *args: calls.append(args) or True)
    assert cli.main(['verify-account', 'alpha', '--item-id', '123']) == 0
    assert calls == [(repo, 'alpha', '123')]


@pytest.mark.parametrize('raw', [b'[]', b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}'])
def test_authenticated_non_json_payload_is_rejected(raw):
    from cryptography.fernet import Fernet
    token = Fernet(KEY).encrypt(raw).decode()
    with pytest.raises(SecretError):
        Cipher(KEY).decrypt(token)


def test_cipher_finite_float_and_bad_key():
    cipher = Cipher(KEY.encode())
    assert cipher.decrypt(cipher.encrypt({'value': 1.25})) == {'value': 1.25}
    with pytest.raises(SecretError):
        Cipher('bad-key')
    with pytest.raises(SecretError):
        cipher.decrypt(None)


def test_permanent_alert_failure_is_not_retried():
    repo = FakeRepo()
    dispatch_one(repo, WEBHOOK, client=FakeClient(404))
    assert repo.finished[0][1] == {'error': 'delivery_rejected', 'retry_after': None}


@pytest.mark.parametrize('payload', [{'retry_after': 'not-numeric'}, {'retry_after': float('nan')}])
def test_bad_retry_after_uses_safe_backoff(payload):
    repo = FakeRepo()
    dispatch_one(repo, WEBHOOK, client=FakeClient(429, payload))
    assert repo.finished[0][1]['retry_after'] == 30


def test_cli_duplicate_alias_and_failed_verify(monkeypatch, capsys):
    cli, repo = cli_setup(monkeypatch)
    repo.accounts = [{'alias': 'alpha'}]
    assert cli.main(['add-account', 'alpha', '--cookies-file', 'unused', '--state-file', 'unused']) == 1
    monkeypatch.setattr(cli, '_verify', lambda *args: False)
    assert cli.main(['verify-account', 'alpha', '--item-id', '123']) == 1
    assert repo.calls == []
    assert 'already exists' in capsys.readouterr().err


def test_cli_protected_seed_and_proxy_validation(tmp_path):
    from pathlib import Path

    from shopee_service import cli

    fixture = json.loads(Path('tests/fixtures/sap_root_refresh_golden.json').read_text())
    headers = {**fixture['before_headers'], 'af-ac-enc-sz-token': 'synthetic-token',
               'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21'}
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'headers': headers}))
    seed.chmod(0o600)
    assert cli._seed(str(seed)) == headers
    seed.write_text('{"headers":{},"headers":{}}')
    with pytest.raises(cli.OperatorError, match='seed'):
        cli._seed(str(seed))
    proxy = tmp_path / 'proxy'
    proxy.write_text('https://user:synthetic@proxy.example:8080\n')
    proxy.chmod(0o600)
    assert cli._proxy(str(proxy)) == 'https://user:synthetic@proxy.example:8080'
    proxy.write_text('https://proxy.example/?secret=foo')
    with pytest.raises(cli.OperatorError, match='Proxy'):
        cli._proxy(str(proxy))


def test_cli_private_file_limits_missing_and_symlink(tmp_path):
    from shopee_service import cli

    path = tmp_path / 'secret'
    with pytest.raises(cli.OperatorError):
        cli._read_private(str(path), 10)
    path.write_text('12345678901')
    path.chmod(0o600)
    with pytest.raises(cli.OperatorError, match='size limit'):
        cli._read_private(str(path), 10)
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(cli.OperatorError):
        cli._read_private(str(link), 100)


@pytest.mark.parametrize('arguments', [
    ['disable-account', 'contains spaces'], ['verify-account', 'alpha', '--item-id', '01'],
])
def test_cli_rejects_invalid_alias_and_product(arguments):
    from shopee_service import cli

    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize('changes', [{'SH_DATABASE_URL': 'https://db.example'}, {'SH_API_TOKEN': 'a\nb' * 32}])
def test_settings_rejects_invalid_scheme_and_control_characters(changes):
    with pytest.raises(ConfigurationError):
        Settings.from_env(env(**changes))


@pytest.mark.parametrize('level', [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize('outcome', ['success', 'server_error', 'transport_error'])
def test_real_httpx_webhook_transport_never_logs_credentials(caplog, level, outcome):
    import httpx

    # Construct lower-level loggers before dispatch just as HTTPX's transport does.
    core_logger = logging.getLogger('httpcore.http11')
    hpack_logger = logging.getLogger('hpack.hpack')
    token = WEBHOOK.rsplit('/', 1)[1]

    def transport(request):
        # MockTransport bypasses HTTPCore; emit its representative path/exception
        # records to cover the DEBUG messages a real connection can produce.
        core_logger.log(level, 'send_request_headers.started target=%r', request.url.raw_path)
        hpack_logger.log(level, 'Encoding %s', token)
        if outcome == 'transport_error':
            raise httpx.ConnectError('Failed request ' + str(request.url), request=request)
        return httpx.Response(200 if outcome == 'success' else 503, json={})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client, caplog.at_level(level):
        assert dispatch_one(FakeRepo(), WEBHOOK, client=client)
        logging.getLogger('shopee_service.tests').log(level, 'safe application telemetry')
    assert 'safe application telemetry' in caplog.text
    assert token not in caplog.text and WEBHOOK not in caplog.text
