# SPDX-License-Identifier: Apache-2.0
"""End-to-end acceptance over the public HTTPS URL.

Run from the hosting panel's "Execute python script" box, which accepts only a
path::

    /home/herbswor/repositories/pytracking/acceptance.py

Unlike smoke.py (which drives Flask's test client in-process), this sends real
HTTP requests to the deployed site, so it exercises Passenger, LiteSpeed, the
proxies, and the real MySQL in one go. That combination is what missed the
missing-Content-Length bug: the test client does not enforce it.

It creates a throwaway tenant, key and origin in MySQL, posts a real event over
HTTPS, verifies the row landed, then removes everything again.

Set PYTRACKING_BASE_URL to override the default URL.
Secrets are never printed.
"""

import json
import os
import secrets
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BASE_URL = os.environ.get("PYTRACKING_BASE_URL", "https://my.herbsworld.net").rstrip("/")
SLUG = "acceptance-wire-test"

results = []


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    print("[{0}] {1}".format("PASS" if passed else "FAIL", name))
    if detail:
        print("       {0}".format(detail))


def request(method, path, body=None, headers=None, timeout=25,
            content_type="application/json"):
    """Return (status, headers, body_text). HTTPError is a normal result."""
    url = BASE_URL + path
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = body.encode("utf-8")
        request_headers.setdefault("Content-Type", content_type)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return (response.status, dict(response.headers),
                    response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return (exc.code, dict(exc.headers or {}),
                exc.read().decode("utf-8", "replace"))
    except Exception as exc:
        return (None, {}, "{0}: {1}".format(type(exc).__name__, exc))


def connect():
    import pymysql

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
        charset="utf8mb4",
    )


