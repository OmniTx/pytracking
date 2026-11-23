# pytracking

Server-side conversion tracking receiver and forwarder built for shared
hosting: Python 3.12/3.13 on cPanel with Passenger (WSGI), MySQL via
PyMySQL, Flask, and no Node build step.

## Status

**Phase 1 - schema and ingestion: implemented.**
Phase 0 (host validation) and Phase 1 (tenant resolution, origin checks,
dedupe, IP hashing) are in place. Phases 2-5 (browser SDK, forwarder, admin
UI, hardening) are not built yet. See `plan.md` for the roadmap and
`CHANGELOG.md` for what has landed.

## Layout

```
passenger_wsgi.py       WSGI entry point (Passenger), defines `application`
requirements.txt        Pinned dependencies (flask, PyMySQL)
.env.example            Environment variable template (copy to .env)
db/migrations/001_init.sql  Phase 1 schema (tenants, keys, origins, events)
db/run_migration.py     Executes a migration SQL file
tests/test_phase0.py    Offline tests for health/diagnostic routes
tests/test_receiver.py  Offline data-protection tests for the receiver
DECISIONS.md            Ambiguities resolved and open questions
CHANGELOG.md            Change history
```

## Configuration

All secrets come from environment variables. Copy `.env.example` to `.env`
and fill in real values. `.env` is git-ignored and must never be committed.

| Variable            | Purpose                                          |
| ------------------- | ------------------------------------------------ |
| `DB_HOST`           | MySQL host                                       |
| `DB_NAME`           | Database name                                    |
| `DB_USER`           | Database user                                    |
| `DB_PASSWORD`       | Database password                                |
| `PYTRACKING_IP_SALT`| HMAC salt for client-IP hashing (keep secret)    |

Generate a salt with:

```sh
python -c "import secrets; print(secrets.token_hex(32))"
```

## Database setup

```sh
python db/run_migration.py
```

The SQL file defaults to `db/migrations/001_init.sql` (resolved relative to
the script), so this also works from a hosting panel that accepts only a
script path. The migration is idempotent (`CREATE TABLE IF NOT EXISTS`) and
never drops data. Then insert a tenant, a write key, and at least one allowed
origin:

```sql
INSERT INTO tenants (name, slug, status) VALUES ('Demo', 'demo', 'active');
INSERT INTO tenant_keys (tenant_id, write_key, active)
    VALUES (1, 'demo-write-key', 1);
INSERT INTO tenant_origins (tenant_id, origin)
    VALUES (1, 'https://demo.example.com');
```

## Running the checks

```sh
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate
pip install -r requirements.txt

# Run the offline test suite (mocks DB and outbound calls, no network)
python -m unittest discover -s tests -v

# Start the dev server (not for production; Passenger serves passenger_wsgi.py)
python passenger_wsgi.py
```

With the server running:

```sh
# 204 for a valid event; 204 silently for bad key/origin; 400 bad fields;
# 413 for bodies over 16 KB
curl -i -X POST http://127.0.0.1:5000/api/v2/ev \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://demo.example.com' \
  -d '{"wk":"demo-write-key","event_id":"evt-1","name":"page_view"}'

curl http://127.0.0.1:5000/healthz            # {"ok":true}
curl -i http://127.0.0.1:5000/diag/no-content # 204, Content-Length: 0
```

## Routes

| Route | Purpose |
| --- | --- |
| `POST /api/v2/ev` | event receiver (204 on success and on silent rejects) |
| `OPTIONS /api/v2/ev` | CORS preflight for browser clients |
| `GET /healthz` | health check, `{"ok": true}` |
| `GET /` | landing page with build marker and env-var presence |
| `GET /static/pytracking.js` | browser SDK |
| `GET /static/test.html` | SDK test page |
| `GET /diag/no-content` | confirms the host accepts our 204 framing |

Public diagnostics `/diag/db` and `/diag/outbound` were removed before Phase 2
because they exposed database and egress status; see `DECISIONS.md` D23.

## Browser SDK

```html
<script src="https://my.herbsworld.net/static/pytracking.js"></script>
<script>
  pytracking.init({
    url: "https://my.herbsworld.net/api/v2/ev",
    writeKey: "wk_...",          // tenant write key
    consent: true                // optional; sent as consent_state
  });
  pytracking.addToCart({ value: 19.5, currency: "EUR", email: "a@b.c" });
  pytracking.purchase({ value: 49.99, currency: "USD",
                        email: "a@b.c", phone: "+1 555 0100" });
</script>
```

`init()` sends a `page_view` automatically unless `autoPageView: false`.
Email and phone are SHA-256 hashed in the browser, so raw values are never
transmitted. The receiver only accepts origins listed in `tenant_origins` for
that tenant; see `DECISIONS.md` D24 for the CORS policy and payload shape.

## Deployment

cPanel/Passenger deployment is documented in `DEPLOY.md`: the live layout, why
you must never press Save in the Python panel, and how to verify the startup
file after a pull.

On cPanel, Passenger loads `passenger_wsgi.py` and uses the `application`
object as the WSGI entry point.

## Acceptance testing

`acceptance.py` posts real HTTP to a deployed URL and verifies the whole path
through Passenger, the proxies and MySQL. Run it against staging:

```sh
PYTRACKING_BASE_URL=https://staging.example.net python acceptance.py
```

## License

Apache License 2.0. See `LICENSE` and `NOTICE`. Every source file carries an
SPDX header. Commits require a DCO sign-off (`git commit -s`).
