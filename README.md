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

curl http://127.0.0.1:5000/healthz        # {"ok":true}
curl http://127.0.0.1:5000/diag/db        # {"db":"ok"} or error detail
curl http://127.0.0.1:5000/diag/outbound  # {"status":...,"elapsed_ms":...}
```

On cPanel, Passenger loads `passenger_wsgi.py` and uses the `application`
object as the WSGI entry point.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`. Every source file carries an
SPDX header. Commits require a DCO sign-off (`git commit -s`).
