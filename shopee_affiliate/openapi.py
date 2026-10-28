"""Shopee Affiliate (VN) Open API client: signed GraphQL, no browser or login cookies.

Usage:
    export SHOPEE_AFFILIATE_APP_ID=... SHOPEE_AFFILIATE_SECRET=...
    python -m shopee_affiliate.openapi product --item-id 26609048170
    python -m shopee_affiliate.openapi search --keyword "ốp lưng" --sort-type 5 --limit 20

Get the App ID and Secret from the affiliate portal's Open API page. See docs/shopee-affiliate-api.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("shopee_affiliate.openapi")

ENDPOINT = "https://open-api.affiliate.shopee.vn/graphql"
APP_ID_ENV = "SHOPEE_AFFILIATE_APP_ID"
SECRET_ENV = "SHOPEE_AFFILIATE_SECRET"
REQUEST_TIMEOUT_S = 30
MAX_LIMIT = 50

PRODUCT_OFFER_FIELDS = """
      itemId productName shopId shopName shopType productCatIds
      priceMin priceMax priceDiscountRate sales ratingStar imageUrl
      commissionRate sellerCommissionRate shopeeCommissionRate commission
      productLink offerLink periodStartTime periodEndTime
"""


class OpenApiError(RuntimeError):
    """The Open API returned GraphQL errors or an unusable response."""


def sign(app_id: str, secret: str, timestamp: int, payload: str) -> str:
    """Authorization header value: SHA256 over AppId + Timestamp + Payload + Secret."""
    digest = hashlib.sha256(f"{app_id}{timestamp}{payload}{secret}".encode()).hexdigest()
    return f"SHA256 Credential={app_id}, Timestamp={timestamp}, Signature={digest}"


def _graphql_args(args: dict[str, Any]) -> str:
    parts = []
    for name, value in args.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(f"Unsupported value for {name}: {value!r}")
        literal = json.dumps(value, ensure_ascii=False) if isinstance(value, str) else str(value)
        parts.append(f"{name}: {literal}")
    return ", ".join(parts)


def build_product_offer_query(**args: Any) -> str:
    """productOfferV2 query, e.g. build_product_offer_query(itemId=1, limit=1)."""
    return (
        f"{{ productOfferV2({_graphql_args(args)}) {{\n"
        f"    nodes {{{PRODUCT_OFFER_FIELDS}    }}\n"
        "    pageInfo { page limit hasNextPage }\n"
        "} }"
    )


def check_graphql_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return payload['data'] or raise OpenApiError with the API's error codes."""
    errors = payload.get("errors")
    if errors:
        details = "; ".join(
            f"[{(e.get('extensions') or {}).get('code')}] {e.get('message')}" for e in errors
        )
        raise OpenApiError(f"Open API error: {details}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise OpenApiError("Open API response has no data")
    return data


def _to_float(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    return float(raw)


def _pct(raw: Any) -> float | None:
    """'0.225' (fraction) -> 22.5"""
    value = _to_float(raw)
    return None if value is None else round(value * 100, 4)


def normalize_offer(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten one productOfferV2 node into a scraper row (same names as shopee_affiliate.parse)."""
    item_id = node.get("itemId")
    shop_id = node.get("shopId")
    return {
        "item_id": None if item_id is None else str(item_id),
        "shop_id": None if shop_id is None else str(shop_id),
        "name": node.get("productName"),
        "image": node.get("imageUrl"),
        "price_min_vnd": _to_float(node.get("priceMin")),
        "price_max_vnd": _to_float(node.get("priceMax")),
        "discount_pct": node.get("priceDiscountRate"),
        "sales": node.get("sales"),
        "rating_star": _to_float(node.get("ratingStar")),
        "category_ids": node.get("productCatIds"),
        "shop_name": node.get("shopName"),
        "shop_type": node.get("shopType"),
        "default_commission_rate_pct": _pct(node.get("commissionRate")),
        "seller_commission_rate_pct": _pct(node.get("sellerCommissionRate")),
        "shopee_commission_rate_pct": _pct(node.get("shopeeCommissionRate")),
        "est_commission_vnd": _to_float(node.get("commission")),
        "period_start_time": node.get("periodStartTime"),
        "period_end_time": node.get("periodEndTime"),
        "product_link": node.get("productLink"),
        "affiliate_link": node.get("offerLink"),
    }


class OpenApiClient:
    def __init__(self, app_id: str, secret: str, endpoint: str = ENDPOINT) -> None:
        if not app_id or not secret:
            raise ValueError(f"Set {APP_ID_ENV} and {SECRET_ENV}")
        self._app_id = app_id
        self._secret = secret
        self._endpoint = endpoint

    @classmethod
    def from_env(cls) -> OpenApiClient:
        return cls(os.environ.get(APP_ID_ENV, ""), os.environ.get(SECRET_ENV, ""))

    def query(self, query: str) -> dict[str, Any]:
        # The signature covers the exact body bytes, so serialize once and send that string.
        payload = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":"))
        request = urllib.request.Request(
            self._endpoint,
            data=payload.encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": sign(self._app_id, self._secret, int(time.time()), payload),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise OpenApiError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
        except urllib.error.URLError as exc:
            raise OpenApiError(f"Request failed: {exc.reason}") from exc
        try:
            return check_graphql_payload(json.loads(body))
        except json.JSONDecodeError as exc:
            raise OpenApiError(f"Non-JSON response: {body[:300]}") from exc

    def product_offers(self, **args: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return (raw nodes, pageInfo) for productOfferV2."""
        data = self.query(build_product_offer_query(**args))
        connection = data.get("productOfferV2") or {}
        return connection.get("nodes") or [], connection.get("pageInfo") or {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query the Shopee Affiliate (VN) Open API.")
    parser.add_argument("--out", default="output", help="Directory for raw and normalized JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    product = sub.add_parser("product", help="Look up one product offer by item ID")
    product.add_argument("--item-id", type=int, required=True)
    product.add_argument("--shop-id", type=int, help="Optional; narrows the lookup")

    search = sub.add_parser("search", help="List product offers")
    search.add_argument("--keyword", default="")
    search.add_argument("--shop-id", type=int)
    search.add_argument("--category-id", type=int, help="productCatId")
    search.add_argument("--list-type", type=int, help="Open API listType")
    search.add_argument("--sort-type", type=int, help="Open API sortType")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--limit", type=int, default=20, help=f"1-{MAX_LIMIT}")
    return parser


def query_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "product":
        return {"itemId": args.item_id, "shopId": args.shop_id, "limit": 1}
    return {
        "keyword": args.keyword,
        "shopId": args.shop_id,
        "productCatId": args.category_id,
        "listType": args.list_type,
        "sortType": args.sort_type,
        "page": args.page,
        "limit": args.limit,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    args = build_parser().parse_args(argv)
    if args.command == "search" and not (1 <= args.limit <= MAX_LIMIT and args.page >= 1):
        log.error("--limit must be 1-%d and --page >= 1", MAX_LIMIT)
        return 2
    try:
        client = OpenApiClient.from_env()
    except ValueError as exc:
        log.error("%s (App ID and Secret from the affiliate portal's Open API page)", exc)
        return 2

    try:
        nodes, page_info = client.product_offers(**query_args(args))
    except (OpenApiError, TypeError) as exc:
        log.error("%s", exc)
        return 1

    rows = [normalize_offer(node) for node in nodes]
    out_dir = Path(args.out) / datetime.now().strftime("openapi-%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw.json").write_text(json.dumps(nodes, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        log.info(
            "%s | %s | %s-%s ₫ | comm %s%% (~%s ₫) | sales %s | %s",
            row["item_id"],
            (row["name"] or "")[:50],
            row["price_min_vnd"],
            row["price_max_vnd"],
            row["default_commission_rate_pct"],
            row["est_commission_vnd"],
            row["sales"],
            row["shop_name"],
        )
    log.info("Saved %d rows to %s (pageInfo=%s)", len(rows), out_dir, page_info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
