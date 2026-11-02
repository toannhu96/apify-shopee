"""Bounded cookie rotation from the fixed affiliate HTTPS response only."""
from __future__ import annotations

import json
import math
import re
import time
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from .cookies import HOSTS, parse_cookies

MAX_SET_COOKIE_HEADERS = 64
MAX_SET_COOKIE_LENGTH = 8192
MAX_SET_COOKIE_BYTES = 65536
MAX_JAR_BYTES = 1024 * 1024
MAX_JAR_COOKIES = 1024
HOST = 'affiliate.shopee.vn'
ENDPOINT_PATH = '/api/v3/offer/product'


def bounded_set_cookie_headers(headers) -> tuple[str, ...]:
    """Keep individual header lines; never split Expires values at commas."""
    if not isinstance(headers, (tuple, list)) or len(headers) > MAX_SET_COOKIE_HEADERS:
        raise ValueError('Response cookie headers exceed the limit')
    if any(not isinstance(h, str) or len(h) > MAX_SET_COOKIE_LENGTH
           or any(not 32 <= ord(c) <= 126 for c in h) for h in headers):
        raise ValueError('Invalid response cookie header')
    if sum(len(h) for h in headers) > MAX_SET_COOKIE_BYTES:
        raise ValueError('Response cookie headers exceed the limit')
    return tuple(headers)


def _canonical(cookie):
    domain = cookie.get('domain', urlsplit(cookie.get('url', '')).hostname or HOST)
    host_only = not domain.startswith('.')
    return {**{k: v for k, v in cookie.items() if k != 'url'},
            'domain': domain, 'path': cookie.get('path', '/'), 'hostOnly': host_only}


def _identity(cookie):
    return cookie['name'], cookie['domain'].lstrip('.'), cookie['path']


def _expiry(morsel, now):
    age = morsel['max-age']
    if re.fullmatch(r'-?[0-9]+', age):
        # Clamp before integer conversion, including adversarial digit counts.
        if age.startswith('-') or not age.strip('0'):
            return 0
        return now + min(int(age) if len(age) < 12 else 10**11, 10**11)
    if morsel['expires']:
        try:
            return max(0, parsedate_to_datetime(morsel['expires']).timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    return -1


def _parse_header(header, now):
    pair, *parts = header.split(';')
    attributes = [tuple(part.strip() for part in attribute.partition('=')) for attribute in parts]
    attribute_names = {name.lower() for name, _, _ in attributes}
    # Partitioned cookies cannot be represented by this unpartitioned store.
    if 'partitioned' in attribute_names:
        return None
    parsed = SimpleCookie()
    try:
        # Only the first pair defines a cookie. Unknown extension attributes
        # (e.g. Priority=High) must not become additional SimpleCookie morsels.
        parsed.load(pair)
    except CookieError:
        return None
    if len(parsed) != 1:
        return None
    morsel = next(iter(parsed.values()))
    for name, _, value in attributes:
        key = name.lower()
        if key in ('secure', 'httponly'):
            morsel[key] = True
        elif key in ('domain', 'path', 'expires', 'max-age', 'samesite'):
            morsel[key] = value
    domain_present = 'domain' in attribute_names
    domain = morsel['domain'].lower().removeprefix('.') if domain_present else HOST
    if domain not in HOSTS or not (HOST == domain or HOST.endswith('.' + domain)):
        return None
    path = morsel['path'] if morsel['path'].startswith('/') else '/api/v3/offer'
    if morsel.key.startswith('__Secure-') and not morsel['secure']:
        return None
    if morsel.key.startswith('__Host-') and (not morsel['secure'] or domain_present or path != '/'):
        return None
    cookie = {'name': morsel.key, 'value': morsel.value,
              'domain': ('.' if domain_present else '') + domain, 'hostOnly': not domain_present,
              'path': path, 'secure': bool(morsel['secure']), 'httpOnly': bool(morsel['httponly']),
              'expires': _expiry(morsel, now)}
    same_site = morsel['samesite'].capitalize()
    if same_site in ('Strict', 'Lax', 'None'):
        cookie = {**cookie, 'sameSite': same_site}
    try:
        parse_cookies(json.dumps([cookie]))
    except ValueError:
        return None
    return cookie


def validate_response_cookie_source(cookie_source: str) -> str:
    """Validate a private persisted snapshot; [] is a deliberate empty jar."""
    if not isinstance(cookie_source, str) or len(cookie_source.encode('utf-8')) > MAX_JAR_BYTES:
        raise ValueError('Invalid cookie source')
    source = [] if cookie_source.strip() == '[]' else parse_cookies(cookie_source)
    jar = [_canonical(cookie) for cookie in source]
    if len(jar) > MAX_JAR_COOKIES:
        raise ValueError('Response cookie jar exceeds the limit')
    if len({_identity(cookie) for cookie in jar}) != len(jar):
        raise ValueError('Duplicate cookie identities')
    result = json.dumps(jar, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    if len(result) > MAX_JAR_BYTES:
        raise ValueError('Response cookie jar exceeds the limit')
    return result


def apply_response_cookies(cookie_source: str, headers, response_url: str, *, now=None) -> str:
    """Return scoped JSON, including [] when all cookies were deleted.

    Invalid individual Set-Cookie scopes are ignored, as in a browser. Invalid
    origins, oversized batches and invalid source jars fail with safe messages.
    Cookie identities include domain and path; request signing state is separate.
    """
    url = urlsplit(response_url)
    if (url.scheme != 'https' or url.netloc != HOST or url.path != ENDPOINT_PATH or url.fragment):
        raise ValueError('Invalid response URL for cookies')
    headers = bounded_set_cookie_headers(headers)
    current_time = time.time() if now is None else now
    if type(current_time) not in (int, float) or not math.isfinite(current_time) or current_time < 0:
        raise ValueError('Invalid cookie response time')
    jar = json.loads(validate_response_cookie_source(cookie_source))
    for header in headers:
        cookie = _parse_header(header, current_time)
        if cookie is None:
            continue
        identity = _identity(cookie)
        index = next((i for i, old in enumerate(jar) if _identity(old) == identity), len(jar))
        retained = [old for old in jar if _identity(old) != identity]
        jar = (retained if cookie['expires'] != -1 and cookie['expires'] <= current_time
               else retained[:index] + [cookie] + retained[index:])
    result = json.dumps(jar, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    if len(jar) > MAX_JAR_COOKIES or len(result) > MAX_JAR_BYTES:
        raise ValueError('Response cookie jar exceeds the limit')
    return result
