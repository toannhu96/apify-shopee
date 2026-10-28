from datetime import UTC, datetime

import pytest

from shopee_contracts import InputError, normalize_input, normalize_output


def test_deduplicates_ids_and_standard_urls_without_network():
    source = {'productIds': ['123', 456], 'startUrls': [
        {'url': 'https://shopee.vn/product/99/123?utm_source=test'},
        'https://shopee.vn/Test-product-i.99.789',
    ]}
    assert normalize_input(source) == ['123', '456', '789']
    assert source['productIds'] == ['123', 456]


@pytest.mark.parametrize('source', [
    {}, [], None, {'productIds': []}, {'productIds': '123'},
    {'productIds': [True]}, {'productIds': ['001']}, {'productIds': ['１２']},
    {'productIds': ['1' * 21]}, {'productIds': ['1'], 'maxItems': True},
    {'productIds': ['1'], 'maxItems': 1001}, {'productIds': ['1'], 'maxItems': 0},
    {'startUrls': ['https://evil.test/product/1/2']},
    {'startUrls': ['https://shopee.vn.evil.test/product/1/2']},
    {'startUrls': ['https://user:pass@shopee.vn/product/1/2']},
    {'startUrls': ['https://shopee.vn:123/product/1/2']},
    {'startUrls': ['https://shopee.vn/product/01/2']},
    {'startUrls': ['https://shopee.vn/product/1/2/extra']},
    {'startUrls': ['https://shopee.vn/product/1/2\n']},
    {'startUrls': ['https://s.shopee.vn/abc']},
    {'productIds': ['1'], 'cookies': 'never-accept'},
    {'productIds': [str(i) for i in range(1, 1002)]},
])
def test_invalid_inputs_are_rejected_without_echoing_values(source):
    with pytest.raises(InputError) as caught:
        normalize_input(source)
    assert 'pass@' not in str(caught.value)
    assert 'never-accept' not in str(caught.value)


def test_limit_applies_after_stable_deduplication():
    assert normalize_input({'productIds': ['1', '1', '2', '3'], 'maxItems': 2}) == ['1', '2']


def test_output_is_allowlisted_and_identifiers_are_strings():
    product = {'item_id': 123, 'shop_id': 456, 'category_id': 789,
               'name': 'Sample', 'price_vnd': 48500.0, 'stock': 0,
               'default_commission_rate_pct': 22.5, 'affiliate_link': 'secret',
               'raw': {'cookie': 'secret'}, 'product_link': 'https://shopee.vn/product/456/123'}
    row = normalize_output(product, fetched_at=datetime(2026, 10, 28, tzinfo=UTC), cache_hit=True)
    assert row['item_id'] == '123' and row['shop_id'] == '456'
    assert row['category_id'] == '789' and row['stock'] == 0
    assert row['fetchedAt'] == '2026-10-28T00:00:00Z' and row['cacheHit'] is True
    assert row['schemaVersion'] == '1.0'
    assert row['product_link'] == 'https://shopee.vn/product/456/123'
    assert 'affiliate_link' not in row and 'raw' not in row
    assert 'secret' not in str(row)
    assert row['price_min_vnd'] is None


def test_output_reconstructs_product_url_instead_of_forwarding_tracking():
    row = normalize_output({'item_id': '1', 'shop_id': '2',
                            'product_link': 'https://evil.test/?token=secret'},
                           fetched_at=datetime.now(UTC), cache_hit=False)
    assert row['product_link'] == 'https://shopee.vn/product/2/1'


@pytest.mark.parametrize('product', [
    {'item_id': True}, {'item_id': '1', 'price_vnd': float('nan')},
    {'item_id': '1', 'name': {'cookie': 'secret'}},
    {'item_id': '1', 'stock': False}, {'item_id': '1', 'is_capped': 'yes'},
])
def test_malformed_output_fails_closed(product):
    with pytest.raises(ValueError):
        normalize_output(product, fetched_at=datetime.now(UTC), cache_hit=False)


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError):
        normalize_output({'item_id': '1'}, fetched_at=datetime(2026, 1, 1), cache_hit=False)
