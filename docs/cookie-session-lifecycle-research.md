# Cookie-session signing and token lifecycle investigation

Date: 2026-11-02. Scope: the retained Shopee VN Affiliate SDK, existing Python
signer, and managed worker. This investigation made no live Shopee requests,
read no account credentials, and changed no production implementation.

## Conclusion

Cookies alone are insufficient for the current implementation. A usable session
needs coordinated cookies, initialized device identity, a server-issued risk
token, and cumulative SAP signing state. The existing signer reproduces fresh
signatures. At the time of the initial investigation, token renewal and atomic
propagation were incomplete. Response-cookie persistence and atomic browser
session replacement were implemented afterward; see the update below.

The nearest testable implementation is a coherent browser-derived snapshot
followed by Python signing. Fully browser-free renewal additionally requires
the actual device-report collection/issuance path and evidence of the refresh
trigger after an initially successful report. Neither is established here.

## New original-SDK observations

The offline probe runs the hash-verified original SDK with synthetic browser
state, an observation hook, and fake XHR. Each case begins in a separate VM.
The success and two failure cases each perform eight follow-up calls.

| Operation | Successful report | Failed report, with or without cached token |
| --- | --- | --- |
| `getDegradedData()` / `getShortToken()` | Returns selected token; no new report | Returns cached token or empty string; no new report |
| `getLongToken()` | Resolves to the exact previously posted device-report body; no new report | Same behavior |
| `getLongToken()` after a one-hour lab-clock advance | No new report | No new report |
| Repeated `initSdk(1)` or `init()` | Returns without another report | Sends another report |
| Change mock response to success, then call `initSdk(1)` | Does not contact mock issuer again | Sends a report, replaces selected token, moves from step 7 to step 2 |
| Initialize again after recovery | No new report | No new report |

Thus neither the long-token getter nor repeated initialization after success is
demonstrated to be a renewal mechanism. Failure-path initialization is a verified
retry entry point in this environment. The long-token result must not be confused
with the short server-issued risk token used in the product header.

These are bounded runtime observations, not proof of server token lifetime.
The facade suppresses interval scheduling and event listeners; no interval
registrations were recorded in these cases. It does not reproduce a real browser
fingerprint, page-level navigation/reload, all timer branches, or backend policy.
The website's vendor wrapper is absent, and the synthetic cookie jar does not
enforce expiry. Synthetic device reports were never transmitted. Clock changes
are lab inputs. All tested actions ended with zero pending timers and zero
timer errors; explicit initialization caused the observed retries.

Reproduce from the repository root:

```sh
node output/rea-cookie-lifecycle-20261102/probe-refresh-triggers.cjs
```

The probe asserts report counts, long-token/report-body equality, and recovery
state changes. Its output contains synthetic metadata and equality flags.

## What is already reconstructed

`token_lifecycle.py` models report-response ingestion, token selection, storage
updates, and cookie-write data. Report success uses numeric code 0 and a nonempty
`data.riskToken`. Its raw setter has a separate length gate. Successful ingestion
updates `shopee_webUnique_ccd` and the device-bound `ds` compatibility digest.
The modeled 365-day cookie persistence is not a server validity guarantee.

`session_signer.py` maintains cumulative signing history. Existing documentation
records isolated live successes on October 28 and repeated uncached failures on
November 2. Those historical results are not new live verification in this pass.

## Historical gaps before the managed-session update

