# SPDX-License-Identifier: Apache-2.0
"""Phase 1 receiver tests — data-protection scenarios.

All tests use mocked PyMySQL connections; no database or network is touched.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymysql.err import IntegrityError

import passenger_wsgi


DB_ENV = {
    "DB_HOST": "db.test.local",
    "DB_NAME": "pytracking_test",
    "DB_USER": "pytracking_test_user",
    "DB_PASSWORD": "replaceme-db-pass",
    "PYTRACKING_IP_SALT": "test-salt-for-hmac",
}


class _Conn:
    """Isolated per-query mock connection.

    Usage::

        conn = _Conn()
        conn.key_row = ("key-ok", 1)          # result of SELECT by write_key
        conn.origins = ["https://ok.com"]     # result of SELECT origins
        conn.events = []                      # records executed INSERTs

    *query()* accepts (sql, args) and appends to *events*.
    """

    def __init__(self):
        self.key_row = None              # (tenant_id, active) for SELECT ... tenant_keys
        self.origins = []                # origin strings for SELECT ... tenant_origins
        self.events = []                 # [(query, args)] for tracking inserts
        self.open = True

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def close(self):
        pass


class _Cursor:
    """Per-call cursor attached to a *_Conn*."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, args=None):
        if "INSERT INTO events" in query:
            self.conn.events.append((query, args))
            return 1
        # other queries record nothing here; results come from conn.*
        return 0

    def fetchone(self):
        return self.conn.key_row

    def fetchall(self):
        # Real PyMySQL returns a sequence of row tuples; mirror that.
        return [(o,) for o in self.conn.origins]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _payload_with_size(content_len):
    """Build JSON whose raw length is roughly *content_len* bytes."""
    base = '{"wk":"v","event_id":"e","name":"n"}'
    fill = content_len - len(base)
    if fill <= 0:
        return base
    return base + ' "f":"' + "x" * fill + '"'


