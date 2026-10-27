"""Pure helpers for Shopee Affiliate API payloads. See docs/shopee-affiliate-api.md for units."""

from __future__ import annotations

import re
from typing import Any

PRICE_SCALE = 100_000  # prices and caps are VND x 100,000
RATE_SCALE = 1_000  # numeric rates are percent x 1,000
NO_END_TIME = 32_503_651_199  # year-3000 sentinel for "no end date"


class BlockedError(RuntimeError):
    """Shopee redirected the session to a login or traffic-verification page."""


def parse_rate_text(text: str | None) -> float | None:
    """'22,5%' -> 22.5"""
    if not text:
        return None
    return float(text.strip().rstrip("%").replace(",", "."))


def parse_vnd_text(text: str | None) -> int | None:
    """'₫10.913' -> 10913"""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def vnd_from_scaled(raw: str | int | None) -> float | None:
    """'4850000000' -> 48500.0"""
    if raw in (None, ""):
        return None
    return int(raw) / PRICE_SCALE


def rate_from_milli(raw: int | None) -> float | None:
    """22500 -> 22.5"""
    if raw is None:
        return None
    return raw / RATE_SCALE


def check_api_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return payload['data'] or raise ValueError describing the API error."""
    if "error" in payload:
        raise ValueError(f"API error: {payload['error']}")
    code = payload.get("code")
    if code != 0:
        raise ValueError(f"API code={code} msg={payload.get('msg')}")
    return payload.get("data") or {}


def _card_fields(card: dict[str, Any]) -> dict[str, Any]:
    rating = card.get("item_rating") or {}
    rating_count = rating.get("rating_count") or []
    return {
        "shop_id": card.get("shopid"),
        "name": card.get("name"),
        "image": card.get("image"),
        "price_vnd": vnd_from_scaled(card.get("price")),
        "price_min_vnd": vnd_from_scaled(card.get("price_min")),
        "price_max_vnd": vnd_from_scaled(card.get("price_max")),
        "price_before_discount_vnd": vnd_from_scaled(card.get("price_before_discount")),
        "discount": card.get("discount") or None,
        "stock": card.get("stock"),
        "sold": card.get("sold"),
        "historical_sold": card.get("historical_sold"),
        "rating_star": rating.get("rating_star"),
        "rating_total": rating_count[0] if rating_count else None,
        "category_id": card.get("catid"),
        "shop_name": card.get("shop_name"),
        "shop_rating": card.get("shop_rating"),
    }


def normalize_list_item(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten one /offer/product/list entry into a scraper row."""
    card = item.get("batch_item_for_item_card_full") or {}
    return {
        "item_id": item.get("item_id"),
        **_card_fields(card),
        "default_commission_rate_pct": parse_rate_text(item.get("default_commission_rate")),
        "seller_commission_rate_pct": parse_rate_text(item.get("seller_commission_rate")),
        "max_commission_rate_pct": parse_rate_text(item.get("max_commission_rate")),
        "is_free_sample": item.get("is_free_sample"),
        "product_link": item.get("product_link"),
        "affiliate_link": item.get("long_link"),
    }


def normalize_detail(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten the /offer/product detail payload into a scraper row."""
    card = data.get("batch_item_for_item_card_full") or {}
    rate_text = data.get("commission_rate") or {}
    rate_num = data.get("commission_rate_detail") or {}
    end_time = data.get("period_end_time")
    return {
        "item_id": data.get("item_id"),
        **_card_fields(card),
        "est_commission_vnd": parse_vnd_text(data.get("commission")),
        "seller_commission_vnd": parse_vnd_text(rate_text.get("seller_commission")),
        "shopee_commission_vnd": parse_vnd_text(rate_text.get("shopee_commission")),
        "commission_cap_vnd": vnd_from_scaled(rate_num.get("commission_cap")),
        "is_capped": rate_text.get("is_capped"),
        "default_commission_rate_pct": rate_from_milli(rate_num.get("default_commission_rate")),
        "seller_commission_rate_pct": rate_from_milli(rate_num.get("seller_commission_rate")),
        "shopee_commission_rate_pct": rate_from_milli(rate_num.get("shopee_commission_rate")),
        "period_start_time": data.get("period_start_time"),
        "period_end_time": None if end_time == NO_END_TIME else end_time,
        "most_used_channel": data.get("most_used_channel"),
        "is_free_sample": data.get("is_free_sample"),
        "product_link": data.get("product_link"),
        "affiliate_link": data.get("long_link"),
    }
