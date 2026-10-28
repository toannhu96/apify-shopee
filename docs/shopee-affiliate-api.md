# Shopee Affiliate (VN) product offer API

## Managed Apify integration

The public Actor and private backend are documented in
[Managed Shopee Vietnam Actor](managed-actor.md). The managed worker uses the
new `session_signer` to persist cumulative request history and calls
`signed_client.fetch_signed_product` with a reserved signature. The standalone
client below remains a single-lookup interface. Offline tests and container
checks do not establish sustained live acceptance or production capacity.

## Browser-free Python client — verified 2026-10-28

`shopee_affiliate.signed_client` generates fresh signatures in Python and calls the
website's internal product endpoint directly. It needs no running browser, Node,
vendor JavaScript, Open API App ID or Open API secret at runtime.

**It requires your cookies plus an accepted signing-state seed. Cookies alone do
not contain the device state and cached token used by SAP.** The seed comes from
one normal logged-in product request, preferably the first request after a page
reload. It is reused locally, with fresh request entropy, time, URL hashes,
counters, encrypted collections and checksums generated for each call.

```bash
# Minimal runtime dependency; Chrome/Playwright are not used by this client.
.venv/bin/python -m pip install curl_cffi==0.17.0

.venv/bin/python -m shopee_affiliate.signed_client 26609048170 \
  --state-file .auth/signing-state.json \
  --cookie-file .auth/cookies.json > output/product.json
```

Create `output/` first if absent. JSON output contains `raw` and normalized
`product`; it may include your account-specific affiliate link. Exit 0 means
validated product data, 2 means blocked/login/redirect, and 1 means another
input/signing/transport/response failure. Each invocation sends one GET with
verified TLS, a timeout and an 8 MiB response limit. It never retries or falls
back to captured signatures.

The client is stateless across invocations: each call advances the supplied
seed's counters once. Reusing the same file does not maintain cumulative SDK
request history. The verified scope is individual product lookups; sustained
batch loops and automatic session renewal have not been verified.

### Local inputs

The cookie file accepts a scoped JSON cookie array or a storage-state object
with `cookies`. Scoped exports support duplicate names on different domains.
A raw Cookie line is also accepted when names do not collide. `--cookie-stdin`
can replace `--cookie-file`. Keep credentials out of command arguments and chat.

The signing-state file has exactly this structure:

```json
{"headers": {"x-sap-ri": "...", "x-sap-sec": "...", "af-ac-enc-sz-token": "...", "user-agent": "...", "x-sz-sdk-version": "1.12.21"}}
```

Preserve the other observed request headers too, including Accept, Referer,
client hints, Affiliate-Program-Type and fetch metadata. Exclude Cookie and
HTTP/2 pseudo headers such as `:authority`. The client validates header names,
sizes, duplicate keys, origin, SDK version and signature structure. Real state
is sensitive: store it under ignored `.auth/` with directory mode700 and file
mode600. The client reads these files; it never writes updated credentials.

The current wrapper is for the product-detail path. For a seed less than two
minutes old, its first-request count must be1 so the path-window anchor can be
recovered. A seed older than two minutes permits the source-defined window
reset. Elapsed state contains only whole seconds; continuation from the captured
wall-clock timestamp has subsecond phase uncertainty. Cookies/token expiry and
future SDK changes may require a new export. Token issuance/refresh and a full
browser fingerprint collector are not implemented. Unlimited seed lifetime or
unattended login renewal has not been established.

### Live verification

| Request | Result |
|---|---|
| Fresh Python signature, seed age16s, product26609048170 | HTTP200, API code0, expected product |
| Complete `signed_client.fetch_product`, scoped cookie JSON, seed age344s, different product47310557966 | API code0, expected product |

These were new signatures, not captured-header replays. Credentials stayed in
memory during the tests. Sanitized records are in
`output/signature-reconstruction/live-python-tokenfix-result.json` and
`live-python-second-product-result.json`. Normalized products, with affiliate
links removed, are under `output/signing-research-20261028/`.

The decisive defect was an unmasked token inside the Salsa collection. Empty-token
synthetic fixtures initially missed it; rekeying that field corrupted real
127-byte tokens. The implementation now preserves it raw and tests it against
nonempty-token outputs from the original SDK. Earlier rejected experiments are
retained as investigation evidence, not presented as current client behavior.