def _valid_body(**overrides):
    defaults = {"wk": "valid-key", "event_id": "evt-001", "name": "page_view"}
    defaults.update(overrides)
    return json.dumps(defaults), {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Unknown/inactive key writes nothing
# ---------------------------------------------------------------------------

class UnknownKeyTests(unittest.TestCase):

    def setUp(self):
        self.client = passenger_wsgi.application.test_client()
        self.env_patch = patch.dict(os.environ, DB_ENV)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_unknown_key_silent_204(self):
        conn = _Conn()
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, headers = _valid_body()
        resp = self.client.post("/api/v2/ev", data=payload, headers=headers)
        self.assertEqual(resp.status_code, 204)

    def test_no_insert_on_unknown_key(self):
        conn = _Conn()                       # key_row stays None → lookup fails
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, headers = _valid_body()
        self.client.post("/api/v2/ev", data=payload, headers=headers)
        for q, _ in conn.events:
            self.assertNotIn("INSERT", q)

    def test_inactive_key_silent_204(self):
        conn = _Conn()
        conn.key_row = (99, False)           # inactive tenant
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, headers = _valid_body()
        resp = self.client.post("/api/v2/ev", data=payload, headers=headers)
        self.assertEqual(resp.status_code, 204)

    def test_no_insert_on_inactive_key(self):
        conn = _Conn()
        conn.key_row = (99, False)
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, headers = _valid_body()
        self.client.post("/api/v2/ev", data=payload, headers=headers)
        for q, _ in conn.events:
            self.assertNotIn("INSERT", q)


# ---------------------------------------------------------------------------
# Bad origin writes nothing
# ---------------------------------------------------------------------------

class BadOriginTests(unittest.TestCase):

    def setUp(self):
        self.client = passenger_wsgi.application.test_client()
        self.env_patch = patch.dict(os.environ, DB_ENV)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_mismatched_origin_rejected(self):
        conn = _Conn()
        conn.key_row = (1, 1)               # active tenant
        conn.origins = ["https://allowed.example.com"]
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://evil.example.com"}
        resp = self.client.post("/api/v2/ev", data=payload, headers=merged)
        self.assertEqual(resp.status_code, 204)

    def test_no_insert_on_bad_origin(self):
        conn = _Conn()
        conn.key_row = (1, 1)
        conn.origins = ["https://allowed.example.com"]
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://evil.example.com"}
        self.client.post("/api/v2/ev", data=payload, headers=merged)
        for q, _ in conn.events:
            self.assertNotIn("INSERT", q)


# ---------------------------------------------------------------------------
# Oversize body returns 413
# ---------------------------------------------------------------------------

class OversizeBodyTests(unittest.TestCase):

    def setUp(self):
        self.client = passenger_wsgi.application.test_client()
        self.env_patch = patch.dict(os.environ, DB_ENV)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_over_16kb_returns_413(self):
        payload = _payload_with_size(22000)
        headers = {"Content-Type": "application/json"}
        p = patch.object(passenger_wsgi.pymysql, "connect")
        p.start()
        self.addCleanup(p.stop)
        resp = self.client.post("/api/v2/ev", data=payload, headers=headers)
        self.assertEqual(resp.status_code, 413)
        passenger_wsgi.pymysql.connect.assert_not_called()

    def test_below_limit_reaches_db(self):
        conn = _Conn()                     # unknown key → silent 204
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload = json.dumps({"wk": "", "event_id": "e", "name": "n"})
        headers = {"Content-Type": "application/json"}
        resp = self.client.post("/api/v2/ev", data=payload, headers=headers)
        self.assertNotEqual(resp.status_code, 413)


# ---------------------------------------------------------------------------
# Duplicate event_id stores one row
# ---------------------------------------------------------------------------

class _UniqueDb:
    """Fake DB that enforces UNIQUE(tenant_id, event_id) like MySQL.

    Rows are stored in *rows* so tests can assert what was persisted.
    """

    def __init__(self, key_row=(1, 1), origins=None):
        self.key_row = key_row
        self.origins = origins or []
        self.rows = {}                   # (tenant_id, event_id) -> args tuple
        self.insert_attempts = 0
        self.open = True

    def cursor(self):
        return _UniqueCursor(self)

    def commit(self):
        pass

    def close(self):
        pass


class _UniqueCursor:
    def __init__(self, db):
        self.db = db

    def execute(self, query, args=None):
        if "INSERT INTO events" in query:
            self.db.insert_attempts += 1
            key = (args[0], args[1])     # (tenant_id, event_id)
            if key in self.db.rows:
                raise IntegrityError(1062, "Duplicate entry")
            self.db.rows[key] = args
        return 0

    def fetchone(self):
        return self.db.key_row

    def fetchall(self):
        # Real PyMySQL returns a sequence of row tuples; mirror that.
        return [(o,) for o in self.db.origins]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class DuplicateEventIdTests(unittest.TestCase):

    def setUp(self):
        self.client = passenger_wsgi.application.test_client()
        self.env_patch = patch.dict(os.environ, DB_ENV)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_duplicate_event_id_returns_204(self):
        """Replaying the same event_id is accepted (204), not an error."""
        db = _UniqueDb(origins=["https://shop.example.com"])
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=db)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://shop.example.com"}
        resp1 = self.client.post("/api/v2/ev", data=payload, headers=merged)
        resp2 = self.client.post("/api/v2/ev", data=payload, headers=merged)
        self.assertEqual(resp1.status_code, 204)
        self.assertEqual(resp2.status_code, 204)

    def test_only_one_row_stored_on_dedupe(self):
        """Three posts of the same event_id persist exactly one row."""
        db = _UniqueDb(origins=["https://shop.example.com"])
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=db)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://shop.example.com"}
        for _ in range(3):
            resp = self.client.post("/api/v2/ev", data=payload, headers=merged)
            self.assertEqual(resp.status_code, 204)

        self.assertEqual(len(db.rows), 1)
        self.assertEqual(db.insert_attempts, 3)  # every replay hit the INSERT


# ---------------------------------------------------------------------------
# Raw IP never appears in stored row
# ---------------------------------------------------------------------------

