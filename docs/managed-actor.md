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
docker build -f deploy/Dockerfile.api -t shopee-api:local .
docker build -f deploy/Dockerfile.worker -t shopee-worker:local .
docker build -f .actor/Dockerfile -t shopee-product-actor:local .
```

The two runtime requirement files pin the resolved dependency trees. Re-resolve,
audit and test when upgrading dependencies or base-image digests. Runtime images
exclude development tests, browser profiles, local outputs and credentials.

### GitHub Actions and container images

The API and worker each have their own Dockerfile and independent GitHub workflow:

| Image | Dockerfile | Workflow |
| --- | --- | --- |
| `shopee-api` | `deploy/Dockerfile.api` | `.github/workflows/docker-api.yml` |
| `shopee-worker` | `deploy/Dockerfile.worker` | `.github/workflows/docker-worker.yml` |

Each workflow runs on pushes to `main` and manual dispatches. The publish job
builds and pushes its image directly. Python tests, linting, coverage and dependency
audits are run locally using the development commands above, not in these
workflows. GitHub Actions are pinned to commit SHAs. Action and Docker base-image
updates are managed manually.

Only successful runs on `main` publish images: `ghcr.io/<owner>/shopee-api`,
`ghcr.io/<owner>/shopee-worker`. The owner is
lowercased. Each image receives `latest` and `sha-<full-commit>` tags, build
provenance and an SBOM. Images retain the existing `linux/arm64` deployment
architecture and build on native ARM runners. They are not x86 images.
Apify continues to use `.actor/actor.json` and `.actor/Dockerfile` separately.

The API and worker Dockerfiles each define their own startup command and can be
built directly without a target flag. Compose builds the two separate images;
its shared settings only cover runtime configuration. Both run as UID/GID 10001.
Application code stays root-owned; the Actor's `/app/storage` is writable.
Configure the API's `/health` probe in the deployment platform; Compose already
does this. Runtime credentials must come from secret files or runtime environment
variables, never image build arguments.

Optional CapRover deployment lives inside each service's workflow; no separate
`deployment.yaml` is used. Deployment is disabled until the repository variable
`CAPROVER_DEPLOY` is set to `true`. First configure the `production` GitHub
environment and its protection rules, then add `CAPROVER_HOST`,
`CAPROVER_API_APP`, `CAPROVER_API_APP_TOKEN`, `CAPROVER_WORKER_APP`, and
`CAPROVER_WORKER_APP_TOKEN` as environment secrets. The API workflow only deploys
the API app, and the worker workflow only deploys the worker app. These must
identify this project's apps, not the copied Nudgen app settings. Configure
CapRover's GHCR pull credentials for private images, port 8000 for the API, and
the runtime secrets/database for both apps. The worker image already has the
worker command. Deployment uses the commit tag and does not run migrations.
After a successful deployment, each workflow sends a Discord notification with
the image name, full GHCR commit tag and commit message. Set `DISCORD_WEBHOOK`
as a repository or `production` environment secret to enable notifications; an
unset webhook skips the notification. Mentions are disabled and messages are
limited to 2,000 characters. Provision the schema with the server setup steps before
enabling deployment; schema changes still require a coordinated migration and
rollback plan.

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
   cap ordinary request bodies at 64 KiB, and never log Authorization headers or
   request bodies. The optional operator session route needs the larger limit
   described below. PostgreSQL has
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

### Recover a database password and session key together

When both credentials need rotation, stop all API, worker and account-admin
processes. The maintenance helper runs inside the service network using the
current `SH_DATABASE_URL` and `SH_SECRET_KEY`, plus privately supplied
`SH_NEXT_DATABASE_PASSWORD` and `SH_NEXT_SECRET_KEY`:

```sh
python -m shopee_service.recover --maintenance-confirmed --wait
```

It re-encrypts stored sessions and changes the connecting PostgreSQL role's
password in the same transaction under the repository lock. Outstanding leases,
mixed or unknown encryption, and invalid replacement inputs abort recovery.
The raw password is converted to a SCRAM verifier locally before the SQL command.
Only `CREDENTIAL_RECOVERY_COMPLETE` with numeric counts establishes success.
With `--wait`, the container stays idle after either success or failure.

After success, stop the maintenance container, update both service configurations
to the new password/key, and update PostgreSQL's configured password to match.
Replace the separate API bearer token in the API, worker and Apify as needed.
Restore the normal worker command before starting services. On a retry after an
uncertain outcome, retain the same old/new inputs: the helper can reconnect with
the replacement password and recognize rows already using the replacement key.
Never clear lease markers or discard account data to force a recovery through.

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
validated product data is returned. It also stores that validated, allowlisted
product in the same 15-minute cache used by the worker, under the verified account
version. An Actor request for that product can reuse this result without issuing
an immediate duplicate Shopee request. The result retains its original `fetchedAt`
and is delivered with `cacheHit: true`. Failed or stale verifications do not write
cache entries. This reuse does not establish acceptance of later uncached requests.
Replace an expired session using the same
protected files with `replace-session primary`; then verify it again. Use
`disable-account primary` to stop new work on an account. An optional `--proxy-file`
contains a protected operator-controlled proxy URL; customers cannot supply proxies.
Replacing a session creates a new version and invalidates that account's cache.

To change only the outbound proxy, pass a protected JSON file containing exactly
`{"proxy_url":"https://USER:PASSWORD@proxy.nudgen.net:8443"}` to the private CLI.
The managed deployment uses `proxy.nudgen.net:8443`; replace only the host when
updating an existing protected proxy URL, retaining its configured credentials.
The runtime reads the encrypted account configuration, so changing this endpoint
does not require embedding a proxy address or credentials in application code:

```sh
docker exec -i SERVICE_CONTAINER python -m shopee_service.cli set-proxy-stdin primary \
  --expected-version CURRENT_VERSION --item-id 24995347301 < /protected/proxy.json
