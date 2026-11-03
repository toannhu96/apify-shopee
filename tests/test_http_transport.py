"""No-network tests for the captured-profile direct HTTP transport."""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from shopee_affiliate import http_transport as transport

ITEM = '26609048170'
URL = 'https://affiliate.shopee.vn/api/v3/offer/product?item_id=' + ITEM
HEADERS = {'accept': 'application/json', 'affiliate-program-type': '1',
           'x-sap-ri': 'synthetic-ri', 'x-sap-sec': 'synthetic-sec'}
COOKIE = 'duplicate=path; duplicate=root; session=synthetic=='


def mock_session(monkeypatch, *, chunks=(b'{"code":', b'0}'), status=200, failure=None, response_headers=None):
    calls = []

    class Session:
        def __init__(self, **kwargs):
            calls.append(('session', kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append(('closed',))

        def get(self, url, **kwargs):
            calls.append(('get', url, kwargs))
            if failure is not None:
                raise failure
            for chunk in chunks:
                assert kwargs['content_callback'](chunk) == len(chunk)
            return SimpleNamespace(status_code=status, headers=response_headers)

    monkeypatch.setattr(transport, '_load_requests', lambda: SimpleNamespace(Session=Session))
    return calls


def test_exact_transport_configuration_and_order(monkeypatch):
    calls = mock_session(monkeypatch)
    before = dict(HEADERS)
    result = transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)
    assert result.status == 200
    assert result.body == b'{"code":0}'
    assert HEADERS == before
    assert calls[0] == ('session', {'trust_env': False})
    get = calls[1]
    assert get[1] == URL
    assert get[2]['headers'] == {**HEADERS, 'Cookie': COOKIE}
    assert list(get[2]['headers']) == list(HEADERS) + ['Cookie']
    assert get[2]['impersonate'] == 'chrome152_macos'
    assert get[2]['default_headers'] is False
    assert get[2]['verify'] is True
    assert get[2]['allow_redirects'] is False
    assert get[2]['timeout'] == 20
    assert calls[-1] == ('closed',)
    assert sum(call[0] == 'get' for call in calls) == 1
    assert COOKIE not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.status = 500


@pytest.mark.parametrize('target', [ITEM, int(ITEM), URL])
def test_product_url(target):
    assert transport.product_url(target) == URL


@pytest.mark.parametrize('target', [True, None, 0, -1, '', '01', '１２', '1' * 21,
    URL + '#fragment', URL + '&extra=1', URL + '&item_id=1',
    URL.replace('https:', 'http:'), URL.replace('affiliate.shopee.vn', 'other.test'),
    URL.replace('affiliate.shopee.vn', 'affiliate.shopee.vn:443'),
    URL.replace('affiliate.shopee.vn', 'u:p@affiliate.shopee.vn'),
    URL.replace('item_id=', 'item_id=%32'), URL.replace('/product?', '/product/?'),
    URL + '\n', 'https://affiliate.shopee.vn/api/v3/offer/product?item_id=1&x=secret'])
def test_invalid_target_rejected_before_dependency_load(monkeypatch, target):
    monkeypatch.setattr(transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(ValueError) as caught:
        transport.request_product(target, headers=HEADERS, cookie=COOKIE)
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('headers', [None, [], {'Cookie': 'secret'}, {'cookie': 'secret'},
    {':authority': 'affiliate.shopee.vn'}, {'Host': 'other.test'},
    {'Authorization': 'secret'}, {'Proxy-Authorization': 'secret'},
    {'X-Sap-Ri': 'a', 'x-sap-ri': 'b'}, {'x-sap-ri': 'a\r\nb'},
    {'x-sap-ri': 'a\tb'}, {'x-sap-ri': 'a\0b'}, {'x-sap-ri': '\x7f'},
    {'x-sap-ri': 'é'}, {'x-sap-ri': 1}, {1: 'secret'},
    {'x-sap-ri': 'x' * 16385}])
def test_invalid_headers(monkeypatch, headers):
    monkeypatch.setattr(transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(ValueError) as caught:
        transport.request_product(ITEM, headers=headers, cookie=COOKIE)
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('cookie', ['', None, 1, 'x' * 65537, 's=secret\n', 's=é', 's=\x7f'])
def test_invalid_cookie(monkeypatch, cookie):
    monkeypatch.setattr(transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(ValueError) as caught:
        transport.request_product(ITEM, headers=HEADERS, cookie=cookie)
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('status', [301, 302, 401, 403, 429, 500])
def test_status_is_returned_without_following_or_retrying(monkeypatch, status):
    calls = mock_session(monkeypatch, status=status, chunks=(b'synthetic-body',))
    result = transport.request_product(URL, headers=HEADERS, cookie=COOKIE)
    assert result.status == status
    assert result.body == b'synthetic-body'
    assert sum(call[0] == 'get' for call in calls) == 1
    assert calls[-1] == ('closed',)


def test_size_limit_exact_boundary(monkeypatch):
    monkeypatch.setattr(transport, 'MAX_RESPONSE_BYTES', 8)
    mock_session(monkeypatch, chunks=(b'1234', b'5678'))
    assert transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE).body == b'12345678'


def test_size_limit_aborts_before_retaining_overflow(monkeypatch):
    monkeypatch.setattr(transport, 'MAX_RESPONSE_BYTES', 8)
    calls = mock_session(monkeypatch, chunks=(b'1234', b'56789', b'should not be received'))
    with pytest.raises(transport.TransportError, match='size limit'):
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)
    assert sum(call[0] == 'get' for call in calls) == 1
    assert calls[-1] == ('closed',)


@pytest.mark.parametrize('failure', [RuntimeError('session=secret'), OSError('secret')])
def test_network_failure_is_sanitized_and_closed(monkeypatch, failure):
    calls = mock_session(monkeypatch, failure=failure)
    with pytest.raises(transport.TransportError, match='failed') as caught:
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)
    assert 'secret' not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert calls[-1] == ('closed',)


def test_missing_dependency_is_sanitized(monkeypatch):
    def missing(_name):
        raise ImportError('secret import detail')
    monkeypatch.setattr(transport, 'import_module', missing)
    with pytest.raises(transport.TransportError, match='curl_cffi') as caught:
        transport._load_requests()
    assert 'secret' not in str(caught.value)


def test_oversized_total_headers(monkeypatch):
    headers = {name: 'x' * 16384 for name in transport.ALLOWED_HEADERS}
    monkeypatch.setattr(transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(ValueError):
        transport.request_product(ITEM, headers=headers, cookie=COOKIE)


def test_load_dependency_success(monkeypatch):
    stub = object()
    names = []
    def load(name):
        names.append(name)
        return stub
    monkeypatch.setattr(transport, 'import_module', load)
    assert transport._load_requests() is stub
    assert names == ['curl_cffi.requests']


@pytest.mark.parametrize('status', [True, None, '200', 99, 600])
def test_invalid_response_status_sanitized(monkeypatch, status):
    mock_session(monkeypatch, status=status)
    with pytest.raises(transport.TransportError, match='failed'):
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)


def test_invalid_response_chunk_sanitized(monkeypatch):
    mock_session(monkeypatch, chunks=('secret',))
    with pytest.raises(transport.TransportError, match='failed') as caught:
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)
    assert 'secret' not in str(caught.value)


