"""Credential-free, shared validation for the public Actor and private service."""

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit

MAX_ITEMS = 1000
ID_PATTERN = r'[1-9][0-9]{0,19}'
TEXT_FIELDS = ('name', 'image', 'discount', 'shop_name')
ID_FIELDS = ('item_id', 'shop_id', 'category_id')
INTEGER_FIELDS = ('stock', 'sold', 'historical_sold', 'rating_total',
                  'period_start_time', 'period_end_time')
NUMBER_FIELDS = (
    'price_vnd', 'price_min_vnd', 'price_max_vnd', 'price_before_discount_vnd',
    'rating_star', 'shop_rating', 'est_commission_vnd', 'seller_commission_vnd',
    'shopee_commission_vnd', 'commission_cap_vnd', 'default_commission_rate_pct',
    'seller_commission_rate_pct', 'shopee_commission_rate_pct',
)
BOOL_FIELDS = ('is_capped', 'is_free_sample')
PUBLIC_FIELDS = (*ID_FIELDS, *TEXT_FIELDS, *INTEGER_FIELDS, *NUMBER_FIELDS,
                 *BOOL_FIELDS, 'most_used_channel', 'product_link')


class InputError(ValueError):
    """Safe to display without reflecting any supplied input."""


def product_id(value: object) -> str:
    if type(value) not in (str, int) or not re.fullmatch(ID_PATTERN, str(value)):
        raise InputError('Product IDs must be positive decimal identifiers of at most 20 digits.')
    return str(value)


def id_from_url(value: object) -> str:
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise InputError('A valid full Shopee Vietnam product URL is required.')
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in ('shopee.vn', 'www.shopee.vn')
                or url.username is not None or url.password is not None
                or url.port not in (None, 443)):
            raise ValueError
    except ValueError:
        raise InputError('Use a full HTTPS shopee.vn product URL; short links are unsupported.') from None
    full = re.fullmatch(r'/product/(' + ID_PATTERN + r')/(' + ID_PATTERN + r')/?', url.path)
    slug = re.fullmatch(r'/[^/]+-i\.(' + ID_PATTERN + r')\.(' + ID_PATTERN + r')/?', url.path)
    match = full or slug
    if not match:
        raise InputError('Unsupported Shopee product URL format; short links are unsupported.')
    return match.group(2)


def normalize_input(value: object) -> list[str]:
    """Validate everything before applying maxItems; preserve first-seen order."""
    if not isinstance(value, Mapping) or set(value) - {'productIds', 'startUrls', 'maxItems'}:
        raise InputError('Supported input fields are productIds, startUrls and maxItems.')
    limit = value.get('maxItems', MAX_ITEMS)
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise InputError('maxItems must be an integer between 1 and 1000.')
    identifiers, urls = value.get('productIds', []), value.get('startUrls', [])
    if (not isinstance(identifiers, list) or not isinstance(urls, list)
            or len(identifiers) > MAX_ITEMS or len(urls) > MAX_ITEMS):
        raise InputError('Each product input must be a list of at most 1000 entries.')
    items = [product_id(item) for item in identifiers]
    for entry in urls:
        if isinstance(entry, dict):
            if set(entry) != {'url'}:
                raise InputError('Each URL object must contain only url.')
            entry = entry['url']
        items.append(id_from_url(entry))
    unique = list(dict.fromkeys(items))
    if not 1 <= len(unique) <= MAX_ITEMS:
        raise InputError('Supply between 1 and 1000 unique products.')
    return unique[:limit]


def _output_field(name: str, value: object) -> object:
    if value is None:
        return None
    if name in ID_FIELDS:
        return product_id(value)
    if name in TEXT_FIELDS:
        valid = isinstance(value, str) and len(value) <= 16384
    elif name in INTEGER_FIELDS:
        valid = type(value) is int and 0 <= value <= 2**63 - 1
    elif name in NUMBER_FIELDS:
        valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e18
    elif name in BOOL_FIELDS:
        valid = type(value) is bool
    else:  # Channel metadata is a scalar, never an account object.
        valid = (type(value) is int and 0 <= value <= 2**31 - 1) or (
            isinstance(value, str) and len(value) <= 256)
    if not valid:
        raise ValueError('Malformed normalized product field: ' + name)
    return value


def normalize_output(product: Mapping, *, fetched_at: datetime, cache_hit: bool) -> dict:
    """Allowlist product data; reconstruct links without any account attribution."""
    if (not isinstance(product, Mapping) or not isinstance(fetched_at, datetime)
            or fetched_at.tzinfo is None or type(cache_hit) is not bool):
        raise ValueError('Invalid output metadata')
    item = product_id(product.get('item_id'))
    row = {name: _output_field(name, product.get(name))
           for name in PUBLIC_FIELDS if name != 'product_link'}
    url = f"https://shopee.vn/product/{row['shop_id']}/{item}" if row['shop_id'] else None
    return {**row, 'product_link': url, 'schemaVersion': '1.0',
            'fetchedAt': fetched_at.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
            'cacheHit': cache_hit}
