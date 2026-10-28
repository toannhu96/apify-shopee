import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shopee_affiliate import internal
from shopee_affiliate.parse import BlockedError

ITEM_ID = "26609048170"
PAYLOAD = {"code": 0, "data": {"item_id": ITEM_ID,
    "batch_item_for_item_card_full": {"itemid": ITEM_ID, "name": "Case", "price": "4850000000"}}}


def page_for(result=None, url="https://affiliate.shopee.vn/offer/product_offer"):
    return SimpleNamespace(url=url, evaluate=Mock(return_value=result or {
        "status": 200, "text": json.dumps(PAYLOAD), "url": internal.DETAIL_URL + ITEM_ID,
    } if result is None else {"url": internal.DETAIL_URL + ITEM_ID, **result}), on=Mock(), remove_listener=Mock())


def test_product_request_uses_fixed_same_origin_url():
    page = page_for()
    assert internal.fetch_product(page, ITEM_ID) == PAYLOAD
    assert page.evaluate.call_args.args[1] == internal.DETAIL_URL + ITEM_ID


@pytest.mark.parametrize("url", ["https://evil.test", "http://affiliate.shopee.vn", "https://shopee.vn/buyer/login",
                                 "https://shopee.vn/verify/traffic?private=secret"])
def test_origin_guard_prevents_requests(url):
    page = page_for(url=url)
    with pytest.raises(BlockedError) as exc:
        internal.fetch_product(page, ITEM_ID)
    assert "secret" not in str(exc.value)
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("result,exception", [
    ({"status": 401}, BlockedError), ({"status": 403}, BlockedError), ({"status": 429}, BlockedError),
    ({"status": 500}, ValueError), ({"status": 0, "error": "timeout"}, ValueError),
    ({"status": 200, "text": "private"}, ValueError),
    ({"status": 200, "text": "[]"}, ValueError),
    ({"status": 200, "text": '{"code":0}'}, ValueError),
    ({"status": 200, "text": '{"is_login":false}'}, BlockedError),
    ({"status": 200, "text": '{"redirect_to_error_page":true}'}, BlockedError),
    ({"status": 200, "text": '{"error":90309999}'}, BlockedError),
    ({"status": 200, "text": json.dumps(PAYLOAD), "url": "https://evil.test/private"}, BlockedError),
])
def test_failed_request_does_not_report_success_or_secrets(result, exception):
    with pytest.raises(exception) as exc:
        internal.fetch_product(page_for(result), ITEM_ID)
    assert "private" not in str(exc.value)


def test_bad_item_id_fails_before_request():
    page = page_for()
    with pytest.raises(ValueError):
        internal.fetch_product(page, "../private")
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("item_ids", [[ITEM_ID, "../bad"], []])
def test_collect_validates_all_ids_before_bootstrap(item_ids, monkeypatch):
    bootstrap = Mock()
    monkeypatch.setattr(internal, "bootstrap", bootstrap)
    monkeypatch.setattr(internal.probe, "polite_sleep", Mock())
    with pytest.raises(ValueError):
        internal.collect(page_for(), item_ids)
    bootstrap.assert_not_called()


def test_signing_summary_excludes_header_values():
    headers = {"x-sap-ri": "private", "Cookie": "private", "x-sap-sec": "private-long"}
    summary = internal.signing_summary(headers)
    assert summary["x-sap-ri"] == 7
    assert summary["x-sap-sec"] == 12
    assert "private" not in json.dumps(summary)
    assert "Cookie" not in summary


@pytest.fixture
def fake_runtime(monkeypatch):
    page = page_for()
    context = SimpleNamespace(pages=[page], new_page=Mock(return_value=page), add_cookies=Mock(), close=Mock())
    browser = SimpleNamespace(new_context=Mock(return_value=context), close=Mock())
    chromium = SimpleNamespace(launch=Mock(return_value=browser), launch_persistent_context=Mock(return_value=context))
    monkeypatch.setattr(internal, "sync_playwright", lambda: nullcontext(SimpleNamespace(chromium=chromium)))
    monkeypatch.setattr(internal, "bootstrap", Mock())
    monkeypatch.setattr(internal.probe, "polite_sleep", Mock())
    monkeypatch.delenv("SHOPEE_COOKIE", raising=False)
    return page, context, browser, chromium