def test_truncation_not_accepted_if_library_swallows_callback_exception(monkeypatch):
    monkeypatch.setattr(transport, 'MAX_RESPONSE_BYTES', 8)
    class Session:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            try:
                kwargs['content_callback'](b'123456789')
            except transport.TransportError:
                pass  # Simulate a library swallowing its callback error.
            return SimpleNamespace(status_code=200)
    monkeypatch.setattr(transport, '_load_requests', lambda: SimpleNamespace(Session=Session))
    with pytest.raises(transport.TransportError, match='size limit'):
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)


def test_many_small_chunks_have_linear_buffering(monkeypatch):
    mock_session(monkeypatch, chunks=(b'x' for _ in range(100000)))
    result = transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)
    assert result.body == b'x' * 100000


def test_transport_preserves_separate_set_cookie_headers(monkeypatch):
    from curl_cffi.requests.headers import Headers
    headers = Headers([
        ('Set-Cookie', 'session=private-rotated; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/'),
        ('Set-Cookie', 'other=private-second; Path=/api'),
    ])
    mock_session(monkeypatch, response_headers=headers)
    result = transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE)
    assert result.set_cookie_headers == tuple(headers.get_list('set-cookie'))
    assert len(result.set_cookie_headers) == 2
    assert 'private-' not in repr(result)


@pytest.mark.parametrize('headers', [('s=x',) * 65, ('s=' + 'secret' * 2000,), ('s=secret\r\nx=y',)])
def test_response_dataclass_rejects_unbounded_headers(headers):
    with pytest.raises(ValueError) as caught:
        transport.HttpResponse(200, b'{}', headers)
    assert 'secret' not in str(caught.value)


@pytest.fixture
def proxy_ca_file(tmp_path):
    import re
    from pathlib import Path

    import certifi
    cert = re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                     Path(certifi.where()).read_text(), re.DOTALL).group(0)
    path = tmp_path / 'operator-ca.pem'
    path.write_text(cert + '\n')
    return path


def test_explicit_https_proxy_ca_does_not_replace_origin_trust(monkeypatch, proxy_ca_file):
    from curl_cffi.const import CurlOpt
    from curl_cffi.curl import DEFAULT_CACERT
    monkeypatch.setenv('SH_PROXY_CA_FILE', str(proxy_ca_file))
    calls = mock_session(monkeypatch)
    transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE,
                              proxy_url='https://proxy.example:8443')
    assert calls[0] == ('session', {'trust_env': False, 'curl_options': {
        CurlOpt.PROXY_CAINFO: str(proxy_ca_file), CurlOpt.PROXY_SSL_VERIFYPEER: 1,
        CurlOpt.PROXY_SSL_VERIFYHOST: 2, CurlOpt.CAINFO: DEFAULT_CACERT,
    }})
    assert calls[1][2]['verify'] is True
    assert calls[1][2]['proxy'] == 'https://proxy.example:8443'


