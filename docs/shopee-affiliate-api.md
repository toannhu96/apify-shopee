# Shopee Affiliate (VN) product offer API

Notes from reverse engineering `https://affiliate.shopee.vn/offer/product_offer` (list) and
`https://affiliate.shopee.vn/offer/product_offer/<item_id>` (detail), captured on 2026-10-27.

Sources:

- **Static:** the site's public JS bundles (`app.ee0652df.js`, `product_offer.238232e4.js`,
  `product_offer_detail.264e4c7b.js`, `5870.f1b21fa7.js`, anti-bot VM in `8568.e933698b.js`), served from
  `deo.shopeemobile.com/shopee/shopee-affiliate-live-vn/static/js/`.
- **Live:** network capture in a logged-in browser session.

Bundle hashes change on every deploy. Re-check enums and endpoints if the site behaves differently.

## Requirements

- A logged-in Shopee session on an account that is a registered affiliate. Signed-out requests to
  `affiliate.shopee.vn` redirect to `shopee.vn/buyer/login?next=...`.
- Every product-data request must be signed by Shopee's anti-bot SDK (see [Anti-bot](#anti-bot)).
  In practice this means the requests must run inside a real browser page on `affiliate.shopee.vn`.

## Base URL and common headers

The app's HTTP client (axios) uses base URL `https://affiliate.shopee.vn/api/v3` with `withCredentials: true`.

| Header | Value | When |
|---|---|---|
| Cookies | Shopee session cookies | Always |
| `Affiliate-Program-Type` | `1` (the "normal" program) | Always |
| `CSRF-token` | From `/api/v3/config/website` (`csrf_token`) or the `csrf-token` response header of GQL/BFF calls | Non-GET requests only (GraphQL, batch links) |
| `x-sap-ri`, `x-sap-sec`, `af-ac-enc-sz-token`, `af-ac-enc-dat`, `x-sz-sdk-version` | Added by the anti-bot SDK | Endpoints on the SDK policy list |

Captured anti-bot header values (shape only):

| Header | Shape |
|---|---|
| `x-sap-ri` | 52 hex characters |
| `x-sap-sec` | ~1.4 KB opaque string |
| `af-ac-enc-sz-token` | Three `|`-separated base64 segments |
| `af-ac-enc-dat` | 16 random hex characters (8 random bytes) |
| `x-sz-sdk-version` | `1.12.21` |

The SDK's policy list (`sapPolicies`) covers:

- `api/v3/gql`
- `api/v3/offer/product`
- `api/v3/offer/product/list`
- `api/v3/dashboard/detail`
- `/api/v3/offer/batch_product_links`

## Endpoints

### Product list

```
GET /api/v3/offer/product/list
```

