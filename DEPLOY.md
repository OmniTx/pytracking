# Deployment

How `my.herbsworld.net` is actually wired, and how to change it without
breaking it. Recorded from the live host (decisions D20 and D22).

## Live layout

| Piece | Path |
| --- | --- |
| Passenger application root | `/home/herbswor/my.herbsworld.net` |
| Passenger config | `/home/herbswor/my.herbsworld.net/.htaccess` |
| Startup file (thin bootstrap) | `/home/herbswor/my.herbsworld.net/passenger_wsgi.py` |
| Git checkout (the real code) | `/home/herbswor/repositories/pytracking` |
| Module that actually serves | `/home/herbswor/repositories/pytracking/passenger_wsgi.py` |
| Virtualenv / Python | `/home/herbswor/virtualenv/my.herbsworld.net/3.13/bin/python` |

`PassengerAppRoot` points at the **domain directory**, not the checkout. The
docroot holds only `.htaccess` plus a small bootstrap
(`deploy/passenger_wsgi.py` in this repo) which loads the checkout's module by
absolute path. The landing page's "Executing file" line is the proof: it
reports the `~/repositories/pytracking` copy.

**One copy of the code only.** The checkout is the deployed copy. Do not leave
a second copy in the docroot - a fix could land in the folder that is not
being served. The docroot should contain exactly `.htaccess` and the
bootstrap.

## Never press Save in the Python panel

Saving (or destroying and re-creating) the Python app makes cPanel rewrite
`passenger_wsgi.py`, usually with this placeholder:

```python
import imp
wsgi = imp.load_source('wsgi', 'passenger_wsgi.py')
```

That re-imports itself until `RecursionError`, so the app cannot start. It
appeared once already as a 488-byte file.

If you must save, restore the file afterwards:

- docroot `passenger_wsgi.py` -> the bootstrap (about 20 lines, uses
  `importlib` to load from `~/repositories/pytracking`)
- repo `passenger_wsgi.py` -> unchanged from git

## After every pull

```sh
# in the checkout
git status --short          # should be clean
wc -c passenger_wsgi.py     # must be the real module (several KB), not 488 bytes
```

Then run `diagnose.py` (from the checkout, via the panel's *Execute python
script* box). It reports the startup file state directly:

```
--- startup file ---
passenger_wsgi.py          real WSGI module
```

If it says `CPANEL STUB - app cannot start`, restore the file as above.

## Environment variables

Set in cPanel's Python panel and **Update** to apply. The code reads
`os.environ` only; it does not load `.env`.

| Name | Notes |
| --- | --- |
| `DB_HOST` | `localhost` on cPanel |
| `DB_NAME` | e.g. `herbswor_tracking_db` |
| `DB_USER` | cPanel prefixes and truncates user names |
| `DB_PASSWORD` | rotate periodically; never commit |
| `PYTRACKING_IP_SALT` | HMAC salt for client-IP hashing |
| `PYTRACKING_DEBUG` | optional; `1` logs receiver tracebacks to `pytracking-debug.log` |

## Deploying a change

1. Push to `main`.
2. cPanel -> Git Version Control -> pytracking -> **Update from Remote**.
3. Restart the app in the Python panel.
4. Run `acceptance.py` from the checkout (see below).

## What must not live on the public host

`diagnose.py`, `acceptance.py`, and any other tooling stay **out of the
docroot** and should be removed from the served tree once a deployment is
verified. They are development tools, not part of the application.

Run them from the checkout directory only; never copy them into
`~/my.herbsworld.net/`.

## Acceptance testing

`acceptance.py` posts real HTTP to the deployed URL and verifies the whole
path through Passenger, LiteSpeed, the proxies and MySQL.

- Against **staging**, not production, once Phase 2 lands.
- It creates and removes its own tenant, key, origin and events.
- Override the target with `PYTRACKING_BASE_URL`.

```sh
PYTRACKING_BASE_URL=https://staging.example.net python acceptance.py
```

## Browser SDK (Phase 2)

The SDK ships as part of the app and is served by Flask:

```
https://my.herbsworld.net/static/pytracking.js
https://my.herbsworld.net/static/test.html
```

Both live in the checkout's `static/` directory, so they deploy with a normal
`git pull` and need no extra web-server configuration. No build step: the
file in `static/` is the artifact (4.99 KB with comments and layout stripped).

### Prepare the test tenant

Use a throwaway tenant and key. Never add test origins to a real tenant.

```sql
INSERT INTO tenants (name, slug, status)
VALUES ('SDK Test', 'sdk-test', 'active');

-- put the id from the previous statement here
INSERT INTO tenant_keys (tenant_id, write_key, active)
VALUES (<id>, 'wk_sdk_test_001', 1);

INSERT INTO tenant_origins (tenant_id, origin) VALUES
  (<id>, 'http://localhost:8000'),      -- cross-origin test page
  (<id>, 'https://my.herbsworld.net');  -- same-origin test page
```

Origins must match exactly: scheme, host and port, no trailing slash.
`file://` sends `Origin: null` and will not match.

### Cross-origin test (exercises CORS)

```sh
cd <folder containing static/test.html>
python -m http.server 8000
```

Then open `http://localhost:8000/test.html?wk=wk_sdk_test_001`.

The test page disables the SDK's automatic `page_view` so every event is a
deliberate click; the production default still fires one on `init()`.

### Same-origin test (production-like HTTPS)

Open
`https://my.herbsworld.net/static/test.html?wk=wk_sdk_test_001`.

### Verify what landed

```sql
SELECT id, tenant_id, event_id, name, received_at,
       JSON_EXTRACT(payload_json, '$.user_data.em') AS email_hash,
       JSON_EXTRACT(payload_json, '$.user_data.fbp') AS fbp,
       source_origin, user_agent
FROM events
WHERE tenant_id = <id>
ORDER BY id DESC
LIMIT 20;
```

Each click should add exactly one row. Clicking **replay last event_id** must
not add another. The `email_hash` should be 64 hex characters, and no raw
email or phone should appear anywhere in `payload_json`.

### Clean up before go-live

```sql
DELETE FROM events   WHERE tenant_id = <id>;
DELETE FROM tenant_keys   WHERE tenant_id = <id>;
DELETE FROM tenant_origins WHERE tenant_id = <id>;
DELETE FROM tenants  WHERE id = <id>;
```

## Public routes

| Route | Purpose |
| --- | --- |
| `POST /api/v2/ev` | event receiver |
| `OPTIONS /api/v2/ev` | CORS preflight for browsers |
| `GET /healthz` | health check |
| `GET /` | landing page |
| `GET /static/pytracking.js` | browser SDK |
| `GET /static/test.html` | SDK test page |
| `GET /diag/no-content` | confirms the host accepts our 204 framing |

`/diag/db` and `/diag/outbound` were removed before Phase 2 (decision D23):
they exposed database and egress status on a public URL.
