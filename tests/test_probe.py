"""Offline regression tests for the browser collector; no Shopee requests."""
from types import SimpleNamespace

import pytest

from shopee_affiliate import probe
from shopee_affiliate.parse import BlockedError

ITEM_ID = "26609048170"
DETAIL_URL = f"{probe.BASE}{probe.DETAIL_API_PATH}?item_id={ITEM_ID}"


def valid_payload():
    return {
        "code": 0,
        "data": {
            "item_id": ITEM_ID,
            "batch_item_for_item_card_full": {"itemid": ITEM_ID, "name": "Phone case", "price": "4850000000"},
        },
    }


class FakeResponse:
    def __init__(self, payload=None, *, url=DETAIL_URL, status=200, method="GET", error=None):
        self.url = url
        self.status = status
        self.request = SimpleNamespace(method=method)
        self.payload = valid_payload() if payload is None else payload
        self.error = error

    def json(self):
        if self.error:
            raise self.error
        return self.payload


class FakePage:
    def __init__(self, initial=(), steps=()):
        self.url = "about:blank"
        self.initial = list(initial)
        self.steps = list(steps)
        self.listeners = {}
        self.navigations = []
        self.elapsed = 0.0

    def on(self, name, callback):
        self.listeners[name] = callback

    def remove_listener(self, name, callback):
        assert self.listeners[name] is callback
        del self.listeners[name]

    def goto(self, url, **kwargs):
        self.url = url
        self.navigations.append(url)
        for response in self.initial:
            self.listeners["response"](response)

    def wait_for_timeout(self, milliseconds):
        self.elapsed += milliseconds / 1000
        if self.steps:
            step = self.steps.pop(0)
            if isinstance(step, str):
                self.url = step
            else:
                self.listeners["response"](step)


@pytest.fixture
def page_factory(monkeypatch):
    def create(*args, **kwargs):
        page = FakePage(*args, **kwargs)
        monkeypatch.setattr(probe.time, "monotonic", lambda: page.elapsed)
        return page
    return create


@pytest.mark.parametrize("value", ["", "0", "-1", "1.2", "1e3", " 1", "1 ", "01", "１２", "../../x", "1?x=2", 1, None])
def test_invalid_id_is_rejected_before_navigation(value, page_factory):
    page = page_factory()
    with pytest.raises(ValueError, match="positive.*ID|ID.*positive"):
        probe.fetch_detail_passively(page, value)
    assert page.navigations == []


def test_cli_rejects_invalid_id():
    with pytest.raises(SystemExit) as error:
        probe.build_parser().parse_args(["detail", "--item-id", "1/2"])
    assert error.value.code == 2


def test_valid_detail_is_returned_once_and_listener_removed(page_factory):
    payload = valid_payload()
    page = page_factory([FakeResponse(payload)])
    assert probe.fetch_detail_passively(page, ITEM_ID) == payload
    assert page.navigations == [f"{probe.BASE}/offer/product_offer/{ITEM_ID}"]
    assert page.listeners == {}


@pytest.mark.parametrize("response", [
    FakeResponse(url=f"https://other.example{probe.DETAIL_API_PATH}?item_id={ITEM_ID}"),
    FakeResponse(url=f"http://affiliate.shopee.vn{probe.DETAIL_API_PATH}?item_id={ITEM_ID}"),
    FakeResponse(url=DETAIL_URL, method="POST"),
    FakeResponse(url=DETAIL_URL.replace(ITEM_ID, "123")),
    FakeResponse(url=DETAIL_URL + "&item_id=123"),
    FakeResponse(url=DETAIL_URL.replace("/product?", "/product/list?")),
])
def test_unrelated_response_is_ignored(response, page_factory):
    page = page_factory([response], [FakeResponse()])
    assert probe.fetch_detail_passively(page, ITEM_ID) == valid_payload()
    assert page.elapsed > 0


@pytest.mark.parametrize("payload", [[], "text", {"code": 0}, {"code": 0, "data": None},
    {"code": 0, "data": []}, {"code": 0, "data": {"item_id": ITEM_ID}},
    {"code": 0, "data": {"item_id": ITEM_ID, "batch_item_for_item_card_full": []}},
    {"code": 0, "data": {"item_id": "123", "batch_item_for_item_card_full": {"name": "wrong"}}},
    {"code": 0, "data": {"item_id": ITEM_ID, "batch_item_for_item_card_full": {"itemid": "123", "name": "wrong"}}},
])
def test_malformed_or_wrong_product_is_rejected(payload, page_factory):
    page = page_factory([FakeResponse(payload)])
    with pytest.raises(ValueError):
        probe.fetch_detail_passively(page, ITEM_ID)
    assert page.listeners == {}


def test_non_json_response_has_actionable_error(page_factory):
    page = page_factory([FakeResponse(error=ValueError("bad JSON"))])
    with pytest.raises(ValueError, match="JSON"):
        probe.fetch_detail_passively(page, ITEM_ID)
    assert page.listeners == {}


def test_http_error_is_not_a_success_payload(page_factory):
    page = page_factory([FakeResponse(status=500)])
    with pytest.raises(ValueError, match="HTTP 500"):
        probe.fetch_detail_passively(page, ITEM_ID)


def test_rate_limit_stops_without_retry(page_factory):
    page = page_factory([FakeResponse(status=429)])
    with pytest.raises(BlockedError, match="429"):
        probe.fetch_detail_passively(page, ITEM_ID)
    assert len(page.navigations) == 1


