# Shopee Vietnam Product Details and Affiliate Commissions

Fetch product and affiliate commission observations from Shopee Vietnam using product IDs or full product URLs. No Shopee login or cookie export is required from Actor customers. The operator maintains the source account sessions.

This Actor covers products accessible through Shopee Vietnam's affiliate product endpoint. It does not guarantee every Shopee listing is available, and it does not support other countries, discovery searches, reviews or short links.

## Input

```json
{
  "productIds": ["26609048170"],
  "startUrls": [],
  "maxItems": 1000
}
```

`productIds` accepts positive decimal identifiers. `startUrls` accepts full HTTPS URLs such as `https://shopee.vn/product/89827191/26609048170` or product-name URLs ending in `-i.89827191.26609048170`. The same item supplied more than once produces one result. At least one product is required, and at most 1,000 unique products may be supplied. `maxItems` selects the first unique products in input order.

URLs are parsed locally into identifiers. Redirects, remote URL-list files, short links and non-Vietnam domains are rejected.

## Output and freshness

The default dataset contains successful products. Identifiers are strings to preserve precision. Prices and commissions with `_vnd` suffixes are Vietnamese dong; `_pct` values are percentages, so `22.5` means 22.5%. Missing source fields are `null`.

Fields include product and shop IDs, name, image identifier, canonical product link, price ranges, stock, sales, ratings, shop name, category, commission amounts and percentages, commission period, and sample eligibility. The Actor does not return raw source responses, account identifiers, cookies, signing state or affiliate-attributed links.

Every row includes `schemaVersion`, `resultId`, `fetchedAt` and `cacheHit`. Results may be reused across customers for up to 15 minutes; `fetchedAt` is the original UTC collection time. A cache hit is billed as a successfully delivered result. Commission values are observations from an operator-managed affiliate account, not a promise that your own account receives the same rate or eligibility.

Download results as JSON or CSV using the normal Apify dataset export controls. The output also links to `SUMMARY` in the default key-value store, which records delivered counts, failed products, unfinished IDs and any delivery or billing outcome needing operator reconciliation.

## Billing and incomplete runs

The paid version uses one `product-result` event per successful, unique product delivered during a run. There is no developer charge for duplicate inputs, unavailable products or failures. The event price is configured and displayed by Apify before running; deployment must not invent a price. Set the run's maximum charge in Apify to control spending.

The Actor limits submitted work to the available event budget and stops when that budget is exhausted. A run has a maximum processing period of two hours from its original start, including restarts. Completed results remain available if the deadline, service availability or account health prevents completion.

Infrastructure cannot atomically append a dataset row and record a billing event. The Actor persists progress and reconciles rows after restart. It never blindly repeats a dataset write or charge whose outcome is uncertain. This conservative behavior can produce an unbilled result or an unfinished product after an interruption. Inspect `deliveryUncertainIds` and `billingUncertainIds` in `SUMMARY`; contact the operator for reconciliation rather than repeatedly resurrecting a failed run.

A fully completed run reports success. Product failures, deadlines and reconciliation problems report an incomplete run while preserving results. Budget stops and requested aborts are reported explicitly in `SUMMARY`.

## Operator configuration and launch checklist

The private backend must already be running. Configure these runtime environment variables in Apify Console:

- `SH_BACKEND_URL`: fixed HTTPS origin of the private service.
- `SH_BACKEND_TOKEN`: secret bearer token matching the backend's API token. Mark it **Secret**. Local development may instead use `SH_BACKEND_TOKEN_FILE`.
- For a private, explicitly unbilled smoke test only, set `SH_ALLOW_UNBILLED_TEST=1`. Remove it before public launch. Local runs are unbilled unless Apify's `ACTOR_TEST_PAY_PER_EVENT` mode is enabled.

Keep source files hidden and select **Limited permissions** in Actor settings. Never put source-account credentials, webhook URLs or tokens in Actor input, build arguments, datasets or source files. Do not enable environment variables during image builds.

Configure a repeating, positive-price `product-result` pay-per-event event before a public run. One-time and tiered events are intentionally rejected. The exact price must be chosen after measuring costs. Use a beta build tag and a platform timeout longer than two hours to leave time for orderly persistence.

Build and run privately, inspect sample output, validate the intended network egress and sustained batches, and complete Apify's publishing requirements before making this Actor public. The initial one-account deployment has no established 10,000-fresh-products/day guarantee. Session health, response times and cache reuse determine capacity. See the repository's deployment runbook for database backups, session updates, alerts and rollback.

Local entrypoint: `python -m shopee_actor`. The Docker image contains the Actor and shared validation code only; all signing and Shopee account state stays in the backend. For an explicitly local loopback backend, `SH_BACKEND_ALLOW_HTTP=1` permits HTTP; this option is rejected on Apify.

## Documentation references

- [Apify Python SDK](https://docs.apify.com/sdk/python/docs/overview)
- [Actor publication requirements](https://docs.apify.com/actors/publishing/publish)
- [Pay-per-event charging and idempotency](https://docs.apify.com/sdk/python/docs/concepts/pay-per-event)
- [Limited Actor permissions](https://docs.apify.com/actors/development/permissions)