## Signature implementation

| Module | Responsibility |
|---|---|
| `sap_signer.py` | Seed validation, fresh entropy, clocks and request/window rates |
| `sap_codec.py` | RI, XXH32, SEC framing and custom Base64 |
| `sap_request.py` | Request mask, URL digest, timestamp masking and field rotation |
| `sap_state.py`, `sap_payload.py` | Bounded rotated FlatBuffer decode/serialization |
| `sap_crypto.py`, `sap_salsa20.py` | Custom cipher, Salsa20, keys and checksums |
| `sap_collections.py` | Typed collection parsing, request-state refresh and token preservation |
| `sap_detector.py` | Re-encode captured detector facts without inventing them |
| `sap_refresh.py` | Assemble refreshed root and encrypted collections |
| `http_transport.py`, `signed_client.py` | One bounded HTTPS GET and product validation |

### How the recovered layers work

`x-sap-ri` is the lowercase hex representation of 26 bytes. Bytes 0–3 hold seconds as a
little-endian uint32; bytes 4–25 start as 22 supplied random bytes. The high nibble of byte 11
is set to 3, byte 12 to 5, and byte 13 to 1. Byte 4 supplies the outer SEC XOR mask.

The SEC envelope before XOR is:

```text
uint32LE XXH32(bytes[4:], seed=0)
uint32LE 20190001
uint16LE payload_length
payload_bytes
```

Every byte is XORed with RI byte 4, then Base64-encoded with this alphabet and normal padding:

```text
shopEeSHOPkrIJ45KL02/376BM+NQcdRntquvU1VW89XDFTACGYwxZabfgijlmyz
```

The payload is a FlatBuffer with 57 possible root fields. The field index is
`(canonical_index + rotation) % 57`, where
`rotation = (RI_seconds + RI_bytes[22] + RI_bytes[23]) % 57`.
Vector order and field insertion order matter for exact byte parity. `build_payload` accepts
already-rotated indices by default, or canonical indices with the explicit `rotation` argument.

The recovered request-key transform interleaves four timestamp bytes T with four supplied
random bytes R as `[T0^R0, R0, T1^R1, R1, T2^R2, R2, T3^R3, R3]`.
Confirmed fields include the millisecond timestamp as masked uint64LE, masked host text,
and the raw URL's UTF-8 bytes plus a four-byte salt hashed with XXH32 seed zero. The URL hash
is unpadded lowercase hexadecimal ASCII, so it can have fewer than eight characters.
Do not normalize the URL or assume a fixed-width hash.

Two encrypted collection vectors carry additional typed, key-masked state. The
eight-byte request mask is `VoJ`; the eight-byte request random input is `VoR`.
With the public SDK constant
`C = 17e88cde461c0050af4fb3d230d667ccdb31ff62d513a6deac996ab4d2223966`,
their keys are:

- Custom stream: `C[16:] + VoJ + VoR`.
- Salsa20: `C[:16] + VoR + VoJ`, with nonce `C[16:24]`.

The custom stream starts from the SDK's fixed 256-byte permutation and applies
its key-dependent swaps and output selection; it does not use a standard RC4
key-scheduling pass. Both encrypted results are subsequently XOR-masked with
repeating `VoJ`. Their checksums are little-endian XXH32 of the **unmasked
ciphertext**, then masked with `VoJ`. Vector 12 uses custom encryption and its
checksum is vector 13; vector 15 uses Salsa20 and its checksum is vector 11.
The root's special key check uses `Adler32(VoJ) XOR 3084883394`.

These ciphers alone do not make a fresh signature: decrypted values include
request URL and RI hashes as well as masked device state. Refresh must update
those values before encrypting again. Captured fingerprint values are inputs,
not properties derivable from the account cookie.

The inner URL hash differs from the root hash: the custom collection uses
unpadded lowercase ASCII `Adler32(raw_url UTF8 + VoR[4:])`. Its RI check is
`XXH32(full RI hex text) XOR uint32LE(VoJ[:4]) XOR 3284639437`.

DAT independently transforms eight random samples using `floor(256*x)`, producing 16 lowercase
hex characters. The cached token branch JSON-decodes `shopee_webUnique_ccd`, selects session
before local storage using JavaScript truthiness, then applies the vendor string/ASCII gate.
It does not create or refresh a token. HTTP control-character validation remains a separate step.

