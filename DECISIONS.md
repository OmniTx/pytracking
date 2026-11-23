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

### D9: `/diag/*` routes stay unauthenticated in Phase 0

`plan.md` line 12 calls `/diag/outbound` an "admin-only route", but no auth
or user system exists until Phase 4. Owner decision (2026-11-22): keep the
routes unauthenticated for Phase 0 so the host checks can run immediately;
add auth when the Phase 4 login system lands. The routes leak only
connectivity status and can be IP-restricted at the web-server level in the
meantime.

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
unconfigured tenant. Owner decision (2026-11-22): keep fail-closed. Add
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

### D20: The working app on this host keeps its code in the domain directory

A known-good app on the same server (`~/crawler.herbsworld.net/`) has
`app.py`, `passenger_wsgi.py`, `requirements.txt` and its `.htaccess` **in the
domain directory**, and that `.htaccess` sets
`PassengerAppRoot "/home/herbswor/crawler.herbsworld.net"` — the document root
and the application root are the same directory, and LiteSpeed honours it.

Ours differs: code in `~/repositories/pytracking`, document root
`~/my.herbsworld.net`. That vhost answers with a LiteSpeed 404 and an empty
directory listing, so no request reaches Passenger even though `diagnose.py`
loads all six routes from the code.

The same app's `stderr.log` also reveals cPanel's placeholder
`passenger_wsgi.py`:

```python
import imp
wsgi = imp.load_source('wsgi', 'passenger_wsgi.py')
```

That re-imports itself until `RecursionError`, so the app can never start, and
cPanel rewrites the file on every Save. Any deployment step must end by
restoring the real module and not saving again afterwards.

Options, awaiting the owner's choice (both documented rather than guessed):

- **A. Move the application root to the domain directory** (mirrors the
  working app): copy the checkout into `~/my.herbsworld.net`, set Application
  root to `my.herbsworld.net` in the Python panel, then restore the real
  `passenger_wsgi.py` over cPanel's stub and restart.
- **B. Keep the checkout in `~/repositories`** and place a small bootstrap
  `passenger_wsgi.py` in the docroot that loads the repository module by
  explicit path and re-exports `application`, avoiding a name clash with
  Passenger's own module name.

## Open questions

1. **How should `my.herbsworld.net` be wired to the app?** See D20. Either
   move the application root to the domain directory (mirrors the working app
   on this host) or keep the checkout in `~/repositories` and add a bootstrap
   entry point in the docroot.
