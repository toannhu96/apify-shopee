import io
import json
import sys
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError

import pytest

from shopee_affiliate import direct

ITEM = "26609048170"
PAYLOAD = {"code": 0, "data": {"item_id": ITEM,
    "batch_item_for_item_card_full": {"itemid": ITEM, "name": "Product", "price": "100000"}}}


class Response(io.BytesIO):
    status = 200


def mock_response(monkeypatch, payload=PAYLOAD, raw=None, status=200):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            response = Response(raw if raw is not None else json.dumps(payload).encode())
            response.status = status
            return response

    monkeypatch.setattr(direct, "build_opener", lambda *handlers: Opener())
    return calls


def test_fetch_product(monkeypatch):
    calls = mock_response(monkeypatch)
    result = direct.fetch_product("session=secret==", ITEM, {"x-sap-ri": "test"})
    assert result["raw"] == PAYLOAD
    assert result["product"]["price_vnd"] == 1
    request, timeout = calls[0]
    assert request.full_url == direct.ENDPOINT + "?item_id=" + ITEM
    assert request.get_header("Cookie") == "session=secret=="
    assert request.get_header("X-sap-ri") == "test"
    assert timeout == 30
    assert len(calls) == 1


@pytest.mark.parametrize("item", [True, 0, -1, "", "01", "1&x=y", "１２", "1" * 21, None])
def test_invalid_id(item):
    with pytest.raises(ValueError):
        direct.fetch_product("s=v", item)


@pytest.mark.parametrize("headers", [{"Host": "evil"}, {"Cookie": "evil"},
    {"Authorization": "evil"}, {"x-sap-ri": "a\nb"}, {"x-sap-ri": 1},
    {"x-sap-ri": "a", "X-SAP-RI": "b"}, ["bad"]])
def test_invalid_headers(headers):
    with pytest.raises(ValueError):
        direct.fetch_product("s=v", ITEM, headers)


def test_cookie_scope_and_order():
    cookies = [
        {"name": "host", "value": "skip", "domain": "shopee.vn"},
        {"name": "url", "value": "skip", "url": "https://shopee.vn"},
        {"name": "suffix", "value": "skip", "domain": ".shopee.vn", "path": "/api/v3/off"},
        {"name": "expired", "value": "skip", "domain": ".shopee.vn", "expires": 1},
        {"name": "s", "value": "root", "domain": ".shopee.vn"},
        {"name": "s", "value": "path", "domain": "affiliate.shopee.vn", "path": "/api/v3"},
        {"name": "s", "value": "exact", "domain": ".affiliate.shopee.vn", "path": "/api/v3/offer/product"},
        {"name": "session", "value": "ok", "url": "https://affiliate.shopee.vn", "expires": -1},
    ]
    assert direct.cookie_header(json.dumps(cookies)) == "s=exact; s=path; s=root; session=ok"


def test_no_applicable_cookies():
    with pytest.raises(ValueError):
        direct.cookie_header('[{"name":"s","value":"secret","domain":"shopee.vn"}]')


def test_explicit_host_only_cookie_scope():
    source = json.dumps([
        {"name": "skip", "value": "secret", "domain": ".shopee.vn", "hostOnly": True},
        {"name": "send", "value": "ok", "domain": "shopee.vn", "hostOnly": False},
    ])
    assert direct.cookie_header(source) == "send=ok"


@pytest.mark.parametrize("payload", [[], {}, {"code": False}, {"code": 0, "data": []},
    {"code": 0, "data": {"item_id": "1"}},
    {"code": 0, "data": {"item_id": ITEM, "batch_item_for_item_card_full": {"itemid": "1"}}},
    {"code": 0, "data": {"item_id": ITEM, "batch_item_for_item_card_full": {"price": "bad"}}},
    {"error": "secret"}, {"code": 3, "msg": "secret"}])
def test_invalid_payload(monkeypatch, payload):
    mock_response(monkeypatch, payload)
    with pytest.raises(direct.DirectError) as exc:
        direct.fetch_product("s=v", ITEM)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("raw", [b"<html>secret</html>", b'{"code":NaN}', b'{"code":0,"code":1}',
    b'{"code":0,"n":1e999}', b"\xff"])
