import hashlib
import json

import pytest

from shopee_affiliate.openapi import (
    OpenApiClient,
    OpenApiError,
    build_product_offer_query,
    check_graphql_payload,
    normalize_offer,
    sign,
)


def test_sign_hashes_app_id_timestamp_payload_secret_in_order():
    expected = hashlib.sha256(b'123' b'1700000000' b'{"query":"q"}' b"s3cret").hexdigest()

    header = sign("123", "s3cret", 1700000000, '{"query":"q"}')

    assert header == f"SHA256 Credential=123, Timestamp=1700000000, Signature={expected}"


def test_sign_changes_when_payload_changes():
    assert sign("1", "s", 1, "a") != sign("1", "s", 1, "b")


def test_query_inlines_ints_and_skips_empty_args():
    query = build_product_offer_query(itemId=26609048170, shopId=None, keyword="", limit=1)

    assert "productOfferV2(itemId: 26609048170, limit: 1)" in query
    assert "shopId" not in query.split("{", 2)[1]
    assert "commissionRate" in query and "pageInfo" in query


def test_query_escapes_keyword_string():
    query = build_product_offer_query(keyword='ốp "lưng"')

    assert 'keyword: "ốp \\"lưng\\""' in query


def test_query_rejects_unsupported_values():
    with pytest.raises(TypeError):
        build_product_offer_query(itemId=1.5)
    with pytest.raises(TypeError):
        build_product_offer_query(isAMSOffer=True)


def test_check_graphql_payload_raises_with_error_codes():
    payload = {"errors": [{"message": "Invalid Authorization Header", "extensions": {"code": 10020}}]}

    with pytest.raises(OpenApiError, match=r"\[10020\] Invalid Authorization Header"):
        check_graphql_payload(payload)


def test_check_graphql_payload_requires_data():
    with pytest.raises(OpenApiError):
        check_graphql_payload({})
    assert check_graphql_payload({"data": {"x": 1}}) == {"x": 1}


def test_normalize_offer_converts_fractions_and_strings():
    node = {
        "itemId": 26609048170,
        "shopId": 89827191,
        "productName": "Ốp lưng",
        "priceMin": "48500",
        "priceMax": "52000",
        "commissionRate": "0.225",
        "sellerCommissionRate": "0.2",
        "shopeeCommissionRate": "0.025",
        "commission": "10912.5",
        "ratingStar": "4.9",
        "sales": 1200,
        "offerLink": "https://s.shopee.vn/abc",
    }
    original = json.dumps(node)

    row = normalize_offer(node)

    assert row["item_id"] == "26609048170"
    assert row["shop_id"] == "89827191"
    assert row["price_min_vnd"] == 48500.0
    assert row["default_commission_rate_pct"] == 22.5
    assert row["seller_commission_rate_pct"] == 20.0
    assert row["shopee_commission_rate_pct"] == 2.5
    assert row["est_commission_vnd"] == 10912.5
    assert row["rating_star"] == 4.9
    assert row["affiliate_link"] == "https://s.shopee.vn/abc"
    assert json.dumps(node) == original


def test_normalize_offer_handles_missing_fields():
    row = normalize_offer({})

    assert row["item_id"] is None
    assert row["default_commission_rate_pct"] is None
    assert row["price_min_vnd"] is None


def test_client_requires_credentials():
    with pytest.raises(ValueError, match="SHOPEE_AFFILIATE_APP_ID"):
        OpenApiClient("", "")