The following table records the investigation baseline, not the current release.
The transport, repository, and worker gaps involving response cookies are now
addressed. Cookie and signing-state replacement uses a version check and lease
fencing; disabled accounts cannot be reactivated by an automatic refresh.
The CLI captures an accepted normal Chrome product request, transfers its
headers and scoped cookies privately, and verifies the replacement before use.
It can watch account health through SSH or a local Docker container.
See [the current workflow](managed-actor.md#refresh-from-your-current-chrome-through-a-local-cli).
The separate device-report issuer and in-place token-transition reconstruction
remain research work. Local live verification plus a cached Actor delivery
succeeded on November 2; sustained uncached production acceptance is not implied.

| Component | Observed gap |
| --- | --- |
| `session_signer.py:142` | Copies the seed headers and regenerates SAP/DAT without ingesting a refreshed token. |
| `sap_refresh.py:156`, `sap_collections.py:271` | Refreshes request state while preserving the captured embedded token; no new-token input exists. |
| `http_transport.py:41`, `http_transport.py:134` | Returns status/body and discards the temporary session's response headers/cookie jar. |
| `cookies.py:68` | Imports scoped cookies, but does not import the token SDK's browser storage state. |
| `repository.py:230`, `repository.py:311` | Persists reserved signatures and response counts, not token/cookie lifecycle transitions. |
| `worker.py:34` | Receives product data and response completion information, without renewal state to persist. |
| `token_lifecycle.py:124` | Requires an issuer response supplied by its caller; does not obtain it. |

Updating only `af-ac-enc-sz-token` would leave the token inside SAP inconsistent.
Cookie updates alone also cannot supply the device identity and signing history.

## Concrete implementation sequence

1. Define a versioned session snapshot containing scoped cookies, the actual
   initialized device UUID, token SDK state, SAP seed/counters, SDK hashes, and
   capture time/provenance. Establish a settled capture boundary; a single
   in-flight request cannot reveal other pending page responses.
2. Implement an offline old-token-to-new-token transition. Update the outgoing
   token header and embedded SAP token together, rebuild enclosing checksums,
   and apply observed cookie/storage updates. First obtain independent SDK
   fixtures with different nonempty token lengths.
3. Atomically persist the complete snapshot with account-version and lease
   fencing. The existing account-replacement transaction is a useful starting
   point: it replaces encrypted credentials and invalidates stale leases/cache.
   Preserve reserve-before-send and exactly-once response accounting.
4. Retain relevant response cookie changes with correct host/domain/path,
   deletion, and expiry semantics. Preserve unrelated cookies and duplicate
   names in different scopes. Determine any issuer cookie changes from actual
   observations rather than assuming the report response sets them.
5. Observe a legitimate browser session across startup, successful operation,
   token change, and expiry/recovery. Correlate report requests, SDK state,
   header token, embedded token, and cookie changes using redacted equality
   labels. This must establish which page/SDK event requests a new token after
   initial success. Do not invent a refresh interval from cookie expiry.
6. Verify repeated uncached product requests before and after a genuine token
   transition, then across a worker restart. Report API code and product identity
   separately from HTTP status and cached Actor output. Stop on verification,
   authentication, or rate-limit responses rather than treating retries as proof.

Acceptance tests should cover complete original-SDK signature parity across
token transitions; cookie scoping/deletion; atomic rollback and stale refresh
rejection; concurrent workers; timeouts without counter rollback; exactly-once
response counts; and restart consistency. Offline parity and live acceptance
remain separate milestones.

## REA evidence and limitations

REA 6.4.0 analyzed an unmodified copy of the retained public token SDK:

- Source: `6493.255af8eb.js`, 308,468 bytes.
- SHA-256: `8dd8857d5915dcfdd8fab4238faba13cc4909ea45c68898b0adf4ead0fc80dcc`.
- Evidence: `ev_c81e555278ea7395c14e075a2bf74b7b6bf159cc672729ce76e75fe1739045ba`.
- One JavaScript file parsed, zero parse failures, 81,227 AST nodes visited.
- Semantic coverage is **partial**: the isolated analyzer exhausted its
  configured heap. Static relationships and runtime reachability are unresolved.

The full record and a compact projection are in
`output/rea-cookie-lifecycle-20261102/rea-evidence.json` and `rea-summary.json`.
`provenance.json` binds the source hash; `refresh-triggers.json` records the
separate synthetic runtime evidence. REA did not establish the runtime results.
The SDK was retained from October 28, not freshly downloaded in this pass.
An initial empty-source analysis yielded no useful evidence; it supports none
of these conclusions. Full-SDK analysis above supersedes that setup attempt.

Validation: 188 focused existing tests passed across token lifecycle, session
signing, collector deltas, and signed-client behavior. The new probe's assertions
also passed. Production changes, deployment, and sustained live acceptance are
not part of this research result.

## Chrome live follow-up, 2026-11-02

The user subsequently authorized observation in Chrome. This follow-up used the
existing logged-in browser through native UI and DevTools, with normal navigation
and search. No cookies or token values were exported. This section supersedes
the opening no-live-requests scope for the follow-up only.

1. The product list rendered normally. DevTools recorded an HTTP 200 request to
   the device-report endpoint during startup capture. This is not a verification
   of its JSON success envelope.
2. On the settled list page, the cookie token was 127 characters and matched
   both session storage and local storage.
3. Clicking product 26609048170 opened the website's new tab. Its details rendered
   with price 48,500 VND. Returning to the original tab showed a changed shared
   cookie token, now 131 characters. Local storage matched it; the original tab's
   session storage did not. Child initialization is a plausible source of the
   change, but its issuer response was not captured.
4. Two normal searches in the original tab, for 26609048170 and 52253929694,
   each produced `/api/v3/offer/product/list` HTTP 200 / API code 0. Temporary
   observation wrappers forwarded the original XHR calls and recorded only
   lengths, response codes, and equality labels.
5. For the second search, header token = T1, cookie token = T2, session-storage
   token = T3, local-storage token = T2. All three labels represent distinct
   strings within that observer instance. The header was 131 characters;
   `x-sap-ri` was 52 and `x-sap-sec` was 1428 characters.

**Design correction:** do not impose unconditional equality between request
header, cookie, and every storage cache. A normal browser request succeeded
while these differed. The SDK's active selection state must be captured and
preserved separately from shared browser persistence. The offline step-2
in-memory selection behavior is a possible explanation, not a directly observed
live in-memory result. Header/embedded-SAP consistency still needs independent
verification; the live SAP payload was not decoded in this pass.

This demonstrates bounded live cookie-backed operation through Chrome. It does
not establish browser-free renewal, matching search contents, server-side token
expiry, or sustained acceptance. Resource Timing reported zero report entries
even though DevTools showed a report, so its counts must not be used to infer
absence of issuance. The temporary XHR wrappers were restored and their stored
token references removed, with `OBSERVER_REMOVED` verified in the console.

Redacted observation record:
`output/rea-cookie-lifecycle-20261102/chrome-live-summary.json`.

## REA follow-through: wrapper timing and active token selection

The next investigation analyzed the exact vendor integration factory `at`,
JavaScript UTF-16 offsets 411277..425077 (end-exclusive) of
`chunk-vendors.03fbad24.js`. Source SHA-256:
`8274a9d5002226564d519f585999be07b0fd12af2682bba095429f59a29c146d`.

REA evidence:
`ev_0433cc44ff631c298e844d633cb90f2a43fd1312ef586ee1022fda968b09c1b9`.
The focused run parsed 13,801 bytes and visited 4,910 AST nodes with zero parse
failures and no truncation. Its overall semantic coverage is **unknown**, with
partial relationship families and unresolved free references. Successful parsing
does not mean the application or its runtime has been fully recovered.

### The wrapper has two different readiness states

Manual source tracing of the source-bound extraction establishes:

| Source range, UTF-16 | Behavior |
| --- | --- |
| `B`, 411676..412706 | `T = ot()` loads the module; after T resolves, assigns E and starts `D = E.initSdk(...)`. Returns without awaiting D. Skips initialization when T or D already exists. |
| `K`, 414405..414953 | Awaits T, then calls `getDegradedData()`. Does not await D. Returns only a nonempty ASCII string, otherwise empty string. |
| `$`, 412982..413520 and `V`, 413565..414328 | Client-check paths explicitly wait on a race between D and timeout promises. These have different readiness behavior from the short-token getter. |
| XHR send assignment, 422175..424702 | Calls initialization, then token selection, but retains an already supplied nonempty token header. Repeated initialization calls therefore do not necessarily issue another token. |

Earlier wording calling T an "SDK-ready promise" was imprecise: **T means the
module is loaded, not that the device report has completed**. The inspected
initializer does not reset T/D in its catch branch. This is a bounded source
observation, not proof that no other application component can recreate it.

`wrapper-timing.cjs` executes the actual extracted factory and its original
transpiler helpers with a controlled synthetic SDK boundary. Global browser
hooks are disabled. It verifies that:

- The wrapper initializer resolves while the synthetic issuer promise remains
  pending; the short-token getter can return a cached value at this point.
- After the synthetic issuer resolves, the same getter returns the issued value.
- Calling the wrapper initializer again leaves module-load and SDK-init call
  counts at one.

This test establishes wrapper scheduling, not Shopee's live issuer behavior or
the SDK's real collectors. It makes no network requests.

### SDK memory and persistence must remain separate

Five new cases execute the original token SDK with fake report responses,
then change cookie/session/local state independently. All five match the current
Python lifecycle subset:

1. Successful report, followed by three distinct cache values: SDK selects its
   original in-memory issued token.
2. Failed report with populated caches: cookie wins.
3. Failed report with empty cookie: session storage wins.
4. Failed report with empty cookie and session: local storage wins.
5. Raw token setter after successful initialization and external cache drift:
   the new token updates memory and persistence and remains selectable.

This provides a causal offline explanation for possible cache/header divergence.
It does **not** identify which live instance produced T1 in the Chrome test.
There are separate SAP and vendor request hooks, and an already-set XHR token
may survive a later hook. Hook order, duplicate same-name cookies, and the exact
live SDK instance remain questions for a bounded browser trace. Do not infer
active-header ownership from cache equality alone.

### Revised reconstruction contract

A cookie-session implementation should carry these distinct state groups:

- Authentication: scoped login cookies and their actual expiry/deletion rules.
- Device-token state: initialized device UUID, SDK phase, active in-memory token,
  cookie/session/local caches, and module/initialization readiness.
- Signing state: captured device facts, exact token embedded in SAP, request
  counters, completed-response counts, timing anchors, and SDK versions.
- Request assembly: which hook supplied the outgoing token and whether an
  earlier nonempty header is retained. Validate the actual final header and
  embedded payload from an accepted request before using it as a handoff seed.

The implementation must not collapse these groups into a single "latest token".
A captured final request can bind an outgoing token and SAP seed, but it cannot
by itself recreate initialization, outstanding requests, or future issuance.
Browser-assisted renewal is a candidate architecture to validate, not an
implemented or proven unattended service. Full browser-free issuance still
requires real device-report semantics and server acceptance evidence.

The next live experiment should correlate, using opaque per-run labels, the
device-report response, each relevant SDK getter, each request hook's token
assignment, and the final outgoing header. Observe one normal page startup and
product request, then a second-tab startup. This resolves ownership and ordering
without inventing tokens, changing cookie values, or bypassing verification.

Artifacts under `output/rea-token-selection-20261102/` include source provenance,
the full REA record, the timing probe/result, and the original-SDK cache-drift
goldens and comparison script. Reproduce from the project root:

```sh
node output/rea-token-selection-20261102/wrapper-timing.cjs
node output/rea-token-selection-20261102/cache-drift-oracle.cjs
PYTHONPATH=. .venv/bin/python output/rea-token-selection-20261102/verify_cache_drift.py
```

All assertions passed. No production code, credentials, or browser state were
changed by this offline follow-through.

## Live Python acceptance test, 2026-11-02 20:32 Vietnam time

The user requested an actual working check. The test used the existing paired
October 29 cookie/seed export, approximately 381,614 seconds old at start. No
fresh browser handoff or renewal was performed. It initialized cumulative state,
recorded the assumed completed seed response, generated fresh signatures, and
made two direct HTTPS product requests without a browser, cache, redirects, or
retries. Encrypted test-only reservations were flushed before dispatch; received
responses advanced completion state. Production account state was untouched.

| Request | Result |
| --- | --- |
| 24995347301 | HTTP 200, API code 0, matching returned product ID, price 32,500 VND |
| 26609048170, after a six-second pause | HTTP 200, API code 90309999, verification rejection |

Both requests had newly generated RI and SEC values. The test stopped at the
first rejection. The first success establishes a bounded pure-Python lookup
with this saved session; it does **not** establish reliable session continuation.
The second failure does not isolate token lifetime, signing-history mismatch,
request timing, or another server-side check as its cause. In particular, the
age of the export alone cannot explain the failure because its first call passed.

Full local suite: **940 passed, 61 skipped, 15 subtests passed**. The skipped
tests include PostgreSQL-backed integration cases requiring a disposable test
database. Wrapper-timing and five cache-drift comparisons also passed. These
offline results do not override the failed live continuation check.

The redacted result is
`output/lifecycle-verification-20261102/live-result.json`; the bounded test driver
is `live_check.py` in that directory. Its temporary encrypted state was removed.
No raw product response, affiliate links, cookie values, or signing values were
written to the report. Running the driver again makes new live requests and
should not be treated as an offline test.