### State rates and offline verification

The custom collection rate uses total path requests and elapsed whole seconds
since the first signed request. Root field5 has a separate per-path window:
`60 * window_count / max(60, floor(window_age_ms / 1000))`. On the next request,
a window older than120000ms resets to count1 and a new anchor. These rates must
not be conflated; independent original-SDK fixtures cover the415-second case.

The SDK source SHA-256 is
`8279065013fbb6b7b5f8c3f48a9f6efb7b1ddb346ddf59b7abc85572eb280094`.
REA static analysis evidence includes
`ev_24b42a5d82813baed30bdc77fd00bfb8257a62c59cba641f0ae6f8c99fa575b6` and
`ev_dafda97e93ecc3601e71f5e9f153513dd78153c8ed2f5802a0c7a39c7b2ea7b3`.
Static graph evidence is separate from original-SDK runtime fixtures and live
server acceptance. Synthetic fixtures cover complete headers, consecutive
requests, aged windows, nonzero device facts, nonempty tokens and malformed
input; they contain no account cookies or real signing tokens.

Final local verification: **598 tests passed**, with **94% statement coverage**
across the twelve signing, state, transport and client modules. This includes
offline CLI tests and independent original-SDK known-answer comparisons. The
two live product successes above are recorded separately from the test suite.

`python -m shopee_affiliate.sap` remains an offline reconstruction/inspection
utility. The older `direct.py` cookie-only/Node modes and browser-assisted
`internal.py` are retained research alternatives. Use `signed_client` for the
verified pure-Python path. Detailed source traces and historical negative
experiments remain under ignored `output/signature-reconstruction/` and
`output/signing-research-20261028/`.

## Legacy browser-assisted client

`shopee_affiliate/internal.py` is an **experimental browser-assisted client** for the internal
product endpoint. It does not need Open API permission or an App ID/Secret. It loads the affiliate
list page once, then sends product GETs through the website's installed XMLHttpRequest hook.
This legacy route requires Chrome. The verified `signed_client` above is browser-free.

Use an existing Shopee VN cookie export stored locally, not pasted into chat or source code:

```bash
.venv/bin/python -m shopee_affiliate.internal 26609048170 \
  --cookie-file .auth/cookies.json --inspect-signing \
  --out output/product.json
```

The cookie file may contain a JSON cookie array, Playwright storage-state JSON with `cookies`,
or one raw `Cookie` header value such as `name=value; other=value` (without the `Cookie:` prefix).
Only cookies scoped to `shopee.vn` or `affiliate.shopee.vn` are accepted. Raw header cookies are
restricted to the affiliate host. Local/session storage from storage-state exports is not imported.
These formats are not equivalent to transferring a complete browser profile or device state.
Keep real cookie files inside ignored `.auth/` and restrict file access, for example with
`chmod 600 .auth/cookies.json`. The script reads the file but never exports or saves fresh cookies.

Alternatively, with `SHOPEE_COOKIE` already set locally:

```bash
.venv/bin/python -m shopee_affiliate.internal 26609048170 --cookie-env
```

Supplied cookies use an ephemeral Chrome context. Without a cookie source, the client uses the
existing dedicated `.auth/shopee-profile` and permits manual login. JSON goes to stdout and the
optional output file; progress and signing diagnostics go to stderr. Diagnostics show only header
names and lengths. The output can include your account-specific affiliate link.

Requests use the fixed affiliate origin, numeric product IDs, a timeout and 4–9 second spacing.
The client stops on HTTP 401/403/429, redirects, invalid data and API error `90309999`.
It does not retry a blocked request or automatically switch to a different transport.

### What the signing investigation established

- Captured SDK `8568.e933698b.js` exports `generateSignEntry(url, body)`, `hookInit` and `hook`.
  XHR/fetch hooks call the signer and add the resulting headers. The SDK obtains its additional
  `af-ac-enc-sz-token` separately.
- The core signing routine runs in an obfuscated VM. The inspected SDK references browser/device
  APIs, clocks, randomness and storage, whose relevant transformations are reconstructed in the pure-Python path above.
- Prefilling `x-sap-ri` or `x-sap-fixme` can skip the hook's fresh-signing path. The Python client
  sets neither; it lets Shopee's SDK construct each request.
