import pytest

from shopee_affiliate.parse import (
    BlockedError,
    check_api_payload,
    normalize_detail,
    normalize_list_item,
    parse_rate_text,
    parse_vnd_text,
    rate_from_milli,
    vnd_from_scaled,
)


class TestUnitParsers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("22,5%", 22.5), ("20%", 20.0), ("0%", 0.0), ("", None), (None, None)],
    )
    def test_parse_rate_text_handles_comma_decimal(self, text, expected):
        assert parse_rate_text(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("₫10.913", 10913), ("₫40.000", 40000), ("₫1.213.500", 1213500), ("", None), (None, None)],
    )
    def test_parse_vnd_text_strips_thousands_separator(self, text, expected):
        assert parse_vnd_text(text) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("4850000000", 48500.0), ("4000000000", 40000.0), ("", None), (None, None)],
    )
    def test_vnd_from_scaled_divides_by_100000(self, raw, expected):
        assert vnd_from_scaled(raw) == expected

    def test_rate_from_milli_divides_by_1000(self):
        assert rate_from_milli(22500) == 22.5
        assert rate_from_milli(None) is None


CARD = {
    "itemid": "26609048170",
    "shopid": "89827191",
    "name": "Ốp Lưng iPhone",
    "image": "vn-11134207-7ra0g-ma0no4sczi8q98",
    "price": "4850000000",
    "price_min": "4850000000",
    "price_max": "5150000000",
    "price_before_discount": "6000000000",
    "discount": "19%",
    "stock": 35118,
    "sold": 1000000,
    "historical_sold": 1000000,
    "item_rating": {"rating_star": 4.79, "rating_count": [5847, 126, 62, 114, 306, 5239]},
    "catid": 100013,
    "shop_name": "SHIN CASE",
    "shop_rating": 4.918997,
    "label_ids": ["1000254"],
}


class TestNormalize:
    def test_normalize_list_item_flattens_card_and_converts_units(self):
        item = {
            "item_id": "26609048170",
            "long_link": "https://shopee.vn/universal-link/product/89827191/26609048170?x=1",
            "product_link": "https://shopee.vn/product/89827191/26609048170",
            "default_commission_rate": "22,5%",
            "seller_commission_rate": "20%",
            "max_commission_rate": "0%",
            "is_free_sample": False,
            "batch_item_for_item_card_full": CARD,
        }

        row = normalize_list_item(item)

        assert row["item_id"] == "26609048170"
        assert row["shop_id"] == "89827191"
        assert row["price_vnd"] == 48500.0
        assert row["price_max_vnd"] == 51500.0
        assert row["default_commission_rate_pct"] == 22.5
        assert row["seller_commission_rate_pct"] == 20.0
        assert row["rating_star"] == 4.79
        assert row["rating_total"] == 5847
        assert row["affiliate_link"].startswith("https://shopee.vn/universal-link/")
        assert "label_ids" not in row

    def test_normalize_list_item_does_not_mutate_input(self):
        item = {"item_id": "1", "batch_item_for_item_card_full": dict(CARD)}
        snapshot = {"item_id": "1", "batch_item_for_item_card_full": dict(CARD)}

        normalize_list_item(item)

        assert item == snapshot

    def test_normalize_detail_reads_numeric_commission_block(self):
        data = {
            "item_id": "26609048170",
            "long_link": "https://shopee.vn/universal-link/x",
            "product_link": "https://shopee.vn/product/89827191/26609048170",
            "period_start_time": 1790787600,
            "period_end_time": 32503651199,
            "commission": "₫10.913",
            "commission_rate": {"is_capped": False, "seller_commission": "₫9.700", "shopee_commission": "₫1.213"},
            "commission_rate_detail": {
                "commission_cap": "4000000000",
                "default_commission_rate": 22500,
                "seller_commission_rate": 20000,
                "shopee_commission_rate": 2500,
            },
            "batch_item_for_item_card_full": CARD,
        }

        row = normalize_detail(data)

        assert row["est_commission_vnd"] == 10913
        assert row["seller_commission_vnd"] == 9700
        assert row["shopee_commission_vnd"] == 1213
        assert row["commission_cap_vnd"] == 40000.0
        assert row["default_commission_rate_pct"] == 22.5
        assert row["shopee_commission_rate_pct"] == 2.5
        assert row["period_end_time"] is None  # year-3000 sentinel means "no end"
        assert row["period_start_time"] == 1790787600
        assert row["name"] == "Ốp Lưng iPhone"


class TestCheckApiPayload:
    def test_returns_data_on_success(self):
        assert check_api_payload({"code": 0, "msg": "success", "data": {"a": 1}}) == {"a": 1}

    def test_raises_on_error_shape(self):
        with pytest.raises(ValueError, match="error_not_found"):
            check_api_payload({"error": "error_not_found"})

    def test_raises_on_nonzero_code(self):
        with pytest.raises(ValueError, match="code=1"):
            check_api_payload({"code": 1, "msg": "fail"})

    def test_blocked_error_is_distinct(self):
        assert not issubclass(BlockedError, ValueError)