@pytest.mark.parametrize('proxy', [None, 'http://proxy.example:8080', 'socks5://proxy.example:1080'])
def test_proxy_ca_config_not_loaded_without_https_proxy(monkeypatch, proxy):
    monkeypatch.setenv('SH_PROXY_CA_FILE', '/missing/secret-cert-path')
    calls = mock_session(monkeypatch)
    transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE, proxy_url=proxy)
    assert calls[0] == ('session', {'trust_env': False})
    assert calls[1][2]['verify'] is True


@pytest.mark.parametrize('kind', ['missing', 'directory', 'invalid', 'empty', 'oversized', 'private_key', 'fifo', 'symlink'])
def test_invalid_proxy_ca_fails_before_dependency_or_network(monkeypatch, tmp_path, kind):
    import os
    path = tmp_path / 'SECRET-private-path.pem'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'symlink':
        path.symlink_to(tmp_path / 'missing-target')
    elif kind != 'missing':
        path.write_text({'invalid': 'SECRET-not-a-cert', 'empty': '', 'oversized': 'x' * 262145,
                         'private_key': '-----BEGIN ' + 'PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----'}[kind])
    monkeypatch.setenv('SH_PROXY_CA_FILE', str(path))
    monkeypatch.setattr(transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(transport.TransportError) as caught:
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE,
                                  proxy_url='https://user:SECRET@proxy.example:8443')
    assert caught.value.category == 'input'
    assert 'SECRET' not in str(caught.value)
    assert str(path) not in str(caught.value)


def test_proxy_ca_survives_real_curl_certificate_initialization(monkeypatch, proxy_ca_file):
    from curl_cffi import Curl
    from curl_cffi.const import CurlOpt
    from curl_cffi.curl import DEFAULT_CACERT
    monkeypatch.setenv('SH_PROXY_CA_FILE', str(proxy_ca_file))
    calls = mock_session(monkeypatch)
    transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE,
                              proxy_url='https://proxy.example:8443')
    settings = {}
    original = Curl.setopt
    def recording_setopt(self, option, value):
        settings[option] = value
        return original(self, option, value)
    monkeypatch.setattr(Curl, 'setopt', recording_setopt)
    curl = Curl()
    try:
        for option, value in calls[0][1]['curl_options'].items():
            curl.setopt(option, value)
        curl._ensure_cacert()  # perform's local preparation, without opening a socket
    finally:
        curl.close()
    assert settings[CurlOpt.PROXY_CAINFO] == str(proxy_ca_file)
    assert settings[CurlOpt.CAINFO] == DEFAULT_CACERT


def test_https_proxy_without_custom_ca_still_verifies_both_tls_layers(monkeypatch):
    from curl_cffi.const import CurlOpt
    monkeypatch.delenv('SH_PROXY_CA_FILE', raising=False)
    calls = mock_session(monkeypatch)
    transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE,
                              proxy_url='https://proxy.example:8443')
    options = calls[0][1]['curl_options']
    assert options[CurlOpt.PROXY_SSL_VERIFYPEER] == 1
    assert options[CurlOpt.PROXY_SSL_VERIFYHOST] == 2
    assert CurlOpt.PROXY_CAINFO not in options
    assert calls[1][2]['verify'] is True


@pytest.mark.parametrize('configured', ['', 'x' * 4097, 'secret\npath'])
def test_invalid_proxy_ca_path_setting_is_redacted(monkeypatch, configured):
    monkeypatch.setenv('SH_PROXY_CA_FILE', configured)
    monkeypatch.setattr(transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(transport.TransportError, match='Invalid operator HTTPS proxy CA configuration'):
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE, proxy_url='https://proxy.example')


@pytest.mark.parametrize('content', ['-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----',
    '-----BEGIN CERTIFICATE-----\n☃\n-----END CERTIFICATE-----'])
def test_invalid_certificate_encoding_rejected_before_socket(monkeypatch, tmp_path, content):
    path = tmp_path / 'bad.pem'
    path.write_text(content)
    monkeypatch.setenv('SH_PROXY_CA_FILE', str(path))
    monkeypatch.setattr(transport, '_load_requests', lambda: pytest.fail('dependency loaded'))
    with pytest.raises(transport.TransportError) as caught:
        transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE, proxy_url='https://proxy.example')
    assert caught.value.category == 'input'


def test_proxy_ca_unreadable_file_is_rejected(monkeypatch, proxy_ca_file):
    proxy_ca_file.chmod(0)
    monkeypatch.setenv('SH_PROXY_CA_FILE', str(proxy_ca_file))
    try:
        with pytest.raises(transport.TransportError) as caught:
            transport.request_product(ITEM, headers=HEADERS, cookie=COOKIE, proxy_url='https://proxy.example')
        assert caught.value.category == 'input'
    finally:
        proxy_ca_file.chmod(0o600)
