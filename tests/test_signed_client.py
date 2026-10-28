"""Offline tests for the browser-free signer/transport client integration."""
import io
import json
import sys
from types import SimpleNamespace

import pytest

from shopee_affiliate import direct, http_transport, signed_client as client

ITEM = '26609048170'
COOKIE = 'session=synthetic=='
SEED = {'X-SAP-RI': 'synthetic-ri', 'X-SAP-SEC': 'synthetic-sec',
        'AF-AC-ENC-SZ-TOKEN': 'synthetic-token', 'User-Agent': 'Synthetic-UA',
        'X-SZ-SDK-VERSION': '1.12.21', 'Accept': 'application/json'}
FRESH = {key.lower(): value for key, value in SEED.items()} | {
    'x-sap-ri': 'fresh-ri', 'x-sap-sec': 'fresh-sec', 'af-ac-enc-dat': 'fresh-dat'}
PAYLOAD = {'code': 0, 'data': {'item_id': ITEM,
    'batch_item_for_item_card_full': {'itemid': ITEM, 'name': 'Product', 'price': '100000'}}}


def wire(monkeypatch, *, status=200, payload=PAYLOAD, failure=None):
    calls = []
    def request(target, **kwargs):
        calls.append((target, kwargs))
        if failure is not None:
            raise failure
        return http_transport.HttpResponse(status, json.dumps(payload).encode())
    monkeypatch.setattr(client.http_transport, 'request_product', request)
    return calls


def signer_calls():
    calls = []
    def signer(item, headers):
        calls.append((item, dict(headers)))
        headers['x-sap-ri'] = 'modified-copy'
        return dict(FRESH)
    return signer, calls


def test_sign_once_then_request_once_without_changing_inputs(monkeypatch):
    requests = wire(monkeypatch)
    signer, calls = signer_calls()
    seed = dict(SEED)
    result = client.fetch_product(ITEM, seed_headers=seed, cookie_source=COOKIE, signer=signer)
    assert result['raw'] == PAYLOAD
    assert result['product']['price_vnd'] == 1
    assert seed == SEED
    assert calls == [(ITEM, {key.lower(): value for key, value in SEED.items()})]
    assert requests == [(ITEM, {'headers': FRESH, 'cookie': COOKIE})]


def test_existing_json_cookie_parser_is_used(monkeypatch):
    calls = wire(monkeypatch)
    source = json.dumps([
        {'name': 'wrong', 'value': 'skip', 'domain': 'shopee.vn'},
        {'name': 'session', 'value': 'root', 'domain': '.shopee.vn'},
        {'name': 'session', 'value': 'path', 'domain': 'affiliate.shopee.vn', 'path': '/api/v3'},
    ])
    client.fetch_product(int(ITEM), seed_headers=SEED, cookie_source=source,
                         signer=lambda *args: FRESH)
    assert calls[0][1]['cookie'] == 'session=path; session=root'


def test_default_signer_is_actual_pure_signer_import(monkeypatch):
    calls = []
    def signer(item, headers):
        calls.append(item)
        return FRESH
    monkeypatch.setitem(sys.modules, 'shopee_affiliate.sap_signer', SimpleNamespace(refresh_headers=signer))
    wire(monkeypatch)
    client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE)
    assert calls == [ITEM]


@pytest.mark.parametrize('item', [True, None, 0, -1, '01', '１２', '1' * 21, 'https://other.test/?secret'])
def test_invalid_item_before_signing(monkeypatch, item):
    wire(monkeypatch, failure=AssertionError('transport called'))
    with pytest.raises(direct.DirectError) as caught:
        client.fetch_product(item, seed_headers=SEED, cookie_source=COOKIE,
            signer=lambda *args: pytest.fail('signer called'))
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('seed', [{}, [], None, {**SEED, 'Cookie': 'secret'},
    {**SEED, 'cookie': 'secret'}, {**SEED, ':authority': 'other.test'},
    {**SEED, 'X-SAP-RI': 'a\r\nsecret'}, {**SEED, 'x-sap-ri': 'duplicate'},
    {**SEED, 'User-Agent': ''}, {**SEED, 'X-SAP-SEC': 1}])
def test_invalid_seed_before_signing(monkeypatch, seed):
    wire(monkeypatch, failure=AssertionError('transport called'))
    with pytest.raises(direct.DirectError) as caught:
        client.fetch_product(ITEM, seed_headers=seed, cookie_source=COOKIE,
            signer=lambda *args: pytest.fail('signer called'))
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('source', ['', None, 1, 's=secret\nX: bad', 'x' * (1024 * 1024 + 1)],
                         ids=['empty', 'null', 'integer', 'newline', 'oversized'])
def test_invalid_cookie_before_signing(monkeypatch, source):
    wire(monkeypatch, failure=AssertionError('transport called'))
    with pytest.raises(direct.DirectError) as caught:
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=source,
            signer=lambda *args: pytest.fail('signer called'))
    assert 'secret' not in str(caught.value)


def test_signer_failure_never_reuses_seed_headers(monkeypatch):
    wire(monkeypatch, failure=AssertionError('transport called'))
    def signer(*args):
        raise ValueError('secret signer state')
    with pytest.raises(direct.DirectError, match='signing') as caught:
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE, signer=signer)
    assert 'secret' not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize('status', [301, 302, 307, 401, 403, 429])