class RawIpProtectionTests(unittest.TestCase):

    def setUp(self):
        self.client = passenger_wsgi.application.test_client()
        self.env_patch = patch.dict(os.environ, DB_ENV)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_hashed_not_stored_raw(self):
        remote_ip = "203.0.113.42"

        # One connection handles key lookup, origin check, and the INSERT.
        conn = _Conn()
        conn.key_row = (1, 1)
        conn.origins = ["https://shop.example.com"]

        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)

        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://shop.example.com"}
        resp = self.client.post(
            "/api/v2/ev", data=payload, headers=merged,
            environ_base={"REMOTE_ADDR": remote_ip},
        )
        self.assertEqual(resp.status_code, 204)

        # Exactly one INSERT was recorded.
        self.assertEqual(len(conn.events), 1)
        query, args = conn.events[0]
        stored_hash = args[5]  # client_ip_hash is the 6th positional arg

        self.assertEqual(len(stored_hash), 64)
        self.assertRegex(stored_hash, r'^[0-9a-f]{64}$')
        # Raw IP must NOT appear anywhere in the stored args.
        for arg in args:
            self.assertNotIn(remote_ip, str(arg))

    def test_different_ips_produce_different_hashes(self):
        h1 = passenger_wsgi.hash_client_ip("10.0.0.1")
        h2 = passenger_wsgi.hash_client_ip("10.0.0.2")
        self.assertNotEqual(h1, h2)

    def test_same_ip_always_same_hash(self):
        self.assertEqual(passenger_wsgi.hash_client_ip("1.2.3.4"),
                         passenger_wsgi.hash_client_ip("1.2.3.4"))

    def test_write_key_not_stored_in_payload(self):
        """The write key must never be persisted in the stored payload."""
        secret_key = "super-secret-write-key"
        conn = _Conn()
        conn.key_row = (1, 1)
        conn.origins = ["https://shop.example.com"]
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)

        payload, hdrs = _valid_body(wk=secret_key, event_id="evt-9", name="purchase")
        merged = {**hdrs, "Origin": "https://shop.example.com"}
        resp = self.client.post("/api/v2/ev", data=payload, headers=merged)
        self.assertEqual(resp.status_code, 204)

        self.assertEqual(len(conn.events), 1)
        query, args = conn.events[0]
        stored_payload = args[3]  # payload_json is the 4th positional arg
        self.assertNotIn(secret_key, stored_payload)
        self.assertNotIn("wk", json.loads(stored_payload))

    # --- the 204 must carry no headers at all (decision D6) -----------------

    def _assert_bare_204(self, resp):
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.data, b"")
        self.assertEqual(dict(resp.headers), {})

    def test_successful_event_returns_bare_204(self):
        conn = _Conn()
        conn.key_row = (1, 1)
        conn.origins = ["https://shop.example.com"]
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://shop.example.com"}
        self._assert_bare_204(self.client.post("/api/v2/ev", data=payload, headers=merged))

    def test_duplicate_returns_bare_204(self):
        db = _UniqueDb(origins=["https://shop.example.com"])
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=db)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://shop.example.com"}
        self.client.post("/api/v2/ev", data=payload, headers=merged)
        self._assert_bare_204(self.client.post("/api/v2/ev", data=payload, headers=merged))

    def test_unknown_key_returns_bare_204(self):
        conn = _Conn()  # key_row None -> unknown
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        self._assert_bare_204(self.client.post("/api/v2/ev", data=payload, headers=hdrs))

    def test_inactive_key_returns_bare_204(self):
        conn = _Conn()
        conn.key_row = (99, False)
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        self._assert_bare_204(self.client.post("/api/v2/ev", data=payload, headers=hdrs))

    def test_bad_origin_returns_bare_204(self):
        conn = _Conn()
        conn.key_row = (1, 1)
        conn.origins = ["https://allowed.example.com"]
        p = patch.object(passenger_wsgi.pymysql, "connect", return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        payload, hdrs = _valid_body()
        merged = {**hdrs, "Origin": "https://evil.example.com"}
        self._assert_bare_204(self.client.post("/api/v2/ev", data=payload, headers=merged))

    def test_missing_key_returns_bare_204(self):
        self._assert_bare_204(self.client.post("/api/v2/ev", json={}))


if __name__ == "__main__":
    unittest.main()
