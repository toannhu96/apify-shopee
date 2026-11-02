"""Bounded in-memory capture from an explicitly connected local Chrome session."""
import asyncio
import json
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from shopee_affiliate.cookies import parse_cookies
from shopee_affiliate.direct import _validate
from shopee_affiliate.http_transport import ALLOWED_HEADERS, MAX_RESPONSE_BYTES
from shopee_affiliate.session_signer import initialize_session
from shopee_affiliate.signed_client import _normalized_headers
from shopee_contracts import product_id

ORIGIN = 'https://affiliate.shopee.vn'
API_PATH = '/api/v3/offer/product'


class CaptureError(ValueError):
    """Safe, credential-free capture failure."""


@dataclass(frozen=True)
class Capture:
    cookie_source: str = field(repr=False)
    headers: dict = field(repr=False)


def validate_cdp_url(value):
    try:
        url = urlsplit(value)
        valid = (url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}
                 and url.port is not None and 1 <= url.port <= 65535
                 and not url.username and not url.password and url.path in ('', '/')
                 and not url.query and not url.fragment
                 and not any(ord(char) <= 32 for char in value))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('CDP URL must be an explicit loopback HTTP origin with a port')
    return value


def _affiliate_page(url):
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.netloc == 'affiliate.shopee.vn'


def matches_response(response, item_id):
    parsed = urlsplit(response.url)
    return (_affiliate_page(response.url) and parsed.path == API_PATH
            and response.request.method == 'GET'
            and parse_qs(parsed.query).get('item_id') == [item_id])


def prepare_capture(raw, headers, cookies, item_id):
    """Validate accepted payload and preserve the exact request's signing token."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CaptureError('Product response exceeds capture limit')
    try:
        _validate(raw, item_id)
        selected = {name.lower(): value for name, value in headers.items()
                    if name.lower() in ALLOWED_HEADERS}
        selected = _normalized_headers(selected)
        initialize_session(selected)
        scoped = [cookie for cookie in cookies
                  if cookie.get('domain', '').removeprefix('.') in {'shopee.vn', 'affiliate.shopee.vn'}]
        source = json.dumps(parse_cookies(json.dumps(scoped)), separators=(',', ':'))
        return Capture(source, selected)
    except Exception:  # noqa: BLE001 - never expose browser headers or cookies in errors.
        raise CaptureError('Browser session or signing seed is not usable; login or verification may be required') from None


async def _capture_connected(playwright, cdp_url, item_id):
    browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=10_000)
    candidates = [context for context in browser.contexts
                  if any(_affiliate_page(page.url) for page in context.pages)]
    if len(candidates) != 1:
        raise CaptureError('Open the Shopee affiliate account in exactly one connected Chrome context')
    context = candidates[0]
    page = await context.new_page()
    try:
        async with page.expect_response(lambda response: matches_response(response, item_id), timeout=30_000) as pending:
            await page.goto(f'{ORIGIN}/offer/product_offer/{item_id}', wait_until='domcontentloaded', timeout=30_000)
        response = await pending.value
        if response.status != 200:
            raise CaptureError('Browser product request failed; login, verification or rate-limit recovery is required')
        if page.url != f'{ORIGIN}/offer/product_offer/{item_id}':
            raise CaptureError('Browser navigated away from the requested product')
        length = (await response.all_headers()).get('content-length')
        if length and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
            raise CaptureError('Product response exceeds capture limit')
        raw = await response.body()
        headers = await response.request.all_headers()
        cookies = await context.cookies([ORIGIN + API_PATH])
        return prepare_capture(raw, headers, cookies, item_id)
    finally:
        await page.close()
        # The Playwright connection is disconnected by its manager. Never close
        # the attached browser or its user-owned contexts/pages.


async def capture_async(cdp_url, item_id, *, factory=None):
    validate_cdp_url(cdp_url)
    item_id = product_id(item_id)
    if os.environ.get('DEBUG') or os.environ.get('PWDEBUG'):
        raise CaptureError('Disable DEBUG and PWDEBUG before capturing session credentials')
    if factory is None:
        from playwright.async_api import async_playwright
        factory = async_playwright
    try:
        async with factory() as playwright:
            return await asyncio.wait_for(_capture_connected(playwright, cdp_url, item_id), timeout=45)
    except CaptureError:
        raise
    except Exception:  # noqa: BLE001 - driver errors may contain request credentials.
        raise CaptureError('Could not capture Chrome session; check the local CDP connection and complete login or verification in Chrome') from None


def capture_browser(cdp_url, item_id):
    return asyncio.run(capture_async(cdp_url, item_id))
