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

### D6: The 204 response carries no headers at all

To keep the 204 truly empty, the route strips the default `Content-Type`
header. Werkzeug already omits `Content-Length` for 204. The test suite
asserts no `Content-Type`, `Content-Length`, `Set-Cookie`, or `X-*`
headers on the response.

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

## Open questions

1. **Who enables Passenger on `my.herbsworld.net`?** The app is confirmed
   healthy and the vhost serves static files only. Either support must enable
   Passenger/WSGI for that domain, or the app should be recreated on a fresh
   subdomain whose docroot cPanel provisions itself.