- A live in-page XHR test generated `x-sap-ri` (52 characters), `x-sap-sec` (1,424),
  `af-ac-enc-dat` (16), `af-ac-enc-sz-token` (127), and `x-sz-sdk-version` (7).
  The XHR reported HTTP 200 but a payload with `error: 90309999`, without product data.
  This was the initial XHR experiment; subsequent comparisons are recorded in the browser-free
  section above. No credential or signing values were retained in the research output.

**This older direct-XHR route remains unverified.** Generating these headers does not prove the server will
accept an automated request. The existing passive page collector succeeded earlier, but the new
direct-XHR Python workflow has not completed a successful live fetch. Offline tests cover its
request construction, cookie handling, rejection paths and cleanup, not Shopee acceptance.

Research artifacts under `output/signing-research-20261028/`:

- `static-findings.md`: source locations, signing call path and unresolved dependencies.
- `live-signing-result.json`: redacted metadata from the rejected XHR test.
- `manifest.json` and `scripts/`: source provenance and inspected code.
- `static-partial-evidence.json`: REA partial Evidence
  `ev_13e569676cfb5b4f722b7cb4a8b724b6309498cab5a914c8b13954d3fe408239`.
  REA hit its 60-second per-source analysis limit on the SDK; full semantic recovery is incomplete.
  Manual source findings are separate from the partial REA graph.

## Legacy browser collector: product ID

