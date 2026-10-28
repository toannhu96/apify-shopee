"""Managed client contracts: typed failures and explicit proxy routing."""
import json
from types import SimpleNamespace

import pytest

from shopee_affiliate import direct, http_transport, signed_client

HEADERS = {'x-sap-ri': 'synthetic-ri', 'x-sap-sec': 'synthetic-sec',
           'af-ac-enc-sz-token': 'synthetic-token', 'user-agent': 'synthetic-UA',
           'x-sz-sdk-version': '1.12.21'}
PAYLOAD = {'code': 0, 'data': {'item_id': '123',
           'batch_item_for_item_card_full': {'itemid': '123', 'name': 'Product'}}}


def wire(monkeypatch, status=200, payload=PAYLOAD):
    calls = []
    def request(target, **kwargs):
        calls.append((target, kwargs))
        return http_transport.HttpResponse(status, json.dumps(payload).encode())
    monkeypatch.setattr(http_transport, 'request_product', request)
    return calls


def test_reserved_headers_are_sent_once_without_signing(monkeypatch):
    monkeypatch.setattr(signed_client, '_default_signer', lambda: pytest.fail('signed twice'))
    calls = wire(monkeypatch)
    result = signed_client.fetch_signed_product('123', headers=HEADERS,
        cookie_source='session=synthetic', proxy_url='http://user:pass@proxy.test:8080')
    assert result['product']['item_id'] == '123'
    assert calls == [('123', {'headers': HEADERS, 'cookie': 'session=synthetic',
                            'proxy_url': 'http://user:pass@proxy.test:8080'})]


@pytest.mark.parametrize('status,category,blocked', [
    (401, 'authentication', True), (403, 'verification', True), (429, 'rate_limit', True),
    (302, 'verification', True), (404, 'unavailable', False), (410, 'unavailable', False),
    (408, 'transient', False), (500, 'transient', False), (503, 'transient', False),
    (204, 'invalid_response', False),
])
def test_http_categories(monkeypatch, status, category, blocked):
    calls = wire(monkeypatch, status=status)
    with pytest.raises(direct.DirectError) as caught:
        signed_client.fetch_signed_product('123', headers=HEADERS, cookie_source='s=synthetic')
    assert caught.value.category == category
    assert isinstance(caught.value, direct.DirectBlockedError) is blocked
    assert len(calls) == 1


@pytest.mark.parametrize('payload,category', [
    ({'is_login': False}, 'authentication'), ({'redirect_to_error_page': True}, 'verification'),
    ({'error': 90309999}, 'verification'), ({'code': 7}, 'invalid_response'),
    ({'code': 0, 'data': {'item_id': 'other'}}, 'invalid_response'),
])
def test_payload_categories(monkeypatch, payload, category):
    wire(monkeypatch, payload=payload)
    with pytest.raises(direct.DirectError) as caught:
        signed_client.fetch_signed_product('123', headers=HEADERS, cookie_source='s=synthetic')
    assert caught.value.category == category


def test_invalid_cookie_header_and_transport_categories(monkeypatch):
    wire(monkeypatch)
    with pytest.raises(direct.DirectError) as caught:
        signed_client.fetch_signed_product('123', headers=HEADERS, cookie_source='')
    assert caught.value.category == 'input'
    with pytest.raises(direct.DirectError) as caught:
        signed_client.fetch_signed_product('123', headers={}, cookie_source='s=synthetic')
    assert caught.value.category == 'input'
    def broken(*args, **kwargs):
        raise http_transport.TransportError('Direct HTTPS request failed')
    monkeypatch.setattr(http_transport, 'request_product', broken)
    with pytest.raises(direct.DirectError) as caught:
        signed_client.fetch_signed_product('123', headers=HEADERS, cookie_source='s=synthetic')
    assert caught.value.category == 'transient'


def test_signing_category_and_secret_redaction(monkeypatch):
    def broken(*args):
        raise ValueError('private-signature-state')
    with pytest.raises(direct.DirectError) as caught:
        signed_client.fetch_product('123', seed_headers=HEADERS, cookie_source='s=synthetic', signer=broken)
    assert caught.value.category == 'signing'
    assert 'private' not in str(caught.value)


def test_expired_cookie_session_requires_refresh_without_network(monkeypatch):
    calls = wire(monkeypatch)
    source = json.dumps([{'name': 'session', 'value': 'synthetic',
                          'domain': '.shopee.vn', 'expires': 1}])
    with pytest.raises(direct.DirectBlockedError) as caught:
        signed_client.fetch_signed_product('123', headers=HEADERS, cookie_source=source)
    assert caught.value.category == 'authentication'
    assert not calls


@pytest.mark.parametrize('proxy', ['', 'file:///etc/passwd', 'http://', 'http://proxy.test:99999',
    'http://proxy.test/a', 'http://proxy.test?token=secret', 'http://proxy.test#secret',
    'http://secret\n@proxy.test', 'http://user:%0Asecret@proxy.test', 42, 'http://proxy test'])
def test_proxy_validation_precedes_dependency_load(monkeypatch, proxy):
    monkeypatch.setattr(http_transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(ValueError) as caught:
        http_transport.request_product('123', headers=HEADERS, cookie='s=synthetic', proxy_url=proxy)
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('proxy', ['http://proxy.test:8080', 'https://u:p@proxy.test:443',
                                  'socks5h://proxy.test:1080', 'http://[::1]:8080'])
def test_explicit_proxy_preserves_transport_security(monkeypatch, proxy):
    calls = []
    class Session:
        def __init__(self, **kwargs):
            assert kwargs == {'trust_env': False}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200)
    monkeypatch.setattr(http_transport, '_load_requests', lambda: SimpleNamespace(Session=Session))
    http_transport.request_product('123', headers=HEADERS, cookie='s=synthetic', proxy_url=proxy)
    url, options = calls[0]
    assert url.startswith('https://affiliate.shopee.vn/')
    assert options['proxy'] == proxy
    assert options['verify'] is True
    assert options['allow_redirects'] is False
    assert options['timeout'] == 20
    assert options['impersonate'] == 'chrome152_macos'
