import json
from types import SimpleNamespace

import pytest

from shopee_service.browser_capture import Capture, matches_response, validate_cdp_url
from shopee_service.browser_refresh import RefreshError, Remote, refresh_once, run_watch


@pytest.mark.parametrize('url', ['https://127.0.0.1:9222', 'http://evil.test:9222', 'http://user@localhost:9222', 'http://localhost:9222/x', 'http://localhost:9222/?x=1'])
def test_cdp_rejects_remote_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        validate_cdp_url(url)


def test_cdp_loopback():
    assert validate_cdp_url('http://127.0.0.1:9222') == 'http://127.0.0.1:9222'


@pytest.mark.parametrize('target', ['-oProxyCommand=x', 'host;cat', 'user@host\n', 'host space', 'user@host:22'])
def test_ssh_target_validation(target):
    with pytest.raises(ValueError):
        Remote(target)


def test_ssh_stdin_only_and_safe_failure():
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout=b'secret', stderr=b'secret')
    remote = Remote('user@host', container='worker', runner=runner)
    with pytest.raises(RefreshError, match='service command failed') as error:
        remote.call(['refresh-session-stdin', 'account'], {'cookie_source': 'secret'})
    assert 'secret' not in str(error.value)
    assert 'secret' not in str(calls[0][0])
    assert calls[0][1]['input'] == b'{"cookie_source":"secret"}'
    assert 'StrictHostKeyChecking=yes' in calls[0][0]
    assert calls[0][1].get('shell', False) is False


def test_refresh_fences_capture_and_rejects_failed_verification():
    events = []
    class Fake:
        def account(self, alias):
            events.append('version')
            return {'alias': alias, 'version': 4, 'status': 'verification'}
        def call(self, args, payload):
            events.append(args)
            assert payload['headers']['af-ac-enc-sz-token'] == 'seed-token'
            return {'alias': 'account', 'version': 5, 'status': 'verification', 'verified': False}
    def capture(url, item):
        events.append('capture')
        return Capture('cookie=secret', {'af-ac-enc-sz-token': 'seed-token'})
    with pytest.raises(RefreshError, match='verification'):
        refresh_once(Fake(), 'account', '1', 'http://localhost:9222', capture=capture)
    assert events[:2] == ['version', 'capture']
    assert events[2] == ['refresh-session-stdin', 'account', '--expected-version', '4', '--item-id', '1']


def test_capture_repr_redacts():
    assert 'secret' not in repr(Capture('secret', {'token': 'secret'}))


def test_response_matching_exact_origin_item_method():
    def response(url, method='GET'):
        return SimpleNamespace(url=url, request=SimpleNamespace(method=method))
    url = 'https://affiliate.shopee.vn/api/v3/offer/product?item_id=1'
    assert matches_response(response(url), '1')
    assert not matches_response(response(url.replace('.vn/', '.vn.evil/')), '1')
    assert not matches_response(response(url), '2')
    assert not matches_response(response(url, 'POST'), '1')
    assert not matches_response(response(url + '&item_id=1'), '1')


def test_successful_refresh_redacts_extra_remote_fields():
    class Fake:
        def account(self, alias):
            return {'version': 4, 'status': 'verification'}
        def call(self, args, payload):
            return {'alias': 'account', 'version': 5, 'status': 'healthy', 'verified': True, 'secret': 'secret'}
    result = refresh_once(Fake(), 'account', '1', 'http://localhost:9222',
                          capture=lambda *args: Capture('secret', {}))
    assert result == {'alias': 'account', 'version': 5, 'status': 'healthy', 'verified': True}


def test_watch_one_replacement_before_healthy_poll():
    calls = []
    remote = SimpleNamespace(account=lambda alias: {'status': 'authentication'})
    def refresh(*args):
        calls.append('refresh')
        return {'status': 'healthy'}
    with pytest.raises(RefreshError, match='healthy poll'):
        run_watch(remote, 'account', '1', 'http://localhost:9222', refresh=refresh,
                  sleep=lambda _: None, report=lambda _: None)
    assert calls == ['refresh']