| Param | Type | Notes |
|---|---|---|
| `list_type` | int | See [list_type](#list_type). Category tabs send `3` (LANDING_CATEGORY). |
| `sort_type` | int | See [sort_type](#sort_type). Default `1`. |
| `page_offset` | int | `0`, `20`, `40`, ... |
| `page_limit` | int | The web UI uses `20`. |
| `client_type` | int | Always `1`. |
| `keyword` | string | Optional search text. The UI sends it with `list_type=0`. |
| `match_type` | int | Optional. See [match_type](#match_type). Used with `match_id`. |
| `match_id` | string | Shop ID, category ID or collection ID, depending on `match_type`. |
| `filter_types` | string | Optional. |
| `filter_shop_types` | string | Optional. See [shop type](#shop-type). |
| `trace` | string | Optional JSON tracking blob passed through from the previous page. Not required. |

Captured request: `?list_type=0&sort_type=1&page_offset=0&page_limit=20&client_type=1`.

Response:

```jsonc
{
  "code": 0,
  "msg": "success",
  "data": {
    "page_offset": 20,
    "page_limit": 20,
    "total_count": 500,   // observed cap; paging stops at 500 results per query
    "trace": "{...}",
    "list": [ /* ListItem */ ]
  }
}
```

`ListItem`:

| Field | Example | Notes |
|---|---|---|
| `item_id` | `"26609048170"` | String |
| `long_link` | `https://shopee.vn/universal-link/product/<shopid>/<itemid>?gads_t_sig=...&utm_medium=affiliates&utm_source=an_<affiliate_id>` | Affiliate link for the logged-in account |
| `product_link` | `https://shopee.vn/product/89827191/26609048170` | Plain product URL |
| `default_commission_rate` | `"22,5%"` | Display string, comma as decimal separator |
| `seller_commission_rate` | `"20%"` | Display string |
| `max_commission_rate` | `"0%"` | Display string |
| `is_free_sample` | `false` | |
| `is_seller_invited` | `false` | |
| `offer_card_type` | `1` | |
| `invited_campaign_offer_card` | `{ "shop_commission_rate": "" }` | |
| `promotion_vouchers` | `[]` | |
| `is_refundable_sample` | `false` | |
| `trace` | JSON string | Tracking |
| `batch_item_for_item_card_full` | object | See [Product card](#product-card) |

### Product detail

```
GET /api/v3/offer/product?item_id=<item_id>
```

Response `data`:

| Field | Example | Notes |
|---|---|---|
| `item_id`, `long_link`, `product_link` | | Same as list |
| `period_start_time` | `1790787600` | Unix seconds |
| `period_end_time` | `32503651199` | Unix seconds. This value (year 3000) means no end date. |
| `commission` | `"₫10.913"` | Estimated payout per sale at the current price |
| `most_used_channel` | `1` | See [commission channel](#commission-channel) |
| `brand_commission_status` | `0` | |
| `is_free_sample` | `false` | |
| `commission_rate` | object | Display strings, see below |
| `commission_rate_detail` | object | Numeric values, see below |
| `batch_item_for_item_card_full` | object | See [Product card](#product-card) |

`commission_rate` (display strings):

- **Rates:** `max_commission_rate`, `default_commission_rate`, `seller_commission_rate`,
  `shopee_commission_rate`, `exist_platform_commission_rate`, `new_platform_commission_rate`, plus
  `web_exist_commission_rate`, `web_new_commission_rate`, `app_exist_commission_rate` and `app_new_commission_rate`.
- **Amounts:** `default_commission`, `seller_commission`, `shopee_commission`, plus the web/app × exist/new variants.
- **Caps:** `commission_cap`, `shopee_new_user_commission_cap`, `is_capped`.
- **`shopee_commission_detail`:** Shopee's share for each combination of:
  - channel: `social_media`, `shopee_video`, `live_streaming`, `facebook_reels_videos`
  - basis: `item_base`, `shop_base`, `check_out_base`
  - buyer: `new` or `exist`
  - for video and live channels, also `discovery` vs `conversion`
- **`seller_commission_detail`:** the seller's share per channel, for example:
  - `social_media_default_exist_commission_rate`
  - `shopee_video_discovery_commission_rate`
  - `live_stream_conversion_commission_rate`

`commission_rate_detail` (numeric, preferred for scraping):

```jsonc
{
  "commission_cap": "4000000000",               // VND x 100,000 -> 40,000 ₫
  "shopee_new_user_commission_cap": "4000000000",
  "default_commission_rate": 22500,             // x 1000 -> 22.5%
  "seller_commission_rate": 20000,              // 20%
  "shopee_commission_rate": 2500,               // 2.5%
  "shopee_commission_detail": { /* same keys as above, ints x 1000 */ },
  "seller_commission_detail": {
    "social_media_default_exist_commission_rate": 20000,
    "shopee_video_discovery_commission_rate": 22000,
    "social_media_commission_end_time": "32503651199"
    // ...
  },
  "channel_cap": {
    "new_user":   { "social_media_cap": "4000000000", "shopee_video_cap": "...", "live_streaming_cap": "...", "facebook_reels_videos_cap": "" },
    "exist_user": { /* same */ }
  }
}
```

Sanity check from the capture:

- Price 48,500 ₫ × 22.5% = 10,912.5 ₫, shown as `commission: "₫10.913"`.
- Seller share: 9,700 ₫ (20%). Shopee share: 1,213 ₫ (2.5%).

### Product card

`batch_item_for_item_card_full` has the same shape in list and detail responses. The main fields:

| Field | Example | Notes |
|---|---|---|
| `itemid`, `shopid` | `"26609048170"`, `"89827191"` | Strings |
| `name` | | |
| `image`, `images[]` | `vn-11134207-7ra0g-ma0no4sczi8q98` | Image IDs, not URLs |
| `currency` | `"VND"` | |
| `price`, `price_min`, `price_max` | `"4850000000"` | VND × 100,000 (48,500 ₫) |
| `price_before_discount`, `price_min_before_discount`, `price_max_before_discount` | `"6000000000"` | VND × 100,000 |
| `discount`, `raw_discount`, `show_discount` | `"19%"`, `19`, `19` | |
| `stock` | `35118` | |
| `sold`, `historical_sold` | `1000000` | |
| `sold_text`, `historical_sold_text` | `"1tr+"` | Localized display |
| `liked_count`, `cmt_count` | `19938`, `51212` | |
| `item_rating.rating_star` | `4.79` | |
| `item_rating.rating_count` | `[5847,126,62,114,306,5239]` | `[total, 1★, 2★, 3★, 4★, 5★]` |
| `item_rating.rcount_with_context`, `item_rating.rcount_with_image` | | |
| `catid` | `100013` | |
| `brand` | | |
| `shop_name`, `shop_rating` | | |
| `shop_location` | | |
| `is_official_shop`, `shopee_verified`, `is_preferred_plus_seller`, `is_mart` | | |
| `tier_variations[]` | `{ name, options[], images[] }` | Variant axes |
| `video_info_list[]` | `{ video_id, duration, formats[{ url, defn, width, height }], default_format }` | Video URLs are absolute |
| `is_on_flash_sale`, `can_use_cod`, `add_on_deal_info`, `voucher_info`, `bundle_deal_info` | | Promotions |
| `ctime` | `1724557088` | Listing creation, Unix seconds |
| `item_status`, `status` | `"normal"`, `1` | |
| `label_ids[]` | | Internal labels, large, can be dropped |

Image IDs are probably served from `https://down-vn.img.susercontent.com/file/<id>`, but that pattern
was not verified in this capture.

### GraphQL: `POST /api/v3/gql?q=<name>`

All GraphQL calls need the `CSRF-token` header.

- **`offerTabList`** (`getOfferTabList`): returns the tabs on the product offer page.

  ```graphql
  query getOfferTabList { offerTabList { listType l1BeCatId feCatId name image } }
  ```

  Captured response:

  ```jsonc
  [
    { "listType": "0" },
    { "listType": "7" },
    { "listType": "2" },
    { "listType": "3", "l1BeCatId": "100630", "feCatId": "11036279", "name": "Sắc Đẹp" }
  ]
  ```

  More category tabs followed, for example 100010, 100011 and 100001.

- **`productOfferLinks`** (`batchGetProductOfferLink`): generates affiliate links.
  - Variables: `productOfferLinkParams: [ProductOfferLinkParam!]!`, `sourceCaller` (`WEB_SITE_CALLER`),
    and optionally `advancedLinkParams`.
  - Returns `{ itemId, shopId, productOfferLink }`.
  - Not needed for scraping, because `long_link` already contains the affiliate link.

### Other endpoints seen

- **Page startup:** `GET /api/v3/config/website`, `/user/profile`, `/user/status`,
  `/user/check_program_permission?program_type=2`, `/offer/checkInAmsWhiteList`.
- **Shop offers:** `GET /api/v3/offer/shop/list` (params `sort_type`, `page_offset`, `page_limit`,
  `shop_type`, `keyword`) and `GET /api/v3/offer/shop?shop_id=`.
- **Batch link generation:** `POST /api/v3/offer/batch_product_links`, `/offer/batch_shop_links`,
  `/offer/batch_campaign_links`.

## Enums

### list_type

| Value | Name |
|---|---|
| 0 | ALL |
| 1 | HIGHEST_COMMISSION |
| 2 | TOP_PERFORMING (the "Bán chạy nhất" tab) |
| 3 | LANDING_CATEGORY |
| 4 | DETAIL_CATEGORY |
| 5 | DETAIL_SHOP |
| 6 | DETAIL_COLLECTION |
| 7 | BRAND_COMMISSION |
| 8 | LIST_TYPE_SPECIFIC |
| 9 | FREE_SAMPLE |

### sort_type

| Value | Name |
|---|---|
| 1 | RELEVANCE_DESC |
| 2 | ITEM_SOLD_DESC |
| 3 | PRICE_DESC |
| 4 | PRICE_ASC |
| 5 | COMMISSION_DESC |

### match_type

| Value | Name |
|---|---|
| 1 | MATCH_SHOP |
| 2 | MATCH_CATEGORY |
| 3 | MATCH_COLLECTION |

### Shop type

`filter_shop_types` and `shop_type`:

| Value | Name |
|---|---|
| 0 | UNKNOWN |
| 1 | MALL |
| 2 | PREFERRED |

### Commission channel

| Value | Name |
|---|---|
| 1 | Social media |
| 2 | Shopee Video |
| 3 | Live streaming |
| 5 | Meta Reels / Videos |

## Units cheat sheet

| Value | Unit | Convert |
|---|---|---|
| `price*`, `commission_cap`, `*_cap` in `commission_rate_detail` | VND × 100,000 (string) | `Number(v) / 100000` |
| Rates in `commission_rate_detail` | percent × 1000 (int) | `v / 1000` → percent |
| Rates in `commission_rate` / list items | display string, `,` decimal | parse `"22,5%"` → 22.5 |
| Amounts in `commission_rate` / `commission` | display string, `.` thousands | parse `"₫10.913"` → 10913 |
| `*_time`, `ctime` | Unix seconds | `32503651199` = no end date |

## Anti-bot

- **The SDK is an obfuscated JavaScript VM** (chunk `8568`, SDK version `1.12.21`). It
  fingerprints the device and signs requests. Rebuilding it outside a browser is not practical.
- **It hooks `XMLHttpRequest` globally.** A plain XHR made from page JS on a logged-in
  `affiliate.shopee.vn` page gets the `x-sap-*` / `af-ac-*` headers added automatically. This was
  verified live: `code: 0` with 20 items.
- **The server scores behaviour, not just signatures.** On 2026-10-27, one extra list call and two
  duplicate detail calls for the same item, fired right after the page's own detail call, redirected
  the tab to `https://shopee.vn/verify/traffic/error` ("Page Unavailable").
  - Recovery went through `/verify/traffic?...&scene=crawler_item` and a captcha
    (`/api/v4/anti_fraud/captcha/verify_v2`), which the user solved by hand.
  - `scene=crawler_item` indicates item crawling is what got flagged.
- **A single, normal page load of the detail page afterwards succeeded.**

Rules for the scraper:

1. Run Playwright with a logged-in affiliate session, and keep the session (cookies and storage) between runs.
2. Issue API calls with XHR from inside an `affiliate.shopee.vn` page so the SDK signs them. Do not
   call the API from Node directly.
3. Pace requests, add jitter, and never repeat the same `item_id` request back to back.
4. Prefer the list endpoint for coverage. Fetch detail only for items that need the full commission breakdown.
5. Treat any redirect to `/verify/traffic` or `/buyer/login` as a hard stop: back off and alert. Never
   try to solve or bypass the captcha automatically.
6. Expect at most 500 results per list query (`total_count` cap). Split large crawls by category
   (`list_type=3` plus a category ID from `offerTabList`), `sort_type` or keyword.

## Test script

`shopee_affiliate/probe.py` follows the rules above:

- **Session:** a persistent Chrome profile in `.auth/shopee-profile`. On the first run you log in by hand
  in the window it opens.
- **List:** reuses the page's own first list response, then fetches further pages with signed in-page XHR.
- **Detail:** opens each item's page once and reads only that page's own `/offer/product` response.
- **Pacing:** random 4–9 s delays between calls.
- **Blocks:** stops on any login or `/verify/traffic` redirect.
- **Output:** raw JSON and normalized `rows.json` go to `output/run-<timestamp>/`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m shopee_affiliate.probe list --pages 2
.venv/bin/python -m shopee_affiliate.probe list --list-type 1 --sort-type 5 --keyword "ốp lưng"
.venv/bin/python -m shopee_affiliate.probe detail --item-id 26609048170
.venv/bin/python -m pytest -q
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Request failed |
| 2 | Bad arguments |
| 3 | Blocked or logged out: stop and wait before retrying |