```

Use the version from `list-accounts`. This operation preserves the stored cookies
and signing state, atomically rejects stale versions or disabled accounts,
invalidates cached results, and performs one verification request after any
existing pacing or lease delay. Proxy credentials stay in encrypted account storage;
they are never accepted through public Actor inputs or the HTTP session refresh route.
An unsuccessful verification leaves the account paused.

When importing cookie and seed files that work with `shopee_affiliate.signed_client`,
use `refresh-session-stdin ... --signing-mode standalone-seed` to match its signing
behavior. Each reserved request calls the same `sap_signer.refresh_headers` with
the original imported headers. The service persists the reservation before HTTP
dispatch; response bookkeeping does not advance that original seed. Response
cookies still update under the account-version fence. The default `cumulative`
mode retains the existing chained signing lifecycle. Proxy/egress acceptance is
independent of the chosen signing mode.

For an explicitly trusted private HTTPS proxy certificate, mount its PEM bundle
read-only and set `SH_PROXY_CA_FILE` to the container path. The bundle must be a
regular, readable PEM file of at most 256 KiB, not a symlink. This setting applies
only to an explicit HTTPS proxy: proxy peer/hostname verification and normal
Shopee HTTPS verification remain enabled. It does not install global trust or
disable certificate checks. A working proxy does not guarantee that Shopee will
accept the account session.

An authentication or verification incident pauses the account. Discord reports
the alias and safe action, never cookies, headers, raw responses or webhook URLs.
Rate limits cool down for 15 minutes and are not described as expired sessions.
Successful verification is silent. Previously queued recovery notifications are
consumed without sending; only failure and session incidents notify Discord.
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

### Refresh directly through the HTTPS API

The browser refresh CLI can send a new session directly to the API. It requires
no local Docker container or SSH connection. Chrome must still be running on
your computer with a loopback debugging endpoint and a signed-in Shopee Affiliate
tab. If login or verification has expired in Chrome, complete it there first.

One-time server setup for the CapRover `shopee-api` app:

1. Deploy the updated service code.
2. Set `SH_SESSION_REFRESH_TOKEN` to a randomly generated operator token of at
   least 32 non-whitespace ASCII characters. It must differ from `SH_API_TOKEN`.
   The API also accepts `SH_SESSION_REFRESH_TOKEN_FILE` for a mounted secret.
   Leave it unset to disable HTTP session administration. Keep this token out
   of Apify Actor inputs and customer-facing configuration.
3. Serve the API over HTTPS. Allow up to 2 MiB for
   `POST /v1/admin/accounts/{alias}/session` and set the reverse proxy's read
   timeout to at least 180 seconds. Verification can wait up to 90 seconds for
   existing pacing before making a Shopee request. Keep Authorization headers
   and request bodies out of proxy/access logs.

On your Mac, load that same token into `SH_SESSION_REFRESH_TOKEN`. To enter it
without placing its value in shell history, use this zsh prompt:

```sh
read -rs 'SH_SESSION_REFRESH_TOKEN?Session refresh token: '
export SH_SESSION_REFRESH_TOKEN
```

Alternatively, use the executable helper, which prompts for the token with hidden
input when the environment variable is unset:

```sh
./scripts/refresh-session.sh
./scripts/refresh-session.sh --watch
./scripts/refresh-session.sh another-account --signing-mode standalone-seed
```

The helper defaults to `primary` and the API URL below. It can be launched from
any directory by using its absolute path. It does not save the token or start
Chrome; the browser debugging endpoint and signed-in session must already exist.
Use `--help` to see defaults and override options.

To invoke the Python CLI directly, run from the repository directory
(replace `primary` with your account alias):

```sh
.venv/bin/python -m shopee_service.browser_refresh primary \
  --api-url https://shopee-api.gcp.nudgen.net \
  --cdp-url http://127.0.0.1:9222 \
  --item-id 24995347301 --once