def test_watch_waits_on_rate_limit_and_observes_cooldown():
    statuses = iter(['rate_limit', 'verification', 'healthy', 'verification'])
    calls = []
    remote = SimpleNamespace(account=lambda alias: {'status': next(statuses)})
    with pytest.raises(StopIteration):
        run_watch(remote, 'account', '1', 'http://localhost:9222',
                  refresh=lambda *args: calls.append('refresh') or {'status': 'healthy'},
                  sleep=lambda _: None, clock=lambda: 100, report=lambda _: None)
    assert calls == ['refresh']


def test_conflict_aborts_no_recapture():
    captured = []
    class Fake:
        def account(self, alias):
            return {'version': 4, 'status': 'verification'}
        def call(self, *args):
            raise RefreshError('SSH remote command failed; check service status and account version')
    with pytest.raises(RefreshError):
        refresh_once(Fake(), 'account', '1', 'http://localhost:9222',
                     capture=lambda *args: captured.append('one') or Capture('secret', {}))
    assert captured == ['one']


@pytest.mark.parametrize('rows', [{}, [], [{'alias': 'account', 'version': True}],
                                   [{'alias': 'account', 'version': 0}]])
def test_invalid_account_response(rows):
    remote = Remote('host', runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(rows).encode()))
    with pytest.raises(RefreshError):
        remote.account('account')


def test_valid_account_response():
    row = {'alias': 'account', 'version': 1, 'status': 'healthy'}
    remote = Remote('host', runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([row]).encode()))
    assert remote.account('account') == row


@pytest.mark.parametrize('output', [b'invalid', b'x' * 65537])
def test_bad_remote_json_is_safe(output):
    remote = Remote('host', runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output))
    with pytest.raises(RefreshError):
        remote.call(['list-accounts'])


def test_disabled_account_never_captures():
    remote = SimpleNamespace(account=lambda alias: {'version': 1, 'status': 'disabled'})
    with pytest.raises(RefreshError, match='disabled'):
        refresh_once(remote, 'account', '1', 'http://localhost:9222', capture=lambda *args: pytest.fail('captured'))
    with pytest.raises(RefreshError, match='disabled'):
        run_watch(remote, 'account', '1', 'http://localhost:9222')


def test_watch_bounds():
    with pytest.raises(RefreshError):
        run_watch(None, 'account', '1', 'http://localhost:9222', poll_seconds=1)


def test_main_success_and_redacted_errors(monkeypatch, capsys):
    from shopee_service import browser_refresh as module
    arguments = ['account', '--ssh-target', 'host', '--item-id', '1', '--cdp-url', 'http://localhost:9222']
    monkeypatch.setattr(module, 'refresh_once', lambda *args: {'status': 'healthy'})
    assert module.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'healthy'}
    assert module.main(['invalid!', *arguments[1:]]) == 1
    assert 'Invalid account alias' in capsys.readouterr().out
    monkeypatch.setattr(module, 'run_watch', lambda *args, **kwargs: None)
    assert module.main([*arguments, '--watch']) == 0


def test_local_container_handoff_uses_stdin_without_ssh():
    from shopee_service.browser_refresh import Local
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"verified":true}')
    result = Local('shopee-api', runner=runner).call(['refresh-session-stdin', 'primary'],
                                                   {'cookie_source': 'private-cookie'})
    assert result == {'verified': True}
    argv, options = calls[0]
    assert argv == ['docker', 'exec', '-i', 'shopee-api', 'python', '-m', 'shopee_service.cli',
                    'refresh-session-stdin', 'primary']
    assert 'private-cookie' not in str(argv)
    assert b'private-cookie' in options['input']


def test_local_container_validation():
    from shopee_service.browser_refresh import Local
    with pytest.raises(ValueError):
        Local('-bad')


def test_main_local_destination(monkeypatch, capsys):
    from shopee_service import browser_refresh as module
    calls = []
    monkeypatch.setattr(module, 'refresh_once', lambda remote, *args:
                        calls.append(type(remote).__name__) or {'verified': True})
    assert module.main(['primary', '--local-container', 'shopee-api', '--item-id', '1',
                        '--cdp-url', 'http://localhost:9222']) == 0
    assert calls == ['Local']
    assert json.loads(capsys.readouterr().out)['verified'] is True
