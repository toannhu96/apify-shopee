"""Offline response-cookie lifecycle contract."""
import json

import pytest

from shopee_affiliate.response_cookies import apply_response_cookies

URL = 'https://affiliate.shopee.vn/api/v3/offer/product?item_id=123'


def test_rotate_preserves_other_scopes_and_duplicate_names():
    source = json.dumps([
        {'name': 's', 'value': 'root', 'domain': '.shopee.vn', 'path': '/'},
        {'name': 's', 'value': 'old', 'domain': 'affiliate.shopee.vn', 'path': '/'},
        {'name': 's', 'value': 'other', 'domain': 'shopee.vn', 'path': '/private'},
    ])
    result = json.loads(apply_response_cookies(source, ('s=new; Path=/; HttpOnly; Secure',), URL, now=100))
    assert [(c['value'], c['hostOnly'], c['domain'], c['path']) for c in result] == [
        ('root', False, '.shopee.vn', '/'), ('new', True, 'affiliate.shopee.vn', '/'),
        ('other', True, 'shopee.vn', '/private')]
    assert result[1]['httpOnly'] is True


def test_expiry_deletion_precedence_and_default_path():
    result = json.loads(apply_response_cookies('s=old', (
        's=deleted; Path=/; Max-Age=0; Expires=Wed, 21 Oct 2037 07:28:00 GMT',
        'n=new; Max-Age=60; Expires=Wed, 21 Oct 2015 07:28:00 GMT',
        'n=last; Max-Age=120',
    ), URL, now=1000))
    assert len(result) == 1
    assert result[0]['path'] == '/api/v3/offer'
    assert result[0]['expires'] == 1120
    assert result[0]['value'] == 'last'
    assert apply_response_cookies('s=old', ('s=x; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT',), URL, now=1000) == '[]'


@pytest.mark.parametrize('header', ['s=bad; Domain=evil.test; Path=/', 's=bad; Domain=vn; Path=/',
    's=bad; Domain=shopee.vn.evil.test; Path=/', 's=bad; Domain=; Path=/',
    's=bad; Partitioned; Path=/'])
def test_invalid_or_unsupported_scope_cannot_replace_existing(header):
    result = json.loads(apply_response_cookies('s=original', (header,), URL))
    assert len(result) == 1 and result[0]['value'] == 'original'


@pytest.mark.parametrize('url', [URL.replace('https:', 'http:'), URL.replace('affiliate.shopee.vn', 'evil.test'),
    URL.replace('affiliate.shopee.vn', 'u:p@affiliate.shopee.vn'), URL.replace('/product?', '/other?')])
def test_untrusted_response_url_rejected(url):
    with pytest.raises(ValueError, match='response URL'):
        apply_response_cookies('s=original', ('s=new',), url)


def test_bounded_headers_and_safe_errors():
    for headers in [('s=secret\r\nother=x',), ('s=' + 'secret' * 2000,), ('s=x',) * 65]:
        with pytest.raises(ValueError) as caught:
            apply_response_cookies('s=old', headers, URL)
        assert 'secret' not in str(caught.value)


def test_empty_snapshot_can_be_read_and_repopulated():
    assert apply_response_cookies('[]', (), URL) == '[]'
    assert json.loads(apply_response_cookies('[]', ('s=new; Path=/',), URL))[0]['value'] == 'new'


def test_storage_validator_accepts_deleted_snapshot_and_rejects_scope_alias_duplicates():
    from shopee_affiliate.response_cookies import validate_response_cookie_source
    assert validate_response_cookie_source(' [] ') == '[]'
    assert json.loads(validate_response_cookie_source('s=x'))[0]['hostOnly'] is True
    duplicate = json.dumps([
        {'name': 's', 'value': 'a', 'domain': 'affiliate.shopee.vn'},
        {'name': 's', 'value': 'b', 'domain': '.affiliate.shopee.vn'},
    ])
    with pytest.raises(ValueError, match='Duplicate'):
        validate_response_cookie_source(duplicate)


@pytest.mark.parametrize('header', ['broken', 'a=x; b=y', '__Secure-s=x', '__Host-s=x; Secure; Domain=shopee.vn; Path=/'])
def test_malformed_or_invalid_prefixed_cookie_ignored(header):
    assert json.loads(apply_response_cookies('s=old', (header,), URL))[0]['value'] == 'old'


def test_domain_replacement_and_invalid_expiry_fallback():
    result = json.loads(apply_response_cookies('s=old', (
        's=new; Domain=affiliate.shopee.vn; Path=/; Max-Age=invalid; Expires=invalid; SameSite=Lax',
    ), URL, now=100))
    assert len(result) == 1
    assert result[0]['hostOnly'] is False
    assert result[0]['domain'] == '.affiliate.shopee.vn'
    assert result[0]['expires'] == -1
    assert result[0]['sameSite'] == 'Lax'


def test_huge_max_age_is_bounded():
    result = json.loads(apply_response_cookies('[]', ('s=x; Max-Age=' + '9' * 5000,), URL, now=100))
    assert result[0]['expires'] == 100 + 10**11


@pytest.mark.parametrize('now', [-1, float('nan'), float('inf'), True, 'secret'])
def test_invalid_clock_rejected_safely(now):
    with pytest.raises(ValueError, match='response time'):
        apply_response_cookies('s=x', (), URL, now=now)


@pytest.mark.parametrize('extension', ['Priority=High', 'Unknown=value', 'UnknownFlag',
    'Priority=High; Another="arbitrary value"'])
def test_unknown_extensions_do_not_block_rotation(extension):
    result = json.loads(apply_response_cookies('s=old', (
        f's=new; {extension}; Path=/; Max-Age=60; HttpOnly; Secure',
    ), URL, now=100))
    assert len(result) == 1
    assert result[0]['value'] == 'new'
    assert result[0]['expires'] == 160
    assert result[0]['httpOnly'] is True
    assert result[0]['secure'] is True


def test_extension_cannot_inject_another_cookie_or_expand_scope():
    result = json.loads(apply_response_cookies('s=old', (
        's=new; extra=evil; Path=/; Priority=High',
        's=foreign; Domain=evil.test; Priority=High; Path=/',
    ), URL))
    assert len(result) == 1
    assert result[0]['name'] == 's' and result[0]['value'] == 'new'
    assert result[0]['hostOnly'] is True
