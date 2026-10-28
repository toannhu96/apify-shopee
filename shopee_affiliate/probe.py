"""Probe the Shopee Affiliate (VN) product offer API from a real, logged-in browser.

Usage:
    python -m shopee_affiliate.probe list --pages 2
    python -m shopee_affiliate.probe list --list-type 1 --sort-type 5 --keyword "ốp lưng"
    python -m shopee_affiliate.probe detail --item-id 26609048170

The first run opens a browser window; log in to Shopee Affiliate there yourself. The session is kept in
--profile-dir for later runs. See docs/shopee-affiliate-api.md for the API and anti-bot rules.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from shopee_affiliate.parse import (
    BlockedError,
    check_api_payload,
    normalize_detail,
    normalize_list_item,
)

log = logging.getLogger("shopee_affiliate.probe")

BASE = "https://affiliate.shopee.vn"
LIST_PAGE_URL = f"{BASE}/offer/product_offer"
LIST_API_PATH = "/api/v3/offer/product/list"
DETAIL_API_PATH = "/api/v3/offer/product"
BLOCK_MARKERS = ("/verify/traffic", "/verify/captcha", "/buyer/login")

PAGE_LIMIT = 20
MAX_RESULTS = 500  # observed total_count cap per list query
DEFAULT_LIST_TYPE = 0
DEFAULT_SORT_TYPE = 1
LOGIN_TIMEOUT_MS = 10 * 60 * 1000
RESPONSE_TIMEOUT_MS = 30_000
POLL_MS = 500

# Runs inside the page so the anti-bot SDK's XMLHttpRequest hook signs the request.
IN_PAGE_XHR = """
(url) => new Promise((resolve) => {
  const x = new XMLHttpRequest();
  x.open('GET', url);
  x.setRequestHeader('Affiliate-Program-Type', '1');
  x.onload = () => resolve({ status: x.status, text: x.responseText });
  x.onerror = () => resolve({ status: 0, text: '' });
  x.send();
})
"""


def raise_if_blocked(page: Page) -> None:
    path = urlparse(page.url).path
    if any(marker in path for marker in BLOCK_MARKERS[:2]):
        raise BlockedError("Shopee requires traffic verification; finish it manually before retrying")
    if "/buyer/login" in path:
        raise BlockedError("Shopee login is required")


def validate_item_id(value: str) -> str:
    """Require a canonical positive ASCII ID before using it in a URL or filename."""
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("Product ID must be a positive ASCII decimal ID without leading zeros")
    return value


def is_affiliate_response(response: Response, path: str) -> bool:
    parsed = urlparse(response.url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "affiliate.shopee.vn"
        and parsed.path == path
        and response.request.method == "GET"
    )


def polite_sleep(delay_min: float, delay_max: float) -> None:
    seconds = random.uniform(delay_min, delay_max)
    log.info("Waiting %.1fs before the next request", seconds)
    time.sleep(seconds)


def wait_out_login(page: Page, deadline: float | None = None) -> None:
    """If the app bounced to the login page, let the user log in by hand, then continue."""
    if "/buyer/login" not in urlparse(page.url).path:
        return
    log.warning("Not logged in. Log in to Shopee Affiliate in the opened browser window (waiting up to 10 min).")
    deadline = deadline if deadline is not None else time.monotonic() + LOGIN_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        parsed = urlparse(page.url)
        if any(marker in parsed.path for marker in BLOCK_MARKERS[:2]):
            raise_if_blocked(page)
        if parsed.scheme == "https" and parsed.netloc == "affiliate.shopee.vn" and "/buyer/login" not in parsed.path:
            return
        page.wait_for_timeout(POLL_MS)
    raise BlockedError("Shopee login did not complete within the allowed time")


def read_response(response: Response) -> dict[str, Any] | None:
    """None means an auth failure whose delayed login redirect may still arrive."""
    if response.status == 429:
        raise BlockedError("HTTP 429: Shopee rate limit reached; stop and wait before retrying")
    if response.status not in (200, 401, 403):
        raise ValueError(f"HTTP {response.status} from the product API")
    try:
        payload = response.json()
    except (ValueError, PlaywrightError) as exc:
        raise ValueError("Product API returned an unreadable JSON response") from exc
    if not isinstance(payload, dict):
        raise ValueError("Product API response must be a JSON object")  # noqa: TRY004 - invalid remote payload
    if payload.get("redirect_to_error_page") is True:
        raise BlockedError("Shopee requires traffic verification; stop before retrying")
    if response.status in (401, 403) or payload.get("is_login") is False:
        return None
    return payload


def load_and_capture(page: Page, url: str, matches: Callable[[Response], bool]) -> dict[str, Any] | None:
    """Open url once and return the JSON of the page's own first response that matches (no extra request).

    The affiliate app redirects to login from JS after load, so login is checked while polling.
    """
    captured: list[Response] = []

    def on_response(response: Response) -> None:
        if matches(response):
            captured.append(response)

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded")
        deadline = time.monotonic() + RESPONSE_TIMEOUT_MS / 1000
        login_deadline = None
        pending_auth = False
        while time.monotonic() < deadline:
            if "/buyer/login" in urlparse(page.url).path:
                login_deadline = login_deadline or time.monotonic() + LOGIN_TIMEOUT_MS / 1000
                wait_out_login(page, login_deadline)
                deadline = time.monotonic() + RESPONSE_TIMEOUT_MS / 1000
            raise_if_blocked(page)
            while captured:
                payload = read_response(captured.pop(0))
                if payload is not None:
                    return payload
                pending_auth = True
            page.wait_for_timeout(POLL_MS)
        raise_if_blocked(page)
        if pending_auth:
            raise BlockedError("Shopee rejected authentication (401/403); log in before retrying")
        return None
    finally:
        page.remove_listener("response", on_response)


def in_page_get(page: Page, path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{path}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    log.info("GET %s", url)
    result = page.evaluate(IN_PAGE_XHR, url)
    raise_if_blocked(page)
    if result["status"] != 200:
        raise ValueError(f"HTTP {result['status']} for {url}")
    return json.loads(result["text"])


def run_list(page: Page, args: argparse.Namespace, out_dir: Path) -> list[dict[str, Any]]:
    first = load_and_capture(page, LIST_PAGE_URL, lambda r: is_affiliate_response(r, LIST_API_PATH))
    if first is None:
        log.warning("Page did not make its own list call in time; will fetch page 0 in-page instead")
    is_default_query = (
        args.list_type == DEFAULT_LIST_TYPE and args.sort_type == DEFAULT_SORT_TYPE and not args.keyword
    )
    rows: list[dict[str, Any]] = []
    max_pages = min(args.pages, MAX_RESULTS // PAGE_LIMIT)

    for page_index in range(max_pages):
        offset = page_index * PAGE_LIMIT
        if page_index == 0 and is_default_query and first is not None:
            log.info("Using the page's own list response for offset 0")
            payload = first
        else:
            if page_index > 0 or first is not None:
                polite_sleep(args.delay_min, args.delay_max)
            payload = in_page_get(page, LIST_API_PATH, list_params(args, offset))

        write_json(out_dir / f"list_offset_{offset}.json", payload)
        data = check_api_payload(payload)
        items = data.get("list") or []
        rows = [*rows, *(normalize_list_item(item) for item in items)]
        log.info("offset=%d got %d items (total_count=%s)", offset, len(items), data.get("total_count"))
        if not items or offset + PAGE_LIMIT >= (data.get("total_count") or 0):
            break
    return rows


def list_params(args: argparse.Namespace, offset: int) -> dict[str, Any]:
    return {
        "list_type": args.list_type,
        "sort_type": args.sort_type,
        "page_offset": offset,
        "page_limit": PAGE_LIMIT,
        "client_type": 1,
        "keyword": args.keyword,
        "match_type": args.match_type,
        "match_id": args.match_id,
    }


def fetch_detail_passively(page: Page, item_id: str) -> dict[str, Any]:
    """Open the detail page once and read only the page's own /offer/product response."""
    item_id = validate_item_id(item_id)

    def is_detail(response: Response) -> bool:
        parsed = urlparse(response.url)
        return is_affiliate_response(response, DETAIL_API_PATH) and parse_qs(parsed.query).get("item_id") == [item_id]

    payload = load_and_capture(page, f"{BASE}/offer/product_offer/{item_id}", is_detail)
    if payload is None:
        raise ValueError(f"Detail page for item {item_id} made no /offer/product call within the timeout")
    validate_detail_payload(payload, item_id)
    return payload