From the project directory, with Python 3.10+ and Google Chrome installed:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python get_product.py 26609048170
```

Sign in manually in the Chrome window opened by the script on its first run. It uses a
dedicated profile at `.auth/shopee-profile`, separate from your normal desktop Chrome.
Later runs reuse that profile. Close any other script using that profile before starting.
Use `--profile-dir /absolute/path/to/dedicated-profile` to choose another dedicated profile.

The script opens the product page and captures its own matching JSON response. It saves
`detail_26609048170.json` (raw response) and `rows.json` (normalized fields) in the printed
`output/run-<timestamp>/` directory. Multiple IDs are accepted and deduplicated:

```bash
.venv/bin/python get_product.py 26609048170 24995347301 --out output
```

The raw response includes product images, variant axes and full channel commission breakdowns.
The normalized row includes price in VND, stock, sold counts, rating, shop and commission.
Real run outputs can include account-specific affiliate links; keep the ignored `output/` and
`.auth/` directories private. `--headless` is optional after signing in, but headed Chrome is
the default. Verification challenges and rate limits stop the script; it does not solve them.

The reusable Python entry point is `shopee_affiliate.probe.fetch_detail_passively(page, item_id)`.
Pass a Playwright page in a dedicated logged-in context. It returns the validated raw payload;
normalize with `normalize_detail(check_api_payload(payload))` from `shopee_affiliate.parse`.

## Verification on 2026-10-28

Chrome's own requests returned HTTP 200 and `code: 0` for both pages:

| Page | Observed request | Result |
|---|---|---|
| Product detail | `GET /api/v3/offer/product?item_id=26609048170` | Matching product and shop `89827191`; no shop ID or trace parameter needed in this request |
| Product list | `GET /api/v3/offer/product/list?list_type=0&sort_type=1&page_offset=0&page_limit=20&client_type=1` | 20 items, `total_count: 500`, requested product first |

The detail response reports 48,500 VND, stock 27,108, rating about 4.79066, default commission
22.5%, and estimated commission 10,913 VND. These are capture-time values and may change.
The list's 500 total is an observation for this query, not a verified universal API limit.
Request headers included `Affiliate-Program-Type` and Shopee's dynamic signing header names.
No cookies or signing values were exported. Signing requirements were not isolated experimentally
in this run; the collector lets the normal website construct its own request.

REA 6.4.0 inspected sanitized, product-only HAR projections offline. Live request observation was
performed through Chrome CDP; REA did not independently contact Shopee or authenticate the capture.
The list event buffer reported truncation of surrounding events, but its matching request and
response were both retained. Only the selected transactions were analyzed.

Local evidence and example outputs are in `output/rea-20261028/`:

- `product-detail.sanitized.har`, `product-list.sanitized.har`: credential-free scoped captures.
- `rea-evidence.json`, `rea-list-evidence.json`: complete REA evidence records and limitations.
- `product-26609048170.json`, `product-list.json`: product-only response projections.
- `rows.json`: normalized detail output from the captured response.

REA evidence IDs:

- Detail: `ev_7db72eae2b73f2328b61199bd6a9ab8340b41af771a05864e73712caf65e59a1`
- List: `ev_85238e13041a77bb030754bfa4a043bdf0c9aeb07275b29a9009540033d96e45`

The Python collector was tested offline, including login timing, response identity, malformed
responses, verification blocks and cleanup. Parsing was also checked against the live capture.
All 83 tests passed; line coverage across `get_product.py`, `probe.py` and `parse.py` was 93%.
The pre-existing Open API client is outside that scoped coverage figure.
A complete standalone Python run against Shopee still requires login in its separate profile;
the logged-in browser used for this investigation is not automatically shared with the script.

## Earlier investigation

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
    "total_count": 500,   // observed total for this query; universal cap not established
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

## Runtime acceptance observations

The SAP SDK uses an obfuscated VM and installs XHR/fetch hooks. The separate vendor wrapper
adds token headers and reports version `1.12.21`. Several internal algorithms are now reproduced
in Python above. The client reuses exported device facts rather than implementing a complete browser collector.

Earlier investigation notes from 2026-10-27 recorded one successful scripted list request and
later verification redirects after additional calls. Those observations do not establish the
server's scoring rules, a safe request frequency, or that duplicates caused the redirects.
The `scene=crawler_item` URL label does not by itself identify the rejection mechanism.
Later 2026-10-28 scripted XHR and direct HTTP comparisons returned `90309999`, while normal page
loads succeeded. The later corrected pure-Python client succeeded, as documented above; these earlier failures do not describe its current result.

The existing browser collector passively reads the page's own responses where possible and stops
on login/verification failures. Its pacing settings are implementation choices, not proven limits.
The standalone reconstruction CLI makes no HTTP calls. A list response reporting 500 total items
does not establish a universal cap or justify treating alternate queries as a tested workaround.

## Test script

`shopee_affiliate/probe.py` is the earlier browser-based collector:

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

## Without login cookies

Tested 2026-10-28 with item `26609048170` (shop `89827191`): one plain request per endpoint, no cookies.

| Request | Result |
|---|---|
| `GET affiliate.shopee.vn/api/v3/offer/product?item_id=…` | 403, `{"is_login":false,"action_type":2,"error":90309999,"redirect_to_error_page":true}` |
| `GET shopee.vn/api/v4/pdp/get_pc?item_id=…&shop_id=…` | 403, same body |
| `GET shopee.vn/api/v4/item/get?itemid=…&shopid=…` | 403, same body |
| `GET shopee.vn/product/89827191/26609048170` (HTML) | 200, but an empty app shell: no JSON-LD and no item data |
| `POST open-api.affiliate.shopee.vn/graphql` (unsigned) | 200, `[10020] Invalid Authorization Header` |

The web app's internal APIs need a logged-in session. Shopee rejects anonymous calls on purpose, so these anonymous probes do not establish whether exported cookies plus freshly generated
headers will work. The corrected cookie-plus-state Python client above has since succeeded with fresh signatures.

## Open API client

`shopee_affiliate/openapi.py` calls the official Affiliate Open API with App ID and Secret instead of cookies.

- **Endpoint:** `POST https://open-api.affiliate.shopee.vn/graphql`
- **Auth header:**
  `SHA256 Credential={AppId}, Timestamp={unix}, Signature={sha256_hex(AppId + Timestamp + Payload + Secret)}`.
  `Payload` is the exact request body. The client serializes it once and signs and sends that same string.
- **Query:** `productOfferV2(itemId, shopId, keyword, productCatId, listType, sortType, page, limit)`.
- **Units:** prices are plain VND. Rates are fractions (`"0.225"` means 22.5%); the client converts them to
  percent. Field names and units come from Shopee's Open API docs; check them against the first live
  response.

```bash
export SHOPEE_AFFILIATE_APP_ID=... SHOPEE_AFFILIATE_SECRET=...
.venv/bin/python -m shopee_affiliate.openapi product --item-id 26609048170
.venv/bin/python -m shopee_affiliate.openapi search --keyword "ốp lưng" --limit 20
```

Exit codes: 0 OK, 1 API or network error, 2 missing credentials or bad arguments.
