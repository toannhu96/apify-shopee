"""One bounded browser-free GET using the live-verified curl transport profile.

The caller supplies fresh signing headers and a raw Cookie value. This module
preserves them in memory, but does not generate signatures, validate product
JSON, follow redirects, retry, or persist credentials. Server acceptance of a
captured request does not establish acceptance of newly generated signatures.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from io import BytesIO
from urllib.parse import unquote, urlsplit

ENDPOINT = 'https://affiliate.shopee.vn/api/v3/offer/product'
PRODUCT_ID = r'[1-9][0-9]{0,19}'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_HEADER_VALUE = 16384
MAX_COOKIE_BYTES = 65536
MAX_TOTAL_HEADER_BYTES = 128 * 1024
CHROME_PROFILE = 'chrome152_macos'
ALLOWED_HEADERS = frozenset({
    'accept', 'accept-encoding', 'accept-language', 'affiliate-program-type',
    'priority', 'referer', 'sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform',
    'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site', 'user-agent',
    'x-sap-ri', 'x-sap-sec', 'af-ac-enc-dat', 'af-ac-enc-sz-token', 'x-sz-sdk-version',
})


class TransportError(RuntimeError):
    """Safe-to-display transport error without headers, cookies or response data."""

    def __init__(self, message: str, *, category: str = 'transient') -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(repr=False)


def product_url(target: str | int) -> str:
    """Accept only a positive product ID or its exact fixed-origin GET URL."""
    if type(target) not in (str, int):
        raise ValueError('Invalid product ID or URL')
    value = str(target)
    if re.fullmatch(PRODUCT_ID, value):
        return ENDPOINT + '?item_id=' + value
    if re.fullmatch(re.escape(ENDPOINT) + r'\?item_id=' + PRODUCT_ID, value):
        return value
    raise ValueError('Invalid product ID or URL')


def _printable(value, maximum: int) -> bool:
    return (isinstance(value, str) and len(value) <= maximum
            and all(32 <= ord(char) <= 126 for char in value))


def _request_headers(headers: Mapping[str, str], cookie: str) -> dict[str, str]:
    if not isinstance(headers, Mapping) or len(headers) > len(ALLOWED_HEADERS):
        raise ValueError('Invalid request headers')
    if not _printable(cookie, MAX_COOKIE_BYTES) or not cookie:
        raise ValueError('Invalid raw Cookie value')
    names = tuple(headers)
    if any(not isinstance(name, str) or name.lower() not in ALLOWED_HEADERS for name in names):
        raise ValueError('Unsupported request header')
    if len({name.lower() for name in names}) != len(names):
        raise ValueError('Duplicate request header')
    if any(not _printable(value, MAX_HEADER_VALUE) for value in headers.values()):
        raise ValueError('Invalid request header value')
    if sum(len(name) + len(value) for name, value in headers.items()) + len(cookie) > MAX_TOTAL_HEADER_BYTES:
        raise ValueError('Request headers exceed the size limit')
    # Copy, without changing caller names/order or parsing/reordering the cookie.
    return {**headers, 'Cookie': cookie}


def _load_requests():
    try:
        return import_module('curl_cffi.requests')
    except (ImportError, OSError):
        raise TransportError('curl_cffi is required for direct HTTPS requests', category='input') from None


def validate_proxy_url(proxy_url: str | None) -> str | None:
    """Validate a trusted operator's explicit proxy without exposing credentials."""
    if proxy_url is None:
        return None
    try:
        if not isinstance(proxy_url, str) or not 1 <= len(proxy_url) <= 4096:
            raise ValueError
        if any(not 33 <= ord(char) <= 126 for char in proxy_url + unquote(proxy_url)):
            raise ValueError
        parsed = urlsplit(proxy_url)
        if (parsed.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed.hostname
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError
        return proxy_url
    except (ValueError, TypeError):
        raise ValueError('Invalid operator proxy URL') from None


def request_product(target: str | int, *, headers: Mapping[str, str], cookie: str,
                    proxy_url: str | None = None) -> HttpResponse:
    """Send one GET and return status plus at most eight MiB of response bytes.

    Non-200 statuses are returned for the caller to classify. Automatic headers,
    proxy environment settings, redirects and retries are not enabled. All input
    is validated before loading the optional curl dependency or opening a socket.
    """
    url = product_url(target)
    request_headers = _request_headers(headers, cookie)
    proxy = validate_proxy_url(proxy_url)
    requests = _load_requests()
    buffer, size, too_large = BytesIO(), 0, False

    def collect(chunk: bytes) -> int:
        nonlocal size, too_large
        if not isinstance(chunk, bytes):
            raise TransportError('Invalid response chunk', category='invalid_response')
        if size + len(chunk) > MAX_RESPONSE_BYTES:
            too_large = True
            raise TransportError('Response exceeds the size limit', category='invalid_response')
        buffer.write(chunk)
        size += len(chunk)
        return len(chunk)

    try:
        with requests.Session(trust_env=False) as session:
            proxy_options = {} if proxy is None else {'proxy': proxy}
            response = session.get(url, headers=request_headers, impersonate=CHROME_PROFILE,
                default_headers=False, timeout=20, allow_redirects=False, verify=True,
                content_callback=collect, **proxy_options)
        if too_large:
            raise TransportError('Response exceeds the size limit', category='invalid_response')
        if type(response.status_code) is not int or not 100 <= response.status_code <= 599:
            raise TransportError('Invalid HTTP status', category='invalid_response')
        return HttpResponse(response.status_code, buffer.getvalue())
    except Exception as error:  # noqa: BLE001 - transport exceptions may contain credentials
        message = 'Response exceeds the size limit' if too_large else 'Direct HTTPS request failed'
        category = 'invalid_response' if too_large else getattr(error, 'category', 'transient')
        raise TransportError(message, category=category) from None
    finally:
        buffer.close()
