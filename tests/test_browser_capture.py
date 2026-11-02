import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shopee_service import browser_capture as module


@pytest.fixture(autouse=True)
def fixture_clock(monkeypatch):
    from shopee_affiliate import session_signer
    monkeypatch.setattr(session_signer.time, 'time_ns', lambda: 1800000000000 * 1_000_000)

ITEM = '26609048170'
PAGE_URL = f'https://affiliate.shopee.vn/offer/product_offer/{ITEM}'
PAYLOAD = {'code': 0, 'data': {'item_id': ITEM,
           'batch_item_for_item_card_full': {'itemid': ITEM, 'name': 'Product'}}}


def headers():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    return {**fixture['before_headers'], 'af-ac-enc-sz-token': 'accepted-request-token',
            'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21',
            'cookie': 'secret-cookie', 'authorization': 'not-exported'}


def cookies():
    return [{'domain': '.shopee.vn', 'name': 'session', 'value': 'secret-cookie', 'path': '/'},
            {'domain': '.shopee.vn', 'name': 'shopee_webUnique_ccd', 'value': 'newer-cookie-token'},
            {'domain': '.other.test', 'name': 'unrelated', 'value': 'not-exported'}]


def test_prepare_preserves_accepted_token_and_filters_secrets():
    result = module.prepare_capture(json.dumps(PAYLOAD).encode(), headers(), cookies(), ITEM)
    assert result.headers['af-ac-enc-sz-token'] == 'accepted-request-token'
    assert 'cookie' not in result.headers
    assert 'authorization' not in result.headers
    assert 'not-exported' not in result.cookie_source
    assert len(json.loads(result.cookie_source)) == 2


@pytest.mark.parametrize('payload', [
    {'code': 90309999}, {'code': 0, 'is_login': False},
    {'code': 0, 'data': {'item_id': '999', 'batch_item_for_item_card_full': {'name': 'wrong'}}},
    {'code': 1}, {'code': 0, 'data': {'item_id': ITEM}}])
def test_rejected_payload_never_becomes_seed(payload):
    with pytest.raises(module.CaptureError):
        module.prepare_capture(json.dumps(payload).encode(), headers(), cookies(), ITEM)


def test_invalid_signing_seed_and_empty_cookies():
    for seed, jar in [({}, cookies()), (headers(), []), ({**headers(), 'x-sap-sec': 'invalid'}, cookies())]:
        with pytest.raises(module.CaptureError):
            module.prepare_capture(json.dumps(PAYLOAD).encode(), seed, jar, ITEM)


class Pending:
    def __init__(self, response):
        self.response = response
    async def __aenter__(self):
        self.value = asyncio.create_task(self.resolve())
        return self
    async def resolve(self):
        return self.response
    async def __aexit__(self, *args):
        return False


def connected(status=200):
    request = SimpleNamespace(method='GET', all_headers=AsyncMock(return_value=headers()))
    response = SimpleNamespace(url=f'https://affiliate.shopee.vn/api/v3/offer/product?item_id={ITEM}',
                               status=status, request=request, all_headers=AsyncMock(return_value={}),
                               body=AsyncMock(return_value=json.dumps(PAYLOAD).encode()))
    page = SimpleNamespace(url=PAGE_URL, goto=AsyncMock(), close=AsyncMock())
    def expect(predicate, **kwargs):
        assert predicate(response)
        return Pending(response)
    page.expect_response = expect
    existing = SimpleNamespace(url=PAGE_URL, close=AsyncMock())
    context = SimpleNamespace(pages=[existing], new_page=AsyncMock(return_value=page),
                              cookies=AsyncMock(return_value=cookies()), close=AsyncMock())
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
    return playwright, browser, context, page, response


def test_capture_closes_only_owned_page():
    playwright, browser, context, page, _ = connected()
    result = asyncio.run(module._capture_connected(playwright, 'http://localhost:9222', ITEM))
    assert result.headers['af-ac-enc-sz-token'] == 'accepted-request-token'
    page.close.assert_awaited_once()
    browser.close.assert_not_called()
    context.close.assert_not_called()
    context.pages[0].close.assert_not_called()


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_http_failure_does_not_transfer_and_closes_owned_page(status):
    playwright, browser, _context, page, response = connected(status)
    with pytest.raises(module.CaptureError):
        asyncio.run(module._capture_connected(playwright, 'http://localhost:9222', ITEM))
    page.close.assert_awaited_once()
    response.body.assert_not_called()
    browser.close.assert_not_called()


def test_no_context_or_ambiguous_context_fails_before_creation():
    for count in (0, 2):
        playwright, browser, context, _page, _ = connected()
        browser.contexts = [context] * count
        with pytest.raises(module.CaptureError):
            asyncio.run(module._capture_connected(playwright, 'http://localhost:9222', ITEM))
        context.new_page.assert_not_called()


class Manager:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *args):
        return False


def test_public_capture_disconnects_without_closing_browser():
    playwright, browser, _context, _page, _ = connected()
    result = asyncio.run(module.capture_async('http://localhost:9222', ITEM, factory=lambda: Manager(playwright)))
    assert result.headers
    browser.close.assert_not_called()


def test_capture_connection_exception_is_redacted():
    playwright, _, _, _, _ = connected()
    playwright.chromium.connect_over_cdp.side_effect = RuntimeError('secret')
    with pytest.raises(module.CaptureError) as error:
        asyncio.run(module.capture_async('http://localhost:9222', ITEM, factory=lambda: Manager(playwright)))
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('mutation', ['redirect', 'length', 'body'])
def test_unusable_response_cleanup(mutation):
    playwright, browser, _context, page, response = connected()
    if mutation == 'redirect':
        page.url = 'https://affiliate.shopee.vn/login'
    elif mutation == 'length':
        response.all_headers.return_value = {'content-length': str(module.MAX_RESPONSE_BYTES + 1)}
    else:
        response.body.return_value = b'x' * (module.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(module.CaptureError):
        asyncio.run(module._capture_connected(playwright, 'http://localhost:9222', ITEM))
    page.close.assert_awaited_once()
    browser.close.assert_not_called()


@pytest.mark.parametrize('name', ['DEBUG', 'PWDEBUG'])
def test_debug_logging_rejected_before_driver_start(monkeypatch, name):
    monkeypatch.setenv(name, 'pw:protocol')
    with pytest.raises(module.CaptureError, match='Disable DEBUG and PWDEBUG'):
        asyncio.run(module.capture_async('http://localhost:9222', ITEM,
                                        factory=lambda: pytest.fail('driver started')))
