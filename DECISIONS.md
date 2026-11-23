# Decisions

Ambiguities in `plan.md` and how they were resolved during Phase 0, plus
open questions for the project owner. Per the plan rules, each decision
below was written down instead of silently guessed.

## Resolved

### D1: Repository root layout

The requested tree shows a `pytracking/` root containing the files. This
checkout is already named `pytracking`, so the files were created directly
at the repository root (no nested `pytracking/pytracking/`).

### D2: Diagnostics report failures in the JSON body with HTTP 200

`/diag/db` and `/diag/outbound` return HTTP 200 in both success and failure
cases and carry the result in the JSON payload (`{"db": "error", ...}` /
`{"error": ..., "message": ...}`). The Phase 0 acceptance criterion is
"diagnostic reports status", so the JSON body is the report. If you prefer
503 on failure for monitoring purposes, say so and it becomes a one-line
change.

### D3: `/diag/outbound` treats any HTTP response as connectivity success

The check exists to prove outbound HTTPS works from the host.
`https://graph.facebook.com/` itself answers with an HTTP error (the root is
not a valid API call), so any HTTP response, including 4xx/5xx, is reported
as `{"status": <code>, "elapsed_ms": ...}`. Only transport failures (DNS,
TLS, timeout, connection refused) report
`{"error": <ExceptionClass>, "message": ..., "elapsed_ms": ...}`.

### D4: Outbound HTTPS uses the standard library

The rule "no dependencies beyond flask and PyMySQL" rules out `requests`,
so `/diag/outbound` uses `urllib.request` from the standard library.

### D5: `POST /api/v2/ev` is a Phase 0 stub

It accepts any request body and always returns 204 with an empty body and
no response headers. Validation, tenant resolution, dedupe, hashing, and
rate limiting are Phase 1 work.

### D6: The 204 response carries no tracking headers

To keep the 204 empty, the route strips the default `Content-Type` header, and
the tests assert there is no `Content-Type`, `Set-Cookie`, or `X-*` header.
The response does carry `Content-Length: 0`, which is not a tracking header
and is required by this host - see D21, which supersedes the earlier "no
headers at all" wording.

### D7: Tests run on stdlib `unittest`

Only flask and PyMySQL are allowed as dependencies, so
`tests/test_phase0.py` uses `unittest` + `unittest.mock`. The tests are
plain `unittest.TestCase` classes, so `pytest` also works if installed.

### D8: `/diag/db` connection settings

The diagnostic connects with `connect_timeout=5` and PyMySQL defaults,
then closes the connection immediately. The schema work in Phase 1 will
pin `charset="utf8mb4"` for real connections.

### D9: `/diag/*` unauthenticated in Phase 0, removed before Phase 2

`plan.md` line 12 calls `/diag/outbound` an "admin-only route", but no auth or
user system exists until Phase 4. Owner decision (2026-11-22): keep the routes
unauthenticated for Phase 0 so the host checks could run immediately.

**Resolved 2026-11-23:** rather than add auth early, the public diagnostics
were removed once Phase 0 and Phase 1 were accepted over HTTPS (see D23). Only
`/diag/no-content` remains, which reveals nothing about the system.

### D10: Any write-key problem returns silent 204

Missing, empty, non-string, unknown, and inactive write keys all return a
bare 204 with no body. Per the Phase 1 spec, the receiver "must not reveal key
validity", so it cannot distinguish these cases to the caller. Only the event
fields (`event_id`, `name`) produce a 400 on missing/invalid input.

### D11: Origin check fails closed

The request `Origin` header must appear in the tenant's `tenant_origins`
rows. If the header is absent, or the tenant has no configured origins, there
is nothing to match, so the request gets a silent 204 and nothing is written.
Failing open was rejected: it would let anyone post events for an
unconfigured tenant. Owner decision (2026-11-23): keep fail-closed. Add
origins via the admin UI / SQL when onboarding a tenant.

### D12: Migration is idempotent, never destructive

`db/migrations/001_init.sql` contains only `CREATE TABLE IF NOT EXISTS`
statements — no `DROP TABLE`. Re-running the runner is a no-op on an existing
schema and never deletes data. Production migrations that alter existing
tables will be added as new numbered files, not by editing 001.

### D13: Rate-limiting deferred with TODO

Phase 1 plan items include rate limits, but Passenger on cPanel spawns multiple
processes, making an in-process limiter unreliable. A code-level TODO
(`TODO(#rate-limit)` in `passenger_wsgi.py`) defers this to a later phase using
an external store (e.g. Redis-backed token bucket or a MySQL window table).

### D14: Only four tables in Phase 1

