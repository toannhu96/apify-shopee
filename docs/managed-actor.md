# Managed Shopee Vietnam Actor

This repository contains a public Apify Actor adapter and a private, Docker-hosted
Python service. The private service owns Shopee sessions, request signing, pacing,
cache and Discord notifications. The Actor owns customer input, dataset delivery
and Apify event charging. No Shopee credential is a customer input.

The source is Shopee Vietnam's affiliate product endpoint. Coverage is limited to
products available to that endpoint. Commissions reflect the managed account,
not a guarantee of what a customer will earn. Source data may be up to 15 minutes
old; each row includes `fetchedAt` and `cacheHit`. Affiliate links and raw source
responses are excluded. The Actor does not discover products or resolve short links.

## Development and verification

Implementation verification on 2026-10-28: 840 tests and 15 subtests passed with
the PostgreSQL integration database enabled; the new-component coverage was 94%.
Both Docker images built, Compose startup/migration/non-root secret mounts passed,
and the actual Apify SDK completed a local run against the API. Apify CLI 1.6.1
validated the input, dataset and output schemas. Runtime dependency auditing found
no known vulnerabilities. The test client emits one upstream HTTPX deprecation
warning. These checks used synthetic data, with no live Shopee or Discord calls,
real charges, production deployment or Git commits.

Use Python 3.14, Docker and a dedicated PostgreSQL 17 test database. Install the
development requirements into a virtual environment:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

PostgreSQL integration tests require `SH_TEST_DATABASE_URL`. They create and drop
isolated schemas in that database. Do not point this setting at production.
Run the complete suite with that variable set, then check coverage:

```sh
.venv/bin/python -m coverage run --source=shopee_actor,shopee_service,shopee_contracts,shopee_affiliate.session_signer -m pytest -q
.venv/bin/python -m coverage report --fail-under=80
.venv/bin/python -m pip_audit -r requirements-actor.txt -r requirements-service.txt
docker build -f deploy/Dockerfile -t shopee-managed-service:local .
docker build -f .actor/Dockerfile -t shopee-product-actor:local .
```

The two runtime requirement files pin the resolved dependency trees. Re-resolve,
audit and test when upgrading dependencies or base-image digests. Runtime images
exclude development tests, browser profiles, local outputs and credentials.

## Server setup

The following commands are deployment instructions; implementing this repository
does not run them on your production server.

1. Copy the source to your server. Run `.venv/bin/python deploy/bootstrap.py` once
   under the operator account that will own the configuration. It creates ignored,
   protected `.secrets/` and `.env` files and refuses to overwrite existing ones.
2. Set `.secrets/discord_webhook` through a private editor. The webhook must be an
   HTTPS Discord incoming webhook. Keep every secret file mode 600 and the directory
   mode 700. Do not paste secrets into shell arguments, issue reports or logs.
3. Run `docker compose build`, then `docker compose up -d db`.
4. Apply the idempotent initial migration with
   `docker compose run --rm api python -m shopee_service.cli init-db`.
5. Start `docker compose up -d api worker`. Check `/health` through the loopback
   endpoint. It verifies database readiness without exposing configuration.
6. Put an HTTPS reverse proxy in front of `127.0.0.1:8000`. Forward only to the API,
   cap request bodies at 64 KiB, and never log Authorization headers. PostgreSQL has
   no public host port. The public Actor refuses plaintext backend connections.

Compose uses the operator UID/GID recorded in `.env` so the non-root application
can read mode-600 mounted secrets. The bootstrap creates these files:

| File | Purpose |
| --- | --- |
| `database_password` | PostgreSQL initialization password |
| `database_url` | Backend database connection string |
| `api_token` | Actor-to-service bearer credential |
| `encryption_key` | Fernet key for stored Shopee credentials and signing state |
| `discord_webhook` | Operator alerts; blank disables delivery during local setup |

The database password is used when the database volume is first initialized.
Changing the file alone does not rotate an existing PostgreSQL role password.
Back up the encryption key separately from database backups: without the key,
stored sessions cannot be recovered. Rotating that key requires re-encrypting
existing rows or re-importing sessions; do not simply replace it.

### Rotate the session-encryption key

Stop every API, worker and account-administration process before rotation. Keep
PostgreSQL running. Leave `SH_SECRET_KEY` set to the old key for this command,
and supply the new Fernet key in an owner-only regular file (mode 600):

```bash
python -m shopee_service.cli rotate-secret-key \
  --new-key-file /operator/new-encryption-key --maintenance-confirmed
```

The command holds the repository lock, rejects outstanding leases or mixed keys,
and atomically re-encrypts all accounts without changing their other fields. It
prints counts only. Repeating it with the same old/new keys recognizes records
already using the new key. Do not clear lease markers blindly if it refuses.
After success, configure every service with the new key before restarting them.

Key rotation does not revoke leaked Shopee cookies: invalidate those sessions and
capture a fresh authorized login. Rotate an exposed API token in both services
and Apify's secret `SH_BACKEND_TOKEN`. For an exposed database password, change
the actual PostgreSQL role password using a trusted interactive tool, then update
the connection URLs. Changing `POSTGRES_PASSWORD` alone does not update an
initialized database.

## Account operations

Account imports contain cookies, accepted signing headers and optional stable
proxy configuration. See [the source-client guide](shopee-affiliate-api.md) for the
accepted export format. Use a seed from the first product request after a page
reload; a recent seed with an unknown request-window anchor is rejected.
Wait for that browser request to finish successfully before exporting. Import
records its completed response once as part of initializing the session.