def test_block_status_stops_without_retry(monkeypatch, status):
    calls = wire(monkeypatch, status=status)
    with pytest.raises(direct.DirectBlockedError, match=str(status)):
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE, signer=lambda *a: FRESH)
    assert len(calls) == 1


@pytest.mark.parametrize('status', [204, 404, 500])
def test_other_bad_status(monkeypatch, status):
    calls = wire(monkeypatch, status=status)
    with pytest.raises(direct.DirectError, match=str(status)):
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE, signer=lambda *a: FRESH)
    assert len(calls) == 1


def test_api_block_payload(monkeypatch):
    calls = wire(monkeypatch, payload={'error': 90309999})
    with pytest.raises(direct.DirectBlockedError, match='90309999'):
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE, signer=lambda *a: FRESH)
    assert len(calls) == 1


def test_transport_error_is_sanitized(monkeypatch):
    wire(monkeypatch, failure=http_transport.TransportError('safe transport failure'))
    with pytest.raises(direct.DirectError, match='transport failure'):
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE, signer=lambda *a: FRESH)


def test_load_state_normalizes_headers_only(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'headers': SEED}))
    assert client.load_state(path) == {key.lower(): value for key, value in SEED.items()}


@pytest.mark.parametrize('raw', [b'private-value', b'[]', b'{}', b'{"headers":{}}',
    b'{"headers":{},"cookies":"private-value"}', b'{"headers":{},"headers":{}}',
    b'{"headers":{"x-sap-ri":NaN}}', b'\xff', b' ' * 131073])
def test_load_state_rejects_invalid_and_secret_content(tmp_path, raw):
    path = tmp_path / 'state.json'
    path.write_bytes(raw)
    with pytest.raises(direct.DirectError) as caught:
        client.load_state(path)
    assert 'private-value' not in str(caught.value)


def setup_cli(monkeypatch, tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'headers': SEED}))
    calls = wire(monkeypatch)
    monkeypatch.setitem(sys.modules, 'shopee_affiliate.sap_signer',
        SimpleNamespace(refresh_headers=lambda *args: FRESH))
    return path, calls


def test_cli_cookie_file(monkeypatch, tmp_path, capsys):
    state, calls = setup_cli(monkeypatch, tmp_path)
    cookie = tmp_path / 'cookies.txt'
    cookie.write_text(COOKIE)
    assert client.main([ITEM, '--state-file', str(state), '--cookie-file', str(cookie)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)['raw'] == PAYLOAD
    assert not output.err
    assert len(calls) == 1


def test_cli_cookie_stdin(monkeypatch, tmp_path, capsys):
    state, calls = setup_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(client.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(COOKIE.encode())))
    assert client.main([ITEM, '--state-file', str(state), '--cookie-stdin']) == 0
    assert json.loads(capsys.readouterr().out)['raw'] == PAYLOAD
    assert len(calls) == 1


def test_cli_blocked_exit(monkeypatch, tmp_path, capsys):
    state, _calls = setup_cli(monkeypatch, tmp_path)
    wire(monkeypatch, status=403)
    monkeypatch.setattr(client.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(COOKIE.encode())))
    assert client.main([ITEM, '--state-file', str(state), '--cookie-stdin']) == 2
    output = capsys.readouterr()
    assert not output.out and '403' in output.err and COOKIE not in output.err


def test_cli_unreadable_file(monkeypatch, tmp_path, capsys):
    state, calls = setup_cli(monkeypatch, tmp_path)
    assert client.main([ITEM, '--state-file', str(state), '--cookie-file', str(tmp_path / 'missing')]) == 1
    output = capsys.readouterr()
    assert not output.out and COOKIE not in output.err
    assert not calls


@pytest.mark.parametrize('source', [
    '[{"name":"s","name":"other","value":"v","domain":".shopee.vn"}]',
    '[{"name":"s","value":"v","domain":".shopee.vn","ignored":NaN}]',
    '[{"name":"s","value":"v","domain":".shopee.vn","ignored":1e999}]',
    's=é', 's=' + 'x' * 65536,
], ids=['duplicate-key', 'nonstandard-constant', 'nonfinite-number', 'non-ascii-cookie', 'wire-cookie-limit'])
def test_cookie_source_strict_json_and_wire_validated_before_signing(monkeypatch, source):
    wire(monkeypatch, failure=AssertionError('transport called'))
    with pytest.raises(direct.DirectError, match='Cookie'):
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=source,
            signer=lambda *args: pytest.fail('signer called'))


@pytest.mark.parametrize('headers', [None, {}, {**FRESH, 'Cookie': 'secret'}, {**FRESH, 'user-agent': '\nsecret'}])
def test_invalid_signer_output_never_reaches_transport(monkeypatch, headers):
    wire(monkeypatch, failure=AssertionError('transport called'))
    with pytest.raises(direct.DirectError, match='signing') as caught:
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE, signer=lambda *args: headers)
    assert 'secret' not in str(caught.value)


def test_missing_actual_signer_has_no_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, 'shopee_affiliate.sap_signer', None)
    wire(monkeypatch, failure=AssertionError('transport called'))
    with pytest.raises(direct.DirectError, match='unavailable'):
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE)


def test_transport_input_validation_error_is_client_error(monkeypatch):
    wire(monkeypatch, failure=ValueError('secret rejected input'))
    with pytest.raises(direct.DirectError, match='request inputs') as caught:
        client.fetch_product(ITEM, seed_headers=SEED, cookie_source=COOKIE, signer=lambda *a: FRESH)
    assert 'secret' not in str(caught.value)