def validate_detail_payload(payload: dict[str, Any], item_id: str) -> None:
    """Reject empty or unrelated products before reporting a successful lookup."""
    data = check_api_payload(payload)
    if not isinstance(data, dict) or str(data.get("item_id")) != item_id:
        raise ValueError(f"Product API returned missing or mismatched item ID for {item_id}")
    card = data.get("batch_item_for_item_card_full")
    if not isinstance(card, dict) or not card:
        raise ValueError(f"Product API returned no product card for item {item_id}")
    if card.get("itemid") is not None and str(card["itemid"]) != item_id:
        raise ValueError(f"Product card item ID does not match requested item {item_id}")


def run_detail(page: Page, args: argparse.Namespace, out_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item_id in enumerate(dict.fromkeys(args.item_id)):  # de-dupe, keep order
        if index > 0:
            polite_sleep(args.delay_min, args.delay_max)
        log.info("Opening detail page for item %s", item_id)
        payload = fetch_detail_passively(page, item_id)
        write_json(out_dir / f"detail_{item_id}.json", payload)
        rows = [*rows, normalize_detail(check_api_payload(payload))]
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def log_summary(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        log.info(
            "%s | %s | %s ₫ | comm %s%% | sold %s | %s",
            row.get("item_id"),
            (row.get("name") or "")[:50],
            row.get("price_vnd"),
            row.get("default_commission_rate_pct"),
            row.get("historical_sold"),
            row.get("shop_name"),
        )


def open_context(playwright: Any, args: argparse.Namespace) -> BrowserContext:
    Path(args.profile_dir).mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=args.profile_dir,
        channel=args.channel or None,
        headless=args.headless,
        locale="vi-VN",
        viewport={"width": 1366, "height": 860},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe the Shopee Affiliate (VN) product offer API.")
    parser.add_argument("--profile-dir", default=".auth/shopee-profile", help="Persistent browser profile")
    parser.add_argument("--out", default="output", help="Directory for raw and normalized JSON")
    parser.add_argument("--channel", default="chrome", help="Browser channel: chrome, msedge, or '' for bundled")
    parser.add_argument("--headless", action="store_true", help="Run headless (needs an existing login)")
    parser.add_argument("--delay-min", type=float, default=4.0, help="Min seconds between API calls")
    parser.add_argument("--delay-max", type=float, default=9.0, help="Max seconds between API calls")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="Fetch product offer list pages")
    list_cmd.add_argument("--pages", type=int, default=1, help="Pages of 20 items (max 25)")
    list_cmd.add_argument("--list-type", type=int, default=DEFAULT_LIST_TYPE, help="0 all, 1 top comm, 2 top perf, 3 category...")
    list_cmd.add_argument("--sort-type", type=int, default=DEFAULT_SORT_TYPE, help="1 relevance, 2 sold, 3 price desc, 4 price asc, 5 comm")
    list_cmd.add_argument("--keyword", default="", help="Search keyword")
    list_cmd.add_argument("--match-type", type=int, help="1 shop, 2 category, 3 collection")
    list_cmd.add_argument("--match-id", help="ID for --match-type")

    detail_cmd = sub.add_parser("detail", help="Read product detail from each item's own page load")
    detail_cmd.add_argument("--item-id", type=validate_item_id, action="append", required=True, help="Repeat for several items")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    args = build_parser().parse_args(argv)
    if args.delay_min < 0 or args.delay_max < args.delay_min:
        log.error("--delay-max must be >= --delay-min >= 0")
        return 2

    try:
        out_dir = Path(args.out) / datetime.now().strftime("run-%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = open_context(playwright, args)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                runner = run_list if args.command == "list" else run_detail
                rows = runner(page, args, out_dir)
            finally:
                context.close()
        write_json(out_dir / "rows.json", rows)
    except BlockedError as exc:
        log.error("%s. Stop, solve any check by hand in a normal browser, and wait before retrying.", exc)
        return 3
    except (ValueError, PlaywrightError, OSError) as exc:
        log.error("Request failed: %s", exc)
        return 1
    log_summary(rows)
    log.info("Saved %d rows and raw responses to %s", len(rows), out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
