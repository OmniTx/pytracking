# SPDX-License-Identifier: Apache-2.0
"""Phase 0 acceptance checks, run on the host without a web server.

Usable from the hosting panel's "Execute python script" box, which accepts
only a path and cannot pass arguments, so this validates the deployed code
against the deployed database and network::

    /home/<user>/repositories/pytracking/smoke.py

It exercises the real Flask app in-process via Flask's test client, so it
tests the same view code and the same PyMySQL connection the vhost would,
without depending on HTTP routing being configured.

Each check prints PASS or FAIL with the actual observed value. A throwaway
tenant is created and fully removed again, so the database is left as found.
Secrets are never printed.
"""

import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SMOKE_SLUG = "phase0-smoke-test"

results = []


def record(name, passed, detail):
    results.append((name, passed, detail))
    print("[{0}] {1}\n      {2}".format("PASS" if passed else "FAIL", name, detail))


def _connect():
    import pymysql

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
        charset="utf8mb4",
    )


def _create_fixture(connection, write_key, origin):
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM tenants WHERE slug = %s", (SMOKE_SLUG,))
        row = cursor.fetchone()
        if row:
            tenant_id = row[0]
        else:
            cursor.execute(
                "INSERT INTO tenants (name, slug, status) "
                "VALUES ('Phase 0 Smoke Test', %s, 'active')",
                (SMOKE_SLUG,),
            )
            tenant_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO tenant_keys (tenant_id, write_key, active) "
            "VALUES (%s, %s, 1)",
            (tenant_id, write_key),
        )
        cursor.execute(
            "INSERT INTO tenant_origins (tenant_id, origin) VALUES (%s, %s)",
            (tenant_id, origin),
        )
    connection.commit()
    return tenant_id


def _remove_fixture(connection, tenant_id, event_ids):
    with connection.cursor() as cursor:
        for event_id in event_ids:
            cursor.execute(
                "DELETE FROM events WHERE tenant_id = %s AND event_id = %s",
                (tenant_id, event_id),
            )
        cursor.execute("DELETE FROM tenant_keys WHERE tenant_id = %s", (tenant_id,))
        cursor.execute("DELETE FROM tenant_origins WHERE tenant_id = %s", (tenant_id,))
        cursor.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    connection.commit()


def _count_events(connection, tenant_id):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id = %s", (tenant_id,)
        )
        return cursor.fetchone()[0]


def main():
    print("=" * 70)
    print("pytracking Phase 0 acceptance checks")
    print("=" * 70)

    import passenger_wsgi

    client = passenger_wsgi.application.test_client()
    write_key = "smoke-" + secrets.token_hex(8)
    origin = "https://phase0-smoke.example.com"
    event_id = "smoke-" + secrets.token_hex(6)

    connection = _connect()
    tenant_id = None
    try:
        tenant_id = _create_fixture(connection, write_key, origin)
        print("fixture tenant id {0}\n".format(tenant_id))

        response = client.get("/healthz")
        record(
            "GET /healthz returns {\"ok\": true}",
            response.status_code == 200 and response.get_json() == {"ok": True},
            "status={0} body={1}".format(
                response.status_code, response.get_data(as_text=True)[:120]
            ),
        )

        response = client.get("/diag/db")
        record(
            "GET /diag/db returns {\"db\": \"ok\"}",
            response.get_json() == {"db": "ok"},
            "status={0} body={1}".format(
                response.status_code, response.get_data(as_text=True)[:200]
            ),
        )

        response = client.get("/diag/outbound")
        body = response.get_json() or {}
        outbound_ok = "status" in body or "error" in body
        record(
            "GET /diag/outbound reports a status",
            outbound_ok,
            "status_code={0} body={1}".format(
                response.status_code, response.get_data(as_text=True)[:200]
            ),
            )

        response = client.post(
            "/api/v2/ev",
            json={
                "wk": write_key,
                "event_id": event_id,
                "name": "page_view",
            },
            headers={"Origin": origin, "User-Agent": "phase0-smoke/1.0"},
            environ_base={"REMOTE_ADDR": "203.0.113.42"},
        )
        headers_present = set(response.headers.keys())
        record(
            "POST /api/v2/ev returns 204 with no body",
            response.status_code == 204 and response.data == b"",
            "status={0} body={1!r} headers={2}".format(
                response.status_code,
                response.data[:80],
                sorted(headers_present) or "none",
            ),
        )

        stored = _count_events(connection, tenant_id)
        record(
            "the event was written to the database",
            stored == 1,
            "{0} row(s) for tenant {1}".format(stored, tenant_id),
        )

        # Replaying the same event must not add a row.
        response = client.post(
            "/api/v2/ev",
            json={"wk": write_key, "event_id": event_id, "name": "page_view"},
            headers={"Origin": origin, "User-Agent": "phase0-smoke/1.0"},
            environ_base={"REMOTE_ADDR": "203.0.113.42"},
        )
        after_duplicate = _count_events(connection, tenant_id)
        record(
            "a duplicate event_id stores nothing extra",
            response.status_code == 204 and after_duplicate == 1,
            "status={0} rows={1}".format(response.status_code, after_duplicate),
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT client_ip_hash, user_agent, source_origin "
                "FROM events WHERE tenant_id = %s AND event_id = %s",
                (tenant_id, event_id),
            )
            row = cursor.fetchone()
        if row:
            ip_hash, user_agent, source_origin = row
            record(
                "the raw client IP is not stored",
                ip_hash != "203.0.113.42" and len(ip_hash) == 64,
                "client_ip_hash={0}... ({1} chars)".format(ip_hash[:16], len(ip_hash)),
            )
            record(
                "user_agent and source_origin are stored",
                user_agent == "phase0-smoke/1.0" and source_origin == origin,
                "user_agent={0!r} source_origin={1!r}".format(user_agent, source_origin),
            )
        else:
            record("the stored row is readable", False, "row not found")

    finally:
        if tenant_id is not None:
            try:
                _remove_fixture(connection, tenant_id, [event_id])
                print("\nfixture removed")
            except Exception as exc:
                print("\nfixture cleanup failed: {0}".format(exc))
        connection.close()

    failures = [name for name, passed, _ in results if not passed]
    print("\n" + "=" * 70)
    print("{0} of {1} checks passed".format(
        len(results) - len(failures), len(results)
    ))
    for name in failures:
        print("  FAILED: {0}".format(name))
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