def create_fixture(connection, write_key, origin):
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM tenants WHERE slug = %s", (SLUG,))
        row = cursor.fetchone()
        if row:
            tenant_id = row[0]
            cursor.execute(
                "DELETE FROM tenant_keys WHERE tenant_id = %s", (tenant_id,))
            cursor.execute(
                "DELETE FROM tenant_origins WHERE tenant_id = %s", (tenant_id,))
        else:
            cursor.execute(
                "INSERT INTO tenants (name, slug, status) "
                "VALUES ('Wire Acceptance', %s, 'active')",
                (SLUG,),
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


def remove_fixture(connection, tenant_id, event_ids):
    with connection.cursor() as cursor:
        for event_id in event_ids:
            cursor.execute(
                "DELETE FROM events WHERE tenant_id = %s AND event_id = %s",
                (tenant_id, event_id),
            )
        cursor.execute(
            "DELETE FROM tenant_keys WHERE tenant_id = %s", (tenant_id,))
        cursor.execute(
            "DELETE FROM tenant_origins WHERE tenant_id = %s", (tenant_id,))
        cursor.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    connection.commit()


def count_events(connection, tenant_id):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id = %s", (tenant_id,))
        return cursor.fetchone()[0]


def main():
    print("=" * 70)
    print("pytracking wire acceptance: {0}".format(BASE_URL))
    print("=" * 70)

    status, _, body = request("GET", "/healthz")
    record("GET /healthz over HTTPS", status == 200 and body.strip() == '{"ok":true}',
           "status={0} body={1!r}".format(status, body.strip()[:80]))

    status, headers, body = request("GET", "/diag/no-content")
    record("204 framing accepted by the host",
           status == 204 and headers.get("Content-Length") == "0",
           "status={0} Content-Length={1!r}".format(
               status, headers.get("Content-Length")))

    try:
        connection = connect()
    except Exception as exc:
        record("connect to MySQL", False, "{0}: {1}".format(type(exc).__name__, exc))
        print("\n" + "=" * 70)
        print("{0} of {1} checks passed".format(
            len([r for r in results if r[1]]), len(results)))
        print("Cannot continue without a database connection.")
        print("=" * 70)
        return 1

    write_key = "acc-" + secrets.token_hex(10)
    origin = BASE_URL
    event_id = "acc-" + secrets.token_hex(6)
    event_ids_to_clean = [event_id]
    tenant_id = None
    try:
        tenant_id = create_fixture(connection, write_key, origin)
        print("\nfixture tenant id {0}, origin {1}\n".format(tenant_id, origin))

        # --- Phase 2: CORS preflight -------------------------------------
        status, headers, body = request(
            "OPTIONS", "/api/v2/ev",
            headers={"Origin": origin,
                     "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "Content-Type"},
        )
        record("preflight grants a registered origin",
               status == 204 and headers.get("Access-Control-Allow-Origin") == origin,
               "status={0} ACAO={1!r}".format(
                   status, headers.get("Access-Control-Allow-Origin")))
        record("preflight keeps Content-Length: 0", 
               headers.get("Content-Length") == "0",
               "Content-Length={0!r}".format(headers.get("Content-Length")))

        status, headers, body = request(
            "OPTIONS", "/api/v2/ev",
            headers={"Origin": "https://not-allowed.example.com",
                     "Access-Control-Request-Method": "POST"},
        )
        record("preflight denies an unregistered origin",
               status == 204 and headers.get("Access-Control-Allow-Origin") is None,
               "status={0} ACAO={1!r}".format(
                   status, headers.get("Access-Control-Allow-Origin")))

        # --- Phase 2: beacon-shaped body (text/plain) ---------------------
        import hashlib

        email = "Buyer@Example.COM "
        email_hash = hashlib.sha256(
            email.strip().lower().encode("utf-8")).hexdigest()
        sdk_event = "acc-sdk-" + secrets.token_hex(6)
        event_ids_to_clean.append(sdk_event)
        sdk_payload = json.dumps({
            "wk": write_key,
            "event_id": sdk_event,
            "name": "purchase",
            "event_time": 1730000000,
            "consent_state": True,
            "user_data": {
                "em": email_hash,
                "fbp": "fb.1.1700000000.111",
                "fbc": "fb.1.1700000000.222",
                "client_user_agent": "acceptance-sdk/1.0",
            },
            "custom_data": {"value": 49.99, "currency": "USD"},
            "source": {"url": "http://localhost:8000/test.html"},
        })
        status, headers, body = request(
            "POST", "/api/v2/ev", body=sdk_payload,
            headers={"Origin": origin, "User-Agent": "acceptance-sdk/1.0"},
            content_type="text/plain;charset=UTF-8",
        )
        record("text/plain beacon body is accepted",
               status == 204,
               "status={0} body={1!r}".format(status, body.strip()[:120]))
        record("successful POST echoes the origin",
               headers.get("Access-Control-Allow-Origin") == origin,
               "ACAO={0!r}".format(headers.get("Access-Control-Allow-Origin")))

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload_json FROM events "
                "WHERE tenant_id = %s AND event_id = %s",
                (tenant_id, sdk_event),
            )
            sdk_row = cursor.fetchone()
        if sdk_row:
            stored_sdk = json.loads(sdk_row[0])
            user_data = stored_sdk.get("user_data") or {}
            record("hashed email stored, raw email absent",
                   user_data.get("em") == email_hash and
                   "Buyer@Example.COM" not in sdk_row[0],
                   "em={0}...".format((user_data.get("em") or "")[:12]))
            record("fbp/fbc and custom_data stored",
                   user_data.get("fbp") == "fb.1.1700000000.111" and
                   user_data.get("fbc") == "fb.1.1700000000.222" and
                   stored_sdk.get("custom_data", {}).get("value") == 49.99,
                   "fbp={0!r} fbc={1!r}".format(
                       user_data.get("fbp"), user_data.get("fbc")))
        else:
            record("beacon-shaped event readable", False, "row not found")

        # --- Phase 1: original wire checks --------------------------------
        status, headers, body = request(
            "POST", "/api/v2/ev",
            body=json.dumps({
                "wk": write_key,
                "event_id": event_id,
                "name": "page_view",
                "consent_state": True,
            }),
            headers={"Origin": origin, "User-Agent": "acceptance/1.0"},
        )
        record("valid key returns 204 over HTTPS", status == 204,
               "status={0} body={1!r}".format(status, body.strip()[:120]))

        # The beacon-shaped event above is already stored, so counts are
        # relative to it rather than absolute.
        stored = count_events(connection, tenant_id)
        record("the event landed in MySQL", stored == 2,
               "{0} row(s) for tenant {1} (sdk event + this one)".format(
                   stored, tenant_id))

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT client_ip_hash, user_agent, source_origin, consent_state, "
                "payload_json FROM events WHERE tenant_id = %s AND event_id = %s",
                (tenant_id, event_id),
            )
            row = cursor.fetchone()
        if row:
            ip_hash, user_agent, source_origin, consent, payload = row
            record("client IP stored as a 64-char hash",
                   ip_hash is not None and len(ip_hash) == 64,
                   "hash={0}... len={1}".format(
                       (ip_hash or "")[:12], len(ip_hash or "")))
            record("user_agent and source_origin stored",
                   user_agent == "acceptance/1.0" and source_origin == origin,
                   "ua={0!r} origin={1!r}".format(user_agent, source_origin))
            record("consent_state recorded as 1", consent == 1,
                   "consent_state={0!r}".format(consent))
            record("write key absent from the stored payload",
                   write_key not in (payload or ""),
                   "payload={0!r}".format((payload or "")[:80]))
        else:
            record("stored row readable", False, "row not found")

        status, _, body = request(
            "POST", "/api/v2/ev",
            body=json.dumps({"wk": write_key, "event_id": event_id,
                             "name": "page_view"}),
            headers={"Origin": origin},
        )
        after = count_events(connection, tenant_id)
        record("duplicate event_id stores nothing extra",
               status == 204 and after == 2,
               "status={0} rows={1}".format(status, after))

        bad_origin_event = "acc-bad-" + secrets.token_hex(6)
        status, _, body = request(
            "POST", "/api/v2/ev",
            body=json.dumps({"wk": write_key, "event_id": bad_origin_event,
                             "name": "page_view"}),
            headers={"Origin": "https://not-allowed.example.com"},
        )
        after_bad_origin = count_events(connection, tenant_id)
        record("bad origin writes nothing",
               status == 204 and after_bad_origin == 2,
               "status={0} rows={1}".format(status, after_bad_origin))
        if after_bad_origin != 2:
            event_ids_to_clean.append(bad_origin_event)

        status, _, body = request(
            "POST", "/api/v2/ev",
            body=json.dumps({"wk": write_key, "event_id": event_id,
                             "name": "x" * 40,
                             "pad": "z" * 20000}),
            headers={"Origin": origin},
        )
        record("oversize body returns 413", status == 413,
               "status={0}".format(status))
    finally:
        if tenant_id is not None:
            try:
                remove_fixture(connection, tenant_id, event_ids_to_clean)
                print("\nfixture removed")
            except Exception as exc:
                print("\nfixture cleanup failed: {0}".format(exc))
        connection.close()

    failures = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 70)
    print("{0} of {1} checks passed".format(len(results) - len(failures), len(results)))
    for name in failures:
        print("  FAILED: {0}".format(name))
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