```

Use `--watch` instead of `--once` to check every 30 seconds and refresh when
needed. Add `--signing-mode standalone-seed` if the existing account uses that
signing lifecycle; the CLI defaults to `cumulative`. Keep Chrome and the CLI
running. The CLI captures cookies and signing
headers into memory and sends them over HTTPS, without writing them to disk.
It refuses plaintext URLs, redirects, and automatic POST retries. After a
timeout, check account status before retrying: the replacement may have committed.

The operator API accepts `Authorization: Bearer <operator-token>`:

| Method and path | Purpose |
| --- | --- |
| `GET /v1/admin/accounts/{alias}` | Returns only `alias`, `version`, `status`. |
| `POST /v1/admin/accounts/{alias}/session` | Replaces and verifies a session with the body below. |

```json
{
  "expected_version": 2,
  "item_id": "24995347301",
  "cookie_source": "<scoped browser cookie export or cookie header>",
  "headers": {"<captured signing header>": "<value>"},
  "signing_mode": "cumulative"
}
```

The browser CLI fills these fields automatically using an accepted browser
request. For a custom caller, read the account version before capturing the
session. Supply all required signing headers, not just cookies. `signing_mode`
defaults to `cumulative`; `standalone-seed` is also accepted for manual imports.
Unknown fields, duplicate JSON keys, invalid headers and oversized bodies are
rejected. Send an accurate `Content-Length` and an uncompressed JSON body.

Success returns `{"alias":"primary","version":3,"status":"healthy","verified":true}`.
A `200` with `verified:false` means the new version was stored but verification
did not complete successfully. Inspect its status before retrying. `409` means
the account was disabled or changed since capture; fetch its current status.
`401` means the operator token is missing/invalid or the feature is disabled.
Admin status requests are limited to 30 per minute and uploads to 5 per minute
per API process, separately from product-job traffic.

Replacement preserves the configured proxy, encrypts credentials at rest,
invalidates cached data for the old session and fences old workers. Verification
is bound to the exact replacement version. Only a successful verification
activates it. The ordinary Actor API token cannot access these operator routes.
Completed Apify runs are not restarted automatically after recovery.

### Refresh from your current Chrome through a local CLI

The local refresh CLI attaches to an already-running Chrome debugging endpoint.
It opens one temporary product tab in the existing Shopee context, waits for a
successful normal product response, and captures that request's signing headers
plus scoped cookies. It closes only its temporary tab. It does not launch Chrome,
read its profile files, or enable remote debugging. Your current Chrome must
already expose a loopback CDP endpoint and have a logged-in affiliate tab open.
A normal Chrome instance without such an endpoint cannot be attached this way.

Install the development requirements on your computer for Playwright. Configure
an SSH host whose identity is already trusted, and deploy the updated service
code before running this command. Replace `shopee-server`, `shopee-api`, and
`primary` with your SSH host, running API container, and existing account alias:

```sh
.venv/bin/python -m shopee_service.browser_refresh primary \
  --ssh-target shopee-server --container shopee-api \
  --cdp-url http://127.0.0.1:9222 --item-id 24995347301 --once
```

Use `--watch` instead of `--once` to poll account health every 30 seconds. Keep
this process running on your computer. It refreshes on authentication,
verification, or pending-session status, with a minimum five-minute cooldown.
It does not guess a token lifetime from cookie expiration. Rate-limit status
uses the service's existing cooldown; it does not trigger browser refresh.
If Shopee asks for login or verification, complete that in Chrome and restart
the CLI. A rejected replacement or another failure before a healthy poll stops
the watcher, preventing repeated renewal attempts.

Cookies and headers travel in memory through SSH stdin to the private admin
command `refresh-session-stdin`. They are not written to files, command arguments,
logs, Actor inputs, datasets, or ordinary product-job API routes. SSH uses noninteractive
key authentication and strict host-key checking. The server checks the account
version captured before browser access, replaces cookies and signing state in
one transaction, preserves the configured proxy, and fences old workers. It
waits up to 90 seconds for existing pacing before making one verification lookup.
Only successful verification activates the account. Longer cooldowns leave it
pending and stop the CLI; inspect health before retrying.

Apify continues using the private backend, so it needs no browser or Shopee
cookies. Runs already returned as `session_unavailable` remain finished; submit
a new run after recovery. Browser success alone does not guarantee that the
service's network/proxy will be accepted by Shopee.

The signed transport also retains scoped `Set-Cookie` rotation and deletion from
received responses. The service saves these changes and response signing counters
atomically under its account lease, including received API rejections. Timeouts
and stale workers cannot overwrite the stored session. Device-token request
headers remain separate from cookies because their values can legitimately differ.

For an existing local Docker service, use `--local-container` instead of the
SSH options. This uses the same private stdin protocol and verification gate:

```sh
.venv/bin/python -m shopee_service.browser_refresh local-primary \
  --local-container shopee-local-20261102-api-1 \
  --cdp-url http://127.0.0.1:9222 --item-id 24995347301 --watch
```

The watcher process must remain running. It never launches or changes Chrome
itself. A dedicated Chrome profile with loopback debugging can be started by
the operator when the normal browser cannot expose a debugging connection;
sign in to Shopee in that profile before starting the watcher.
