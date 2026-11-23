# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 0 host-validation receiver in `passenger_wsgi.py`:
  - `POST /api/v2/ev` returns `204 No Content` with an empty body and no
    response headers.
  - `GET /healthz` returns `{"ok": true}`.
  - `GET /diag/db` opens a PyMySQL connection using `DB_*` environment
    variables and returns `{"db": "ok"}` or
    `{"db": "error", "message": ...}`; the password is never echoed.
  - `GET /diag/outbound` performs one HTTPS GET to
    `https://graph.facebook.com/` with a 5-second timeout and returns the
    HTTP status code and elapsed milliseconds, or the exception class and
    message on transport failure.
- `tests/test_phase0.py`: offline tests for all Phase 0 routes using the
  Flask test client, with the database and outbound calls mocked.
- Project scaffolding: pinned `requirements.txt`, `.env.example`,
  `.gitignore`, Apache 2.0 `LICENSE`, `NOTICE`, `README.md`,
  `DECISIONS.md`, and SPDX headers on all source files.

- **Phase 1 receiver** in `passenger_wsgi.py`: `POST /api/v2/ev` now resolves
  write keys to tenants, enforces a fail-closed Origin allowlist, validates
  `event_id`/`name` (each string, max 64), deduplicates on
  `(tenant_id, event_id)`, and stores events via PyMySQL with HMAC-SHA256
  client-IP hashing (raw IP never stored). Bodies over 16 KB return 413;
  invalid event fields return 400; every write-key problem (missing, unknown,
  inactive) returns a silent 204. Responses remain 204 with no body and no
  tracking headers. The stored payload omits the `wk` write key.
- `db/migrations/001_init.sql`: idempotent schema for `tenants`,
  `tenant_keys`, `tenant_origins`, and `events` (utf8mb4/InnoDB, with the
  `(tenant_id, event_id)` unique key and `(tenant_id, received_at)` index).
- `db/run_migration.py`: runs a migration SQL file using `DB_*` env vars.
- `tests/test_receiver.py`: offline data-protection tests covering unknown-key
  writes, bad-origin writes, oversize-body 413, duplicate-event dedupe, raw-IP
  sanitisation, and write-key redaction from the stored payload.
- `.env.example`: added `PYTRACKING_IP_SALT`.

### Fixed

- **204 responses returned 500 on the real host.** LiteSpeed (behind the
  `Pro Cloudify` proxy) rejects a 204 with no `Content-Length`, and Werkzeug
  strips that header per RFC 7230. `_no_content()` now subclasses `Response`
  and re-adds `Content-Length: 0` after Werkzeug's header processing, so
  silent rejects and successful ingests both deliver as 204. Found by
  measurement via `/diag/204?mode=`, not by guessing. Seven tests pin the
  header so it cannot regress.