For a local container import, mount a directory containing only the protected
files needed for that account. Files must be mode 600 and readable by the service
UID. Example, from the repository on the server:

```sh
docker compose run --rm -v "$PWD/.auth:/operator:ro" api \
  python -m shopee_service.cli add-account primary \
  --cookies-file /operator/cookies.json --state-file /operator/signing-state.json
docker compose run --rm api python -m shopee_service.cli list-accounts
docker compose run --rm api python -m shopee_service.cli verify-account primary --item-id 26609048170
```

`verify-account` makes one real Shopee request and activates the session only if
validated product data is returned. Replace an expired session using the same
protected files with `replace-session primary`; then verify it again. Use
`disable-account primary` to stop new work on an account. An optional `--proxy-file`
contains a protected operator-controlled proxy URL; customers cannot supply proxies.
Replacing a session creates a new version and invalidates that account's cache.

An authentication or verification incident pauses the account. Discord reports
the alias and safe action, never cookies, headers, raw responses or webhook URLs.
Rate limits cool down for 15 minutes and are not described as expired sessions.
Successful verification generates one recovery notification per session version.
Notifications use a durable outbox with bounded retries and disabled mentions.
Inspect failed outbox rows locally if webhook delivery is rejected or retries are
exhausted; restore the webhook and reschedule those rows deliberately.

Only one request per account is dispatched at a time. The initial minimum interval
is six seconds, configurable upward with `SH_MIN_INTERVAL_SECONDS`. Reservations
advance encrypted signing state before dispatch; a timed-out request does not
roll the state back. This preserves request history but does not renew Shopee tokens.
Received responses also advance the SDK completion counters, including rejected
HTTP/API responses. This update commits atomically with the job or verification
outcome under the same account lease; stale or duplicate outcomes cannot advance
the counters again. Transport timeouts do not count as received responses.
Managed reservations also advance the browser XHR hook's global and page request
counts and the initialized SDK's signing counter. Regression fixtures cover full
SDK initialization followed by five complete hook/sign/response cycles. They do
not establish server acceptance or model asynchronous browser state changes.

## Actor configuration and billing

Set `SH_BACKEND_URL` to the fixed HTTPS origin and `SH_BACKEND_TOKEN` to the server's
API token in Apify Console. Mark the token **Secret** and leave build-time secret
injection disabled. Never put credentials in `.actor/actor.json`. Local tests can
use a token file and loopback HTTP with `SH_BACKEND_ALLOW_HTTP=1`; cloud runs cannot.

Use limited Actor permissions and hidden source. Configure the `product-result`
pay-per-event event before commercial use. No commercial price is assumed by this
repository. Cache hits are billable successful results, disclosed in the Store
README. Duplicates within a run and failed/unfinished products are not billed.

The Actor validates all input and deduplicates product IDs across IDs and URLs.
Runs are capped at 1,000 unique products and two hours from their original start.
Data rows are in the default dataset; `SUMMARY` in the default key-value store
contains failures, unfinished IDs and delivery/billing uncertainty.

Delivery and charging are separate external operations. Stable result IDs and a
durable backend journal prevent blind replay. If a dataset write or charge has an
unknown outcome, the Actor reconciles existing rows and conservatively records
uncertainty. It may leave a delivered row unbilled rather than charge twice. Do not
manually restart an uncertain charge without checking the original run's billing.
Apify's charge-idempotency window is only three minutes, not indefinite protection.

## Recovery, retention and release

Customer runs use the same backend operation IDs after migration. The deadline
does not reset. Cancelling one run does not cancel work still needed by another.
Completed results survive partial failure. With no usable sessions, valid cache
hits remain available and misses fail promptly instead of waiting for manual login.

The worker removes expired cache entries and completed operation records older
than seven days; it does not remove active runs. Apify datasets have their own
customer-controlled retention. Back up PostgreSQL and operator secrets separately.
Pause workers before restoring backups, then replace/verify sessions: restoring an
old database also restores old signing counters, which must not be reused blindly.

For rollback, stop dispatch, retain the database and use the previously tested
image plus its compatible schema. Never run `docker compose down -v` as routine
rollback: that removes the database volume. Future schema changes require explicit
versioned migrations and compatibility review.

Before publication, validate actual Linux egress and sustained signing with your
account: small lookups, successive time windows, session replacement, mixed cached
and fresh batches, then a 1,000-product run. Measure availability, latency, session
lifetime and cost before choosing the event price. The 10,000/day goal is not a
verified capacity claim; additional accounts may be needed.

Prepare a small scheduled Apify health task (one product), a missing-product task,
and a duplicate-input task. Monitor run status, schema, errors and result count;
use less frequent batch tests to limit costs. Configure schedules only after
deployment and live acceptance. No production Actor, webhook test message or
schedule is created merely by running the offline suite.

References: [Actor definitions](https://docs.apify.com/actors/development/actor-definition),
[input schema](https://docs.apify.com/actors/development/actor-definition/input-schema),
[output schema](https://docs.apify.com/actors/development/actor-definition/output-schema),
[permissions](https://docs.apify.com/actors/development/permissions),
[state persistence](https://docs.apify.com/sdk/python/docs/concepts/actor-events),
[event charging](https://docs.apify.com/sdk/python/docs/concepts/pay-per-event),
[secret environment variables](https://docs.apify.com/actors/development/programming-interface/environment-variables),
[Store publication](https://docs.apify.com/actors/publishing/publish).