def test_login_redirect_after_rejected_api_can_complete(page_factory):
    page = page_factory([FakeResponse({"is_login": False}, status=401)], [
        "https://shopee.vn/buyer/login?next=affiliate",
        f"{probe.BASE}/offer/product_offer/{ITEM_ID}",
        FakeResponse(),
    ])
    assert probe.fetch_detail_passively(page, ITEM_ID) == valid_payload()
    assert len(page.navigations) == 1
    assert page.listeners == {}


def test_login_timeout_is_bounded(page_factory, monkeypatch):
    monkeypatch.setattr(probe, "LOGIN_TIMEOUT_MS", 1000)
    page = page_factory([], ["https://shopee.vn/buyer/login"])
    with pytest.raises(BlockedError, match="login|Login"):
        probe.fetch_detail_passively(page, ITEM_ID)
    assert page.elapsed <= 2
    assert page.listeners == {}


def test_auth_error_without_redirect_is_bounded(page_factory, monkeypatch):
    monkeypatch.setattr(probe, "RESPONSE_TIMEOUT_MS", 1000)
    page = page_factory([FakeResponse({"is_login": False}, status=403)])
    with pytest.raises(BlockedError, match="403|login|Login"):
        probe.fetch_detail_passively(page, ITEM_ID)
    assert page.elapsed <= 1.5


def test_verification_during_login_stops_and_redacts_query(page_factory):
    page = page_factory([], [
        "https://shopee.vn/buyer/login",
        "https://shopee.vn/verify/traffic?token=private-value",
    ])
    with pytest.raises(BlockedError, match="verification") as error:
        probe.fetch_detail_passively(page, ITEM_ID)
    assert "private-value" not in str(error.value)
    assert page.listeners == {}


def test_verification_payload_stops_without_waiting(page_factory):
    page = page_factory([FakeResponse({"is_login": False, "redirect_to_error_page": True}, status=403)])
    with pytest.raises(BlockedError, match="verification"):
        probe.fetch_detail_passively(page, ITEM_ID)
    assert page.elapsed == 0


def test_no_detail_response_has_clear_timeout(page_factory, monkeypatch):
    monkeypatch.setattr(probe, "RESPONSE_TIMEOUT_MS", 1000)
    page = page_factory()
    with pytest.raises(ValueError, match="no /offer/product call"):
        probe.fetch_detail_passively(page, ITEM_ID)
    assert page.listeners == {}


def test_main_reports_browser_launch_failure(monkeypatch, tmp_path):
    from contextlib import nullcontext

    from playwright.sync_api import Error

    monkeypatch.setattr(probe, "sync_playwright", lambda: nullcontext(object()))

    def cannot_launch(*args):
        raise Error("Chrome profile is already in use")

    monkeypatch.setattr(probe, "open_context", cannot_launch)
    assert probe.main(["--out", str(tmp_path), "detail", "--item-id", ITEM_ID]) == 1


def test_main_reports_output_failure_without_browser(monkeypatch, tmp_path):
    file_path = tmp_path / "existing-file"
    file_path.write_text("data")
    assert probe.main(["--out", str(file_path), "detail", "--item-id", ITEM_ID]) == 1


@pytest.mark.parametrize("status,expected_code", [(200, 0), (429, 3), (500, 1)])
def test_cli_to_json_output_and_browser_cleanup(status, expected_code, page_factory, monkeypatch, tmp_path):
    """Exercise CLI, browser setup, capture, normalization and disk output together."""
    import json
    from contextlib import nullcontext
    from unittest.mock import Mock

    import get_product

    page = page_factory([FakeResponse(status=status)])
    context = SimpleNamespace(pages=[page], close=Mock())
    launch = Mock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(probe, "sync_playwright", lambda: nullcontext(playwright))
    profile = tmp_path / "profile"
    output = tmp_path / "output"

    code = get_product.main([
        ITEM_ID, ITEM_ID, "--profile-dir", str(profile), "--out", str(output),
    ])

    assert code == expected_code
    assert context.close.call_count == 1
    assert launch.call_args.kwargs["channel"] == "chrome"
    assert len(page.navigations) == 1  # duplicate IDs must not repeat network requests
    files = list(output.glob("*/rows.json"))
    if expected_code == 0:
        assert len(files) == 1
        rows = json.loads(files[0].read_text())
        assert len(rows) == 1
        assert rows[0]["item_id"] == ITEM_ID
        assert rows[0]["price_vnd"] == 48500
        raw = json.loads((files[0].parent / f"detail_{ITEM_ID}.json").read_text())
        assert raw == valid_payload()
    else:
        assert not files


def test_list_reuses_page_response_without_second_request(page_factory, tmp_path):
    import json

    payload = {"code": 0, "data": {"list": [valid_payload()["data"]], "total_count": 1}}
    page = page_factory([FakeResponse(payload, url=f"{probe.BASE}{probe.LIST_API_PATH}")])
    args = probe.build_parser().parse_args(["list"])
    rows = probe.run_list(page, args, tmp_path)
    assert rows[0]["price_vnd"] == 48500
    assert len(page.navigations) == 1
    assert json.loads((tmp_path / "list_offset_0.json").read_text()) == payload


def test_pacing_respects_configured_delay(monkeypatch):
    from unittest.mock import Mock

    sleep = Mock()
    monkeypatch.setattr(probe.time, "sleep", sleep)
    probe.polite_sleep(4, 9)
    assert 4 <= sleep.call_args.args[0] <= 9