- `POST /api/v2/ev` 204 responses were again carrying `Content-Type: text/html`
  after the Phase 1 rewrite, contradicting decision D6 ("no tracking
  headers"). All 204s now go through a `_no_content()` helper that strips it,
  and the tests assert the response carries no tracking headers in every
  branch (success, duplicate, unknown key, inactive key, bad origin, missing
  key).
- `.env.example` entries for the deployed host were the source of a 1045
  "Access denied": the host's `DB_USER` was a truncated cPanel user name.
  Recorded in DECISIONS.md as D18.

### Added

- `smoke.py`: runs the Phase 0 acceptance checks on the host itself, via
  Flask's test client against the real MySQL and network. Works from the
  panel's script box (path only, no arguments) and needs no HTTP routing,
  so it validates the deployment even when the vhost is misrouted. Creates
  and removes a throwaway tenant, and prints PASS/FAIL per check.
- `diagnose.py`: environment probe reporting interpreters, dependencies, env
  presence, repository files, DB connectivity and tables, the loaded WSGI
  module with its routes, and any `Passenger*` directives in nearby
  `.htaccess` files. Redacts the DB password from error messages.

### Removed

- **Public diagnostic routes** (decision D23): `GET /diag/db` and
  `GET /diag/outbound` exposed database and outbound-egress status on an
  unauthenticated URL. Both are gone, along with the temporary `/diag/204`
  probe from D21 and their tests. `/diag/no-content` stays because it returns
  a bare 204 and reveals nothing.
- `smoke.py`: it drove Flask's test client in-process, duplicating the unit
  tests and - critically - unable to observe host framing problems (D21).
  `acceptance.py` covers the deployment path instead.

### Added

- `DEPLOY.md`: the live cPanel layout (PassengerAppRoot, startup file, where
  the Git checkout lives, which module actually serves), the rule that the
  Python panel's Save button rewrites `passenger_wsgi.py` with a
  self-recursive stub, how to verify the startup file after a pull, and the
  requirement to keep exactly one copy of the code.
- `tests/test_phase0.py` now asserts the removed diagnostics stay removed, so
  they cannot quietly return.

### Added

- **Phase 2 browser SDK**, `static/pytracking.js`, served at
  `/static/pytracking.js`. Vanilla IIFE, no dependencies, no build step.
  `pytracking.init({url, writeKey, debug, consent, autoPageView})`, `track()`,
  and `pageView()`/`addToCart()`/`checkout()`/`purchase()` helpers; auto
  `page_view` on init unless disabled. Generates `event_id` with
  `crypto.randomUUID()` (fallback, reused on retry), reads `_fbp`/`_fbc`
  cookies, and hashes email and phone with SHA-256 in the browser so raw PII
  never leaves the page. Sends `text/plain` (a CORS simple request, and what
  sendBeacon produces) with `fetch(..., keepalive)` and a `sendBeacon` flush on
  `pagehide`/`visibilitychange`. Measured 7,915 bytes raw, 5,106 bytes with
  comments and layout stripped (4.99 KB).
- **CORS support in the receiver** (D24): `OPTIONS /api/v2/ev` grants
  `Access-Control-Allow-Origin` only for origins registered in
  `tenant_origins`, with methods `POST, OPTIONS`, header `Content-Type`,
  `Max-Age` 600 and `Vary: Origin`; the POST echoes the origin only after the
  tenant-specific origin check passes. Preflight responses keep
  `Content-Length: 0` (D21).
- The receiver now parses the raw body as JSON rather than requiring
  `application/json`, so `navigator.sendBeacon` payloads are accepted;
  malformed JSON is still 400.
- `static/test.html`: test page that fires all four events and shows a replay
  button, usable cross-origin (from `http://localhost:8000`) or same-origin
  (`https://my.herbsworld.net/static/test.html`).
- Tests: CORS preflight (registered vs unregistered origin, no-origin, DB
  failure fails closed, POST echo), and beacon bodies (`text/plain` accepted,
  garbage 400, JSON still works, oversize 413). 36 tests total.

### Verified

- **Phase 0 and Phase 1 accepted over public HTTPS (2026-11-23), 12/12
  checks.** `acceptance.py` posts real HTTP to `https://my.herbsworld.net`:
  `/healthz` 200, 204 framing with `Content-Length: 0`, `/diag/db` `{"db":"ok"}`,
  a valid key returning 204, the event landing in MySQL, a 64-character IP
  hash, `user_agent`/`source_origin` stored, `consent_state` recorded,
  the write key absent from the payload, duplicate suppression, a bad origin
  writing nothing, and an oversize body returning 413. Fixture removed.

- **Phase 0 acceptance cleared on the real host (2026-11-23), 8/8 checks.**
  `smoke.py` against the deployed app, real MySQL, and real outbound network:
  health returns `{"ok": true}`, `/diag/db` returns `{"db": "ok"}`, and
  `/diag/outbound` reports `status=400` in 268.7 ms (the expected success
  signal - any HTTP response proves egress). `POST /api/v2/ev` returns a bare
  204, one row is written, a duplicate `event_id` adds nothing, the stored
  client IP is a 64-character HMAC, and `user_agent`/`source_origin` are
  stored. Phase 1 schema and ingestion are therefore verified end-to-end.

### Changed

- `POST /api/v2/ev` replaces the Phase 0 no-op stub; an empty request body now
  returns a silent 204 rather than unconditionally 204 (same status, now with
  validation behind it).
- `db/run_migration.py`: `--file` is now optional and defaults to
  `migrations/001_init.sql` resolved relative to the script, so the migration
  can be run from a hosting panel that accepts only a script path. Progress
  and failure messages go to stdout.
