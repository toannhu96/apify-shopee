import json
from secrets import token_urlsafe

import httpx
import pytest

from shopee_service import browser_refresh
from shopee_service.browser_capture import Capture
from shopee_service.browser_refresh import HttpRemote, RefreshError, refresh_once

TOKEN = token_urlsafe(32)


def test_standalone_mode_reaches_api():
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'alias': 'primary', 'version': 1, 'status': 'authentication'})
        assert json.loads(request.content)['signing_mode'] == 'standalone-seed'
        return httpx.Response(200, json={'alias': 'primary', 'version': 2, 'status': 'healthy', 'verified': True})
    assert refresh_once(remote(handler), 'primary', '123', 'http://localhost:9222',
                        signing_mode='standalone-seed', capture=lambda *args: Capture('private', {}))['verified']


def test_main_watch_forwards_signing_mode(monkeypatch):
    monkeypatch.setenv('SH_SESSION_REFRESH_TOKEN', TOKEN)
    calls = []
    monkeypatch.setattr(browser_refresh, 'refresh_once', lambda *args, **kwargs: calls.append(kwargs))
    def watch(*args, refresh, **kwargs):
        refresh(*args)
    monkeypatch.setattr(browser_refresh, 'run_watch', watch)
    assert browser_refresh.main(['primary', '--api-url', 'https://service.example',
                                 '--item-id', '123', '--cdp-url', 'http://localhost:9222',
                                 '--signing-mode', 'standalone-seed', '--watch']) == 0
    assert calls == [{'signing_mode': 'standalone-seed'}]


def remote(handler, **kwargs):
    return HttpRemote('https://service.example', TOKEN, transport=httpx.MockTransport(handler), **kwargs)


def test_http_refresh_fences_then_posts_once():
    events = []
    def handler(request):
        events.append(request.method)
        assert request.headers['Authorization'] == f'Bearer {TOKEN}'
        assert request.url.host == 'service.example'
        if request.method == 'GET':
            assert request.url.path == '/v1/admin/accounts/primary'
            return httpx.Response(200, json={'alias': 'primary', 'version': 7, 'status': 'authentication'})
        assert request.url.path == '/v1/admin/accounts/primary/session'
        assert json.loads(request.content) == {
            'expected_version': 7, 'item_id': '123', 'cookie_source': 'private-cookie',
            'headers': {'private-header': 'private-value'},
        }
        return httpx.Response(200, json={'alias': 'primary', 'version': 8, 'status': 'healthy',
                                        'verified': True, 'secret': 'do-not-print'})
    def capture(*args):
        events.append('capture')
        return Capture('private-cookie', {'private-header': 'private-value'})
    result = refresh_once(remote(handler), 'primary', '123', 'http://localhost:9222', capture=capture)
    assert events == ['GET', 'capture', 'POST']
    assert result == {'alias': 'primary', 'version': 8, 'status': 'healthy', 'verified': True}


@pytest.mark.parametrize('url', [
    'http://service.example', 'https://user:secret@service.example', 'https://service.example?secret=x',
    'https://service.example#x', 'https://service.example/path', 'https://service.example:bad',
    'https://service.example/\n', 'https://service.example\\@evil.example', '',
])
def test_unsafe_url_rejected_without_network(url):
    with pytest.raises(RefreshError, match='HTTPS'):
        HttpRemote(url, TOKEN)


@pytest.mark.parametrize('token', ['', 'short', 'x' * 32 + '\n', 'é' * 40, 'x' * 17000])
def test_invalid_token_is_redacted(token):
    with pytest.raises(RefreshError, match='SH_SESSION_REFRESH_TOKEN'):
        HttpRemote('https://service.example', token)


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 404, 409, 429, 500])
def test_failures_never_follow_redirect_or_retry_or_echo_response(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://evil.example'}, text='secret')
    with pytest.raises(RefreshError) as error:
        remote(handler).call(['refresh-session-stdin', 'primary', '--expected-version', '1', '--item-id', '123'],
                             {'cookie_source': 'private', 'headers': {}})
    assert len(calls) == 1
    assert 'secret' not in str(error.value)
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('body', [b'not json secret', b'x' * 65537, b'[]', b'null'])
def test_invalid_response_is_bounded_and_redacted(body):
    with pytest.raises(RefreshError):
        remote(lambda _: httpx.Response(200, content=body)).account('primary')


@pytest.mark.parametrize('row', [{}, {'alias': 'other', 'version': 1, 'status': 'healthy'},
                                {'alias': 'primary', 'version': True, 'status': 'healthy'},
                                {'alias': 'primary', 'version': 0, 'status': 'healthy'},
                                {'alias': 'primary', 'version': 1, 'status': 'secret'}])
def test_invalid_account_response(row):
    with pytest.raises(RefreshError):
        remote(lambda _: httpx.Response(200, json=row)).account('primary')


def test_exception_and_oversized_payload_safe():
    def handler(_):
        raise httpx.ReadTimeout('private-token secret')
    with pytest.raises(RefreshError) as error:
        remote(handler).account('primary')
    assert 'private-token' not in str(error.value)
    with pytest.raises(RefreshError, match='transfer limit'):
        remote(lambda _: pytest.fail('network')).call(
            ['refresh-session-stdin', 'primary', '--expected-version', '1', '--item-id', '1'],
            {'cookie_source': 'x' * 1_179_649, 'headers': {}})


def test_client_disables_environment_proxy_and_redirects(monkeypatch):
    real_client = httpx.Client
    options = []
    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)
    monkeypatch.setattr(httpx, 'Client', client)
    remote(lambda _: httpx.Response(200, json={'alias': 'primary', 'version': 1, 'status': 'healthy'})).account('primary')
    assert options[0]['trust_env'] is False
    assert options[0]['follow_redirects'] is False


def test_main_http_destination(monkeypatch, capsys):
    monkeypatch.setenv('SH_SESSION_REFRESH_TOKEN', TOKEN)
    calls = []
    monkeypatch.setattr(browser_refresh, 'refresh_once', lambda target, *args:
                        calls.append(type(target).__name__) or {'verified': True})
    args = ['primary', '--api-url', 'https://service.example', '--item-id', '123',
            '--cdp-url', 'http://localhost:9222']
    assert browser_refresh.main(args) == 0
    assert calls == ['HttpRemote']
    assert json.loads(capsys.readouterr().out) == {'verified': True}
    assert browser_refresh.main([*args, '--container', 'api']) == 1
    monkeypatch.delenv('SH_SESSION_REFRESH_TOKEN')
    assert browser_refresh.main(args) == 1
    assert TOKEN not in capsys.readouterr().out