def test_cookie_mode_is_ephemeral_and_outputs_json(fake_runtime, monkeypatch, capsys):
    page, context, browser, chromium = fake_runtime
    monkeypatch.setenv("SHOPEE_COOKIE", "a=private")
    assert internal.main([ITEM_ID, ITEM_ID]) == 0
    assert context.add_cookies.call_count == 1
    assert chromium.launch.call_count == 1
    chromium.launch_persistent_context.assert_not_called()
    assert browser.close.call_count == 1
    assert context.close.call_count == 1
    assert page.evaluate.call_count == 1
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
    assert json.loads(output.out)["products"][0]["price_vnd"] == 48500


def test_profile_mode_and_output_file(fake_runtime, tmp_path, capsys):
    _, context, browser, chromium = fake_runtime
    output = tmp_path / "result.json"
    assert internal.main([ITEM_ID, "--profile-dir", str(tmp_path / "profile"), "--out", str(output)]) == 0
    assert chromium.launch_persistent_context.call_count == 1
    browser.close.assert_not_called()
    assert context.close.call_count == 1
    assert json.loads(output.read_text())["products"][0]["item_id"] == ITEM_ID
    assert json.loads(capsys.readouterr().out)["products"][0]["item_id"] == ITEM_ID


def test_request_block_closes_context(fake_runtime, capsys):
    page, context, _, _ = fake_runtime
    page.evaluate.return_value = {"status": 429}
    assert internal.main([ITEM_ID]) == 3
    assert context.close.call_count == 1
    assert capsys.readouterr().out == ""


def test_missing_explicit_cookie_env_is_bad_arguments(fake_runtime):
    assert internal.main([ITEM_ID, "--cookie-env", "UNSET_SHOPEE_TEST_COOKIE"]) == 2
    fake_runtime[3].launch.assert_not_called()


def test_bootstrap_requires_success_and_same_origin(monkeypatch):
    monkeypatch.setattr(internal.probe, "load_and_capture", Mock(return_value=None))
    with pytest.raises(ValueError):
        internal.bootstrap(page_for())
    monkeypatch.setattr(internal.probe, "load_and_capture", Mock(return_value={"code":0,"data":{"list":[]}}))
    internal.bootstrap(page_for())


def test_browser_exception_does_not_log_cookies(fake_runtime, caplog):
    from playwright.sync_api import Error

    fake_runtime[3].launch_persistent_context.side_effect = Error("Cookie: private-secret")
    assert internal.main([ITEM_ID]) == 1
    assert "private-secret" not in caplog.text


def test_explicit_cookie_file_and_unreadable_file(fake_runtime, tmp_path, caplog):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("a=private-secret")
    assert internal.main([ITEM_ID, "--cookie-file", str(cookie_file)]) == 0
    assert fake_runtime[1].add_cookies.call_args.args[0][0]["value"] == "private-secret"
    assert internal.main([ITEM_ID, "--cookie-file", str(tmp_path / "absent")]) == 2
    assert "private-secret" not in caplog.text


def test_cookie_and_profile_cannot_be_combined(fake_runtime, monkeypatch):
    monkeypatch.setenv("SHOPEE_COOKIE", "a=private")
    assert internal.main([ITEM_ID, "--profile-dir", "unused-profile"]) == 2
    fake_runtime[3].launch.assert_not_called()
    fake_runtime[3].launch_persistent_context.assert_not_called()


def test_signing_diagnostic_logs_only_lengths_and_removes_listener(fake_runtime, caplog):
    page = fake_runtime[0]
    response = SimpleNamespace(
        url=internal.DETAIL_URL + ITEM_ID,
        request=SimpleNamespace(method="GET", all_headers=lambda: {"x-sap-ri": "private-secret"}),
    )

    def evaluate(*args):
        page.on.call_args.args[1](response)
        return {"status":200,"text":json.dumps(PAYLOAD),"url":internal.DETAIL_URL + ITEM_ID}

    page.evaluate.side_effect = evaluate
    with caplog.at_level("INFO"):
        assert internal.main([ITEM_ID, "--inspect-signing"]) == 0
    assert "private-secret" not in caplog.text
    assert "Signing header lengths" in caplog.text
    page.remove_listener.assert_called_once()