Per explicit instruction, Phase 1 creates only tenants, tenant_keys,
tenant_origins, and events. Deliveries, users, and audit_log are deferred to
the phases that consume them.

### D15: The stored payload excludes the write key

`events.payload_json` stores the request body with the `wk` field removed. The
write key is a long-lived secret; persisting it in every event row would leak
it into backups, logs, and the admin UI. `event_id` and `name` are also
promoted to their own columns for indexing.

### D16: Runs on Python 3.13.5, matching the plan

`plan.md` line 1 asks for 3.12 or 3.13. The host's Python panel first showed
3.9.23 and creating the app appeared to use it, but the app's actual
virtualenv (`virtualenv/repositories/pytracking/3.13`) runs **3.13.5** -
confirmed by `diagnose.py` reporting `sys.executable` and `VIRTUAL_ENV`. No
version deviation; the panel label was stale. The code stays 3.9-compatible
anyway, since that costs nothing.

### D16a: Confirmed the Python side is healthy; 404s are vhost routing

`diagnose.py` run in the app's venv prints `flask 3.1.3`, `pymysql 1.2.4`,
our real `passenger_wsgi.py` (12,599 bytes, not cPanel's 488-byte stub),
`marker pytracking-phase1-landing`, and all six routes. Therefore the
LiteSpeed 404s on `my.herbsworld.net` are not application faults: that vhost
never hands the request to Passenger. Diagnosing this took one panel script
run, where HTTP probing could not distinguish "broken app" from "unrouted
vhost" - the reason `diagnose.py` exists.

### D17: No cPanel Terminal - everything runs from the panel UI

The host has no Terminal access. The operational path is panel-only:

- **Pip install**: the "Run Pip Install" button (reads `requirements.txt`).
- **Migration**: `db/run_migration.py` takes the SQL file as an optional
  argument and defaults to `migrations/001_init.sql` resolved from its own
  location, so cPanel's "Execute python script" box works with just the
  script path. Results print to stdout, since the panel may not surface
  stderr.
- **Schema inspection / seeding**: phpMyAdmin.

Consequence: seeding a tenant (`tenants`, `tenant_keys`, `tenant_origins`)
must be done by hand in phpMyAdmin, because the setup wizard is Phase 4.
`smoke.py` works around this for acceptance testing by creating and removing
its own throwaway tenant.

### D18: The 1045 "Access denied" was a truncated cPanel DB user name

The host's MySQL user is cut off mid-word in the panel (`..._du`), which is
normal for cPanel's 10-character prefix limit but looked like a typo. It was
the credential itself that had been reset, not the code. Verified by
`diagnose.py` reporting `connect OK` with all four tables. Lesson: when 1045
appears, confirm the exact user via `diagnose.py` (which prints `DB_USER`)
before touching the application.

### D19: Phase 0 acceptance is proven in-process, not over HTTP

`plan.md` line 27 requires the cPanel smoke test against the real host. The
vhost for `my.herbsworld.net` does not hand requests to Passenger (LiteSpeed
serves the docroot statically), which is a host configuration issue outside
this codebase. `smoke.py` therefore runs the same view functions through
Flask's test client inside the app's own virtualenv, against the real MySQL
database and the real outbound network. That exercises everything the HTTP
route would except Passenger's routing layer, so the application is validated
even while the domain is misrouted.

**Accepted on the real host, 2026-11-23: 8/8 checks passed.**

```
[PASS] GET /healthz returns {"ok": true}
[PASS] GET /diag/db returns {"db": "ok"}
[PASS] GET /diag/outbound reports a status   (status=400, 268.7ms)
[PASS] POST /api/v2/ev returns 204 with no body   (headers=none)
[PASS] the event was written to the database      (1 row)
[PASS] a duplicate event_id stores nothing extra  (still 1 row)
[PASS] the raw client IP is not stored            (64-char HMAC)
[PASS] user_agent and source_origin are stored
```

`/diag/outbound` returning 400 is the expected outcome (see D3): the root of
graph.facebook.com is not a valid API call, but receiving any HTTP response
proves TLS, DNS, and egress all work from the host. The throwaway tenant was
created and removed, so the database was left as found.

Consequence: Phase 0 acceptance and Phase 1 schema/ingestion are proven on the
host. The only unproven link is Passenger's HTTP routing, which is host
configuration rather than code.

**Superseded by D22:** routing was subsequently fixed and the checks now run
over the public HTTPS URL, not only in-process.

### D22: Phase 0 and Phase 1 accepted over the public HTTPS URL

`acceptance.py` posts real HTTP to `https://my.herbsworld.net`, so Passenger,
LiteSpeed, the proxies and MySQL are all exercised - the combination that
exposed the missing-`Content-Length` bug (D21), which the in-process tests
could not see.

Result, 2026-11-23: **12 of 12 checks passed.**

```
[PASS] GET /healthz over HTTPS             200 {"ok":true}
[PASS] 204 framing accepted by the host    204, Content-Length: 0
[PASS] GET /diag/db                        200 {"db":"ok"}
[PASS] valid key returns 204 over HTTPS    204
[PASS] the event landed in MySQL           1 row
[PASS] client IP stored as a 64-char hash  len=64
[PASS] user_agent and source_origin stored
[PASS] consent_state recorded as 1
[PASS] write key absent from the stored payload
[PASS] duplicate event_id stores nothing extra    rows=1
[PASS] bad origin writes nothing                  rows=1
[PASS] oversize body returns 413
```

The stored payload reads
`{"event_id":"acc-...","name":"page_view","consent_state":true}`, confirming
the write key is redacted (D15). The throwaway tenant was removed afterwards.

Two acceptance paths now exist and both matter: `smoke.py` (in-process, fast,
proves the code) and `acceptance.py` (over HTTPS, proves the deployment). Only
the second would have caught D21.

### D21: A 204 must carry Content-Length: 0 on this host

LiteSpeed (behind the `Pro Cloudify` proxy) answers **500** for a 204 response
with no `Content-Length`. Werkzeug strips that header from 204 per RFC 7230,
so the receiver's silent rejects arrived as 500s while every `jsonify` path
worked, and the same code returned 204 under Flask's test client.

Established by measurement, not deduction: `GET /diag/204?mode=` returned
`plain` 500, `content-type` 500 (interrupted, inconclusive), `content-length`
**204**, and `empty-200` 200. `_no_content()` therefore subclasses `Response`
and re-adds `Content-Length: 0` in `get_wsgi_headers`, after Werkzeug's own
processing, which is the only place the header survives.

Consequence for tests: the 204 assertion changed from "no headers at all" to
"`Content-Length: 0` and nothing else". Seven tests fail if the header is
removed, so the requirement is pinned. Setting the header on a plain
`Response` does not work - it is stripped again - which is why the earlier
attempt appeared to change nothing.

### D20: Resolved - the deployed layout on `my.herbsworld.net`

Settled 2026-11-23. `DEPLOY.md` holds the operational detail; the essentials:

| Piece | Path |
| --- | --- |
| Passenger application root | `/home/herbswor/my.herbsworld.net` |
| Startup file (thin bootstrap) | `/home/herbswor/my.herbsworld.net/passenger_wsgi.py` |
| Git checkout (real code) | `/home/herbswor/repositories/pytracking` |
| Module that serves | `/home/herbswor/repositories/pytracking/passenger_wsgi.py` |
| Python / venv | `/home/herbswor/virtualenv/my.herbsworld.net/3.13/bin/python` |

`PassengerAppRoot` points at the **domain directory**, not the checkout. The
docroot holds only `.htaccess` and a small bootstrap (repo file
`deploy/passenger_wsgi.py`) that loads the checkout's module by absolute path.
The landing page's "Executing file" line reports the
`~/repositories/pytracking` copy, which is how this was confirmed.

That is option B from the original write-up, chosen because the checkout stays
a clean git tree and the bootstrap is immune to cPanel rewriting the startup
file with its own placeholder.

**One copy of the code only.** The checkout is the deployed copy; the docroot
must contain no second copy, or a fix could land in a folder that is not being
served.

Background: a known-good app on the same host (`~/crawler.herbsworld.net/`)
keeps its files directly in the domain directory, and its `.htaccess` sets
`PassengerAppRoot` to that directory. Its `stderr.log` also exposed cPanel's
placeholder startup file:

```python
import imp
wsgi = imp.load_source('wsgi', 'passenger_wsgi.py')
```

which re-imports itself until `RecursionError`. cPanel rewrites it on every
Save, which is why `DEPLOY.md` says never to press Save.

### D23: Public diagnostic routes removed before Phase 2

`/diag/db` and `/diag/outbound` reported database and outbound-egress status on
an unauthenticated public URL. They existed to get Phase 0 accepted on a host
whose routing was broken; that is done (D22), so both routes and their tests
are removed and `/diag/204` (a temporary probe from D21) with them.

`/diag/no-content` stays: it returns a bare 204 and reveals nothing, and it is
the check that catches a regression in the host-specific 204 framing.

`diagnose.py`, `smoke.py` and `acceptance.py` are development tools and must
not sit on the public host. `smoke.py` was deleted - it drove Flask's test
client in-process, which duplicated the unit tests and, more importantly, could
not see host framing problems (D21). `acceptance.py` is kept in the repo and
run against a staging copy; `diagnose.py` remains for environment checks. See
`DEPLOY.md`.

### D24: Phase 2 - browser hashing, CORS, and the beacon body type

Settled 2026-11-23.

**Email and phone are hashed in the browser.** Owner decision. The SDK
normalises (email trimmed and lowercased; phone reduced to digits with a
leading `+` if present) and applies SHA-256 via `crypto.subtle`, sending
`user_data.em` / `user_data.ph`. Raw values never leave the page and the
receiver stores only hashes inside `payload_json`. Consequence: hashing is
skipped when `crypto.subtle` is unavailable, which on a real site means a
plain-http page (secure context is required).

**Event payload contract** (what the SDK sends, and therefore what
`payload_json` holds, minus `wk`):

```
{
  "wk": "<write key>",                 // stripped before storage (D15)
  "event_id": "<32 hex chars>",        // crypto.randomUUID(), reused on retry
  "name": "page_view|add_to_cart|checkout|purchase|custom",
  "event_time": 1730000000,            // unix seconds
  "consent_state": true | false | null,
  "user_data": {"em": "<sha256>", "ph": "<sha256>",
                "fbp": "...", "fbc": "...", "client_user_agent": "..."},
  "custom_data": {"value": 49.99, "currency": "USD", ...},
  "source": {"url": "...", "referrer": "..."}
}
```

The dedicated columns (`name`, `event_id`, `user_agent`, `source_origin`,
`consent_state`, `client_ip_hash`) are still filled by the receiver from the
request itself, so the payload duplicates a little of that on purpose: the
stored payload should be self-describing for the Phase 3 forwarder.

**CORS.** A preflight carries no body, so no write key exists to resolve a
tenant from. `OPTIONS /api/v2/ev` therefore grants `Access-Control-Allow-Origin`
when the request origin appears in `tenant_origins` **for any tenant**; the POST
itself still enforces the tenant-specific match (D11). The grant is echoed only
for a legitimate origin, never `*`, and `Vary: Origin` is set so caches do not
serve one origin's answer to another. Allowed methods `POST, OPTIONS`, allowed
headers `Content-Type`, `Access-Control-Max-Age` 600. Preflight responses still
go through `_no_content()`, so the `Content-Length: 0` requirement (D21) holds.
On a POST, the grant is added only after the origin matches the resolved
tenant's allowlist.

**The SDK sends `text/plain`.** `sendBeacon(string)` uses
`text/plain;charset=UTF-8`, and a `text/plain` cross-origin POST is a CORS
"simple request", so no preflight round trip is needed and beacon delivery is
not at risk from a failed preflight on unload. The receiver therefore parses
the raw body as JSON instead of trusting `Content-Type` (malformed JSON is
still 400). `OPTIONS` support remains for clients that do send
`application/json`.

**SDK delivery.** Served by Flask from `static/pytracking.js` at
`/static/pytracking.js`, so it versions with the app. Measured size: 7,915
bytes raw, **5,106 bytes with comments and layout stripped (4.99 KB)** - under
the 10 KB budget. No build step; the readable file is the artifact.

**Test origins.** Two, both for a throwaway test tenant only:

- `http://localhost:8000` - cross-origin. Serve `static/test.html` with
  `python -m http.server 8000` from the folder containing it. This exercises
  preflight and the origin check for real.
- `https://my.herbsworld.net` - same-origin, proving the production-like HTTPS
  path. Open `https://my.herbsworld.net/static/test.html`.

A `file://` page sends `Origin: null` and will not match. Origins must be
stored exactly (`scheme://host:port`, no trailing slash). Remove these rows
before go-live; adding `localhost` to a real tenant would let any local page
post for that tenant.

**Phase 2 accepted 2026-11-23** in a real browser (headless Edge) against the
live host, from a cross-origin page at `http://localhost:8000`. Both fetches -
`text/plain` and `application/json` - returned a readable 204, the latter
proving the preflight passed; the SDK loaded with `crypto.subtle` available and
sent its event. The stored rows confirmed tenant resolution from the write key
(`source_origin` was the merchant origin, not the collector's), and
`user_data.em` / `user_data.ph` matched SHA-256 of the normalised email and
phone, with no raw PII anywhere in `payload_json`. The test tenant, its key,
both origins and its events were deleted afterwards.

Note: `_fbp` / `_fbc` were absent from that run because the headless profile
had no Meta cookies. The SDK does read them - verified in the Node contract
test, which sent `fbp=fb.1.1700000000.111` and `fbc=fb.1.1700000000.222` - but
a live browser only has them after a Meta pixel has set them.

### D25: Phase 3 - destinations table, forwarder materialises, encryption deferred

Settled 2026-11-23.

**Destinations are a table, not env vars.** Migration `002_deliveries.sql` adds
`destinations(tenant_id, type, name, config_json, active)` with a unique key on
`(tenant_id, type, name)`. Phase 4's CRUD screen builds on it, and a tenant can
have several destinations of the same type (for example a live and a test
pixel).

**The forwarder materialises deliveries; the receiver is untouched.** At the
end of each pass it runs one idempotent statement:

```sql
INSERT IGNORE INTO deliveries (tenant_id, event_row_id, destination_id, ...)
SELECT e.tenant_id, e.id, d.id, ...
FROM events e JOIN destinations d ON d.tenant_id = e.tenant_id AND d.active = 1
LEFT JOIN deliveries dl ON dl.event_row_id = e.id AND dl.destination_id = d.id
WHERE dl.id IS NULL LIMIT ?
```

The unique key `(event_row_id, destination_id)` makes repeats no-ops. Ingest
keeps its Phase 1 performance and accepted behaviour, and a newly added
destination back-fills from existing events on the next pass.

**Retry policy.** `attempts` increments when a delivery is claimed, so a crash
still counts as an attempt. On a retryable failure the next attempt is
`base * 2^(attempts-1)` seconds, capped: defaults 60s base, 3600s cap, 8
attempts, then dead-letter. Success is a 2xx; `408`, `425`, `429` and any 5xx
retry; other 4xx dead-letter immediately; transport failures (DNS, TLS,
timeout) retry. Two adapters inspect the body because status alone is
misleading:

- **Meta** returns HTTP 400 for throttling (`code` 4, 17, 32, 341, 613,
  80004) and transient trouble (1, 2), so those codes retry while other 400s
  dead-letter. This matters: a blanket "400 is permanent" dead-letters rate
  limits that would have succeeded seconds later.
- **TikTok** returns 200 with a `code` in the body, so `code == 0` is success
  and a non-zero code is dead unless it is one of its rate-limit codes.

Live probe note: a dataset-scoped CAPI token can **write** events but cannot
`GET /{dataset_id}`. That read returns `(#100) Missing Permission` even for a
working token, so token validity must be checked by POSTing a deliberately
invalid payload (`{"data": []}`) and confirming the error is about the payload
(`param data must be non-empty`), not about permissions.

**Concurrency is a claim, not `SKIP LOCKED`.** One statement takes the work:

```sql
UPDATE deliveries SET status='processing', claim_token=?, locked_until=NOW()+INTERVAL ? SECOND,
       attempts=attempts+1
WHERE status IN ('pending','processing') AND next_attempt_at <= NOW()
  AND (locked_until IS NULL OR locked_until < NOW())
ORDER BY next_attempt_at, id LIMIT ?
```

InnoDB locks the rows it touches, so a second run cannot claim them, and a run
that dies leaves `locked_until` in the past so the rows return after the lock
window. `SELECT ... FOR UPDATE SKIP LOCKED` was avoided because it needs MySQL
8 / MariaDB 10.6+, which this host's version is not confirmed to provide.

**Adapters implement `map`, `send`, `classify_error`** and live in
`tracker.adapters`. Private add-ons register under the `tracker.adapters`
entry-point group and are loaded on demand; the core never imports them
(plan.md line 9), and a broken plugin cannot stop a run. Built-ins: `meta`,
`ga4`, `tiktok`. Outbound HTTP uses `urllib` from the standard library, per D4.

Event names are translated per provider: Meta uses `PageView`/`AddToCart`/
`InitiateCheckout`/`Purchase`, TikTok uses `ViewContent`/`AddToCart`/
`InitiateCheckout`/`CompletePayment`, GA4 keeps the internal snake_case names.

**Secrets are stored in clear for now.** plan.md line 7 asks for encryption at
rest with a key from an environment variable, but that needs the `cryptography`
package and the rule for this build allows only flask and PyMySQL. Owner
decision: defer to Phase 5, which already covers the security review. The gap
is real and recorded rather than hidden: `destinations.config_json` holds
provider tokens in plaintext. Two mitigations are already in place - the GA4
`api_secret` travels in a query string, so it is stripped from any stored error
text by `redact()`, and Meta's access token is sent in the body rather than the
URL.

Open for Phase 5: encrypt `config_json`, and decide whether credentials also
need rotating on a schedule.

## Open questions

None. Phase 3's one open item (credential encryption) is recorded in D25 as
Phase 5 work.



