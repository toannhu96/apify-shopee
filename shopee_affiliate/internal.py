"""Internal Shopee API client using cookies and the website's browser signer.

    python -m shopee_affiliate.internal 26609048170
    python -m shopee_affiliate.internal 26609048170 --cookie-file .auth/cookies.json

Chrome loads the site once, then makes product GETs through its normal signed
XMLHttpRequest. This is an experimental browser-assisted client, not a standalone
Python signer. Fresh headers do not guarantee acceptance; Shopee verification
error 90309999 stops the run without retrying or falling back.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from shopee_affiliate import probe
from shopee_affiliate.cookies import parse_cookies
from shopee_affiliate.parse import BlockedError, normalize_detail

log = logging.getLogger(__name__)
DETAIL_URL = probe.BASE + probe.DETAIL_API_PATH + "?item_id="
SIGNING_HEADERS = ("x-sap-ri", "x-sap-sec", "af-ac-enc-sz-token", "af-ac-enc-dat", "x-sz-sdk-version")

# The website SDK sees and signs this ordinary XHR. Nothing replaces its hooks.
SIGNED_XHR = """
(url) => new Promise((resolve) => {
  const x = new XMLHttpRequest();
  let done = false;
  const finish = (error) => {
    if (done) return;
    done = true;
    clearTimeout(watchdog);
    resolve({status:x.status, text:x.responseText, url:x.responseURL, error});
  };
  const watchdog = setTimeout(() => { finish('timeout'); x.abort(); }, 31000);
  try {
    x.open('GET', url);
    x.timeout = 30000;
    x.withCredentials = true;
    x.setRequestHeader('Affiliate-Program-Type', '1');
    x.onload = () => finish('');
    x.onerror = () => finish('network');
    x.ontimeout = () => finish('timeout');
    x.onabort = () => finish('aborted');
    x.send();
  } catch (_) { finish('request'); }
})
"""


def require_origin(page: Any) -> None:
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.netloc != "affiliate.shopee.vn":
        raise BlockedError("Shopee session is no longer on the authenticated affiliate origin")
    probe.raise_if_blocked(page)


def _success(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("API response must be a JSON object")  # noqa: TRY004 - malformed external data
    if (payload.get("is_login") is False or payload.get("redirect_to_error_page") is True
            or payload.get("error") in (90309999, "90309999")):
        raise BlockedError("Shopee requires login or traffic verification; stop before retrying")
    if payload.get("code") != 0 or "error" in payload or not isinstance(payload.get("data"), dict):
        raise ValueError("Shopee API returned an unsuccessful or malformed payload")
    return payload["data"]


def bootstrap(page: Any) -> None:
    payload = probe.load_and_capture(page, probe.LIST_PAGE_URL,
                                    lambda r: probe.is_affiliate_response(r, probe.LIST_API_PATH))
    require_origin(page)
    data = _success(payload)
    if not isinstance(data.get("list"), list):
        raise ValueError("Shopee list response is missing its product list")  # noqa: TRY004 - malformed external data


def fetch_product(page: Any, item_id: str) -> dict[str, Any]:
    item_id = probe.validate_item_id(item_id)
    require_origin(page)
    result = page.evaluate(SIGNED_XHR, DETAIL_URL + item_id)
    require_origin(page)
    if not isinstance(result, dict):
        raise ValueError("Browser returned an invalid API result")  # noqa: TRY004 - malformed external data
    if result.get("status") in (401, 403, 429):
        raise BlockedError("Shopee rejected the session or rate-limited the request; stop before retrying")
    if result.get("status") != 200 or result.get("error"):
        raise ValueError("Product request failed or timed out")
    if result.get("url") != DETAIL_URL + item_id:
        raise BlockedError("Product request was redirected; stop before retrying")
    try:
        payload = json.loads(result.get("text", ""))
    except (ValueError, TypeError):
        raise ValueError("Shopee returned an unreadable JSON response") from None
    _success(payload)
    probe.validate_detail_payload(payload, item_id)
    return payload


def signing_summary(headers: dict[str, str]) -> dict[str, int]:
    lowered = {key.lower(): value for key, value in headers.items()}
    return {name: len(lowered.get(name, "")) for name in SIGNING_HEADERS}


def _observe_signing(page: Any, item_id: str) -> Any:
    def observe(response: Any) -> None:
        if (probe.is_affiliate_response(response, probe.DETAIL_API_PATH)
                and parse_qs(urlparse(response.url).query).get("item_id") == [item_id]):
            try:
                log.info("Signing header lengths: %s", signing_summary(response.request.all_headers()))
            except PlaywrightError:
                log.info("Signing header metadata unavailable")
    page.on("response", observe)
    return observe


def collect(page: Any, item_ids: list[str], inspect_signing: bool = False) -> dict[str, Any]:
    validated_ids = tuple(dict.fromkeys(probe.validate_item_id(item_id) for item_id in item_ids))
    if not validated_ids:
        raise ValueError("At least one product ID is required")
    bootstrap(page)
    products = []
    for item_id in validated_ids:
        probe.polite_sleep(4, 9)
        observer = _observe_signing(page, item_id) if inspect_signing else None
        try:
            payload = fetch_product(page, item_id)
            products = [*products, normalize_detail(payload["data"])]
        finally:
            if observer is not None:
                page.remove_listener("response", observer)
    return {"transport": "browser-signed-internal-api", "products": products}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("item_ids", nargs="+", type=probe.validate_item_id)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--cookie-file", help="Cookie header text or JSON export; kept out of logs")
    source.add_argument("--cookie-env", nargs="?", const="SHOPEE_COOKIE", help="Environment variable (default SHOPEE_COOKIE)")
    parser.add_argument("--profile-dir", help="Dedicated persistent Chrome profile; incompatible with supplied cookies")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--inspect-signing", action="store_true", help="Log header presence/length only")
    parser.add_argument("--out", help="Also save the JSON result to this file")
    return parser


def _input_cookies(args: argparse.Namespace) -> list[dict[str, Any]] | None:
    if args.cookie_file:
        try:
            source = Path(args.cookie_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ValueError("Cannot read the cookie input file") from None
    else:
        source = os.environ.get(args.cookie_env or "SHOPEE_COOKIE")
        if args.cookie_env and not source:
            raise ValueError("The requested cookie environment variable is not set")
    if source is None:
        return None
    if args.profile_dir:
        raise ValueError("Use either supplied cookies or a persistent profile, not both")
    return parse_cookies(source)


def run(args: argparse.Namespace, cookies: list[dict[str, Any]] | None) -> dict[str, Any]:
    with ExitStack() as cleanup:
        playwright = cleanup.enter_context(sync_playwright())
        options = {"channel": args.channel or None, "headless": args.headless}
        if cookies is not None:
            browser = playwright.chromium.launch(**options)
            cleanup.callback(browser.close)
            context = browser.new_context(locale="vi-VN")
            cleanup.callback(context.close)
            context.add_cookies(cookies)
        else:
            profile = args.profile_dir or ".auth/shopee-profile"
            Path(profile).mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(profile, locale="vi-VN", **options)
            cleanup.callback(context.close)
        page = context.pages[0] if context.pages else context.new_page()
        return collect(page, args.item_ids, args.inspect_signing)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        cookies = _input_cookies(args)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    try:
        result = run(args, cookies)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    except BlockedError:
        log.error("Shopee requires login/verification or has rate-limited the session. Stop and retry later.")
        return 3
    except (ValueError, TypeError, AttributeError, PlaywrightError, OSError):
        # Third-party exception messages can contain cookie values or request headers.
        log.error("Request failed. Check Chrome, the cookie/profile input, API response and output path.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