def test_invalid_json(monkeypatch, raw):
    mock_response(monkeypatch, raw=raw)
    with pytest.raises(direct.DirectError):
        direct.fetch_product("s=v", ITEM)


@pytest.mark.parametrize("payload", [{"error": 90309999}, {"code": 90309999},
    {"error": "90309999"}, {"code": "90309999"}])
def test_api_block(monkeypatch, payload):
    mock_response(monkeypatch, payload)
    with pytest.raises(direct.DirectBlockedError, match="90309999"):
        direct.fetch_product("s=v", ITEM)


@pytest.mark.parametrize("flag", [{"is_login": False}, {"redirect_to_error_page": True}])
def test_block_flags_override_product_payload(monkeypatch, flag):
    mock_response(monkeypatch, {**PAYLOAD, **flag})
    with pytest.raises(direct.DirectBlockedError):
        direct.fetch_product("s=v", ITEM)


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_http_error(monkeypatch, status):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, status, "secret", {}, io.BytesIO(b"secret"))
    monkeypatch.setattr(direct, "build_opener", lambda *args: Opener())
    error = direct.DirectBlockedError if status in (401, 403, 429) else direct.DirectError
    with pytest.raises(error, match=str(status)) as exc:
        direct.fetch_product("s=secret", ITEM)
    assert "secret" not in str(exc.value)


def test_no_redirect():
    assert direct.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test") is None


@pytest.mark.parametrize("error", [URLError("secret"), IncompleteRead(b"secret")])
def test_network_error(monkeypatch, error):
    class Opener:
        def open(self, request, timeout):
            raise error
    monkeypatch.setattr(direct, "build_opener", lambda *args: Opener())
    with pytest.raises(direct.DirectError, match="Network request failed"):
        direct.fetch_product("s=v", ITEM)


def test_oversized_response(monkeypatch):
    mock_response(monkeypatch, raw=b" " * (direct.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(direct.DirectError, match="size"):
        direct.fetch_product("s=v", ITEM)


def test_cli_stdin(monkeypatch, capsys):
    mock_response(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("s=secret"))
    assert direct.main([ITEM, "--cookie-stdin"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["product"]["item_id"] == ITEM
    assert output.err == ""


def test_cli_file(monkeypatch, capsys, tmp_path):
    path = tmp_path / "cookie.txt"
    path.write_text("s=secret")
    mock_response(monkeypatch)
    assert direct.main([ITEM, "--cookie-file", str(path)]) == 0
    assert "secret" not in capsys.readouterr().out


def test_cli_errors(monkeypatch, capsys):
    monkeypatch.setenv("SHOPEE_COOKIES", "s=secret")
    mock_response(monkeypatch, {"error": 90309999})
    assert direct.main([ITEM]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "90309999" in output.err
    assert "secret" not in output.err


def test_cli_missing_cookie(monkeypatch, capsys):
    monkeypatch.delenv("SHOPEE_COOKIES", raising=False)
    assert direct.main([ITEM]) == 1
    assert "Cookie input" in capsys.readouterr().err


def test_cli_experimental_signer(monkeypatch, capsys):
    from shopee_affiliate import signing
    monkeypatch.setenv("SHOPEE_COOKIES", "s=secret")
    signatures = {"x-sap-ri": "r" * 52, "x-sap-sec": "s" * 712}
    calls = []

    def sign(path, url):
        calls.append((path, url))
        return signatures

    monkeypatch.setattr(signing, "sign_headers", sign)
    requests = mock_response(monkeypatch)
    assert direct.main([ITEM, "--experimental-signer", "local.cjs"]) == 0
    assert len(calls) == 1
    assert calls[0][1] == requests[0][0].full_url
    assert requests[0][0].get_header("X-sap-sec") == signatures["x-sap-sec"]
    assert "secret" not in capsys.readouterr().out


def test_cli_signer_failure(monkeypatch, capsys):
    from shopee_affiliate import signing
    monkeypatch.setenv("SHOPEE_COOKIES", "s=secret")

    def sign(*args):
        raise signing.SigningError("Experimental signer failed")

    monkeypatch.setattr(signing, "sign_headers", sign)
    assert direct.main([ITEM, "--experimental-signer", "local.cjs"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Experimental signer failed\n"
