import json

import pytest

from shopee_affiliate.cookies import parse_cookies


def test_plain_cookie_header_is_scoped_to_affiliate_host():
    cookies = parse_cookies("a=private; b=has=equals")
    assert cookies == [
        {"name": "a", "value": "private", "url": "https://affiliate.shopee.vn", "secure": True},
        {"name": "b", "value": "has=equals", "url": "https://affiliate.shopee.vn", "secure": True},
    ]


def test_plain_cookie_file_allows_trailing_editor_newline():
    assert parse_cookies("a=private\r\n")[0]["value"] == "private"


def test_storage_state_preserves_cookie_scope_not_storage():
    source = {"cookies": [{"name": "a", "value": "private", "domain": ".shopee.vn", "path": "/",
                           "expires": -1, "httpOnly": True, "sameSite": "Lax"}], "origins": []}
    assert parse_cookies(json.dumps(source))[0] == {**source["cookies"][0], "secure": True}


def test_json_origin_cookie_and_windows_newlines():
    source = '[\r\n{"name":"a","value":"private","url":"https://affiliate.shopee.vn/"}\r\n]'
    assert parse_cookies(source)[0]["url"] == "https://affiliate.shopee.vn"


@pytest.mark.parametrize("source", [
    "", "bad-private", "a=private\r\nx=1", "a=private; a=second", '[{"name":"x","value":"private","domain":"evil.test"}]',
    '[{"name":"x","value":"private","domain":"shopee.vn.evil.test"}]',
    '[{"name":"x","value":"private","url":"http://affiliate.shopee.vn"}]',
    '[{"name":"x","value":"private","url":"https://affiliate.shopee.vn@evil.test"}]',
    '[{"name":"x","value":"private","domain":".shopee.vn","path":"bad"}]',
    '[{"name":"x","value":"private","domain":".shopee.vn","expires":"private"}]',
    '[{"name":"x","value":"private","domain":".shopee.vn","sameSite":"private"}]',
    "[]", '{"other":"private"}', '{"cookies":["private"]}', '{"cookies":',
])
def test_invalid_cookie_source_does_not_leak_values(source):
    with pytest.raises(ValueError) as exc:
        parse_cookies(source)
    assert "private" not in str(exc.value)


@pytest.mark.parametrize(("domain", "host_only", "canonical"), [
    ("shopee.vn", False, ".shopee.vn"), (".shopee.vn", True, "shopee.vn"),
    ("affiliate.shopee.vn", False, ".affiliate.shopee.vn"),
])
def test_explicit_host_only_scope(domain, host_only, canonical):
    source = json.dumps([{"name": "s", "value": "private", "domain": domain, "hostOnly": host_only}])
    assert parse_cookies(source)[0]["domain"] == canonical


@pytest.mark.parametrize("extra", [{"hostOnly": "private"}, {"expires": 10 ** 400}])
def test_invalid_scope_and_overflow_expiry(extra):
    source = json.dumps([{"name": "s", "value": "private", "domain": ".shopee.vn", **extra}])
    with pytest.raises(ValueError) as exc:
        parse_cookies(source)
    assert "private" not in str(exc.value)


def test_url_cookie_cannot_declare_domain_scope():
    with pytest.raises(ValueError):
        parse_cookies(json.dumps([{"name": "s", "value": "private", "hostOnly": False,
                                   "url": "https://shopee.vn"}]))
