# SPDX-License-Identifier: Apache-2.0
"""Route-surface tests for the deployed receiver.

Everything runs offline; the receiver's validation is covered in
tests/test_receiver.py.

The diagnostic routes (/diag/db, /diag/outbound) were removed before Phase 2
because they leaked database and egress status on a public URL (decision D23).
These tests assert they stay gone.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import passenger_wsgi


class RouteSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.client = passenger_wsgi.application.test_client()

    def test_healthz_returns_ok_json(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_event_route_exists_and_handles_missing_key(self):
        """POST /api/v2/ev with no write key returns silent 204 (no key probing)."""
        response = self.client.post("/api/v2/ev", json={})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.data, b"")

    def test_no_content_probe_returns_framed_204(self):
        response = self.client.get("/diag/no-content")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers.get("Content-Length"), "0")
        self.assertEqual(response.data, b"")

    def test_diagnostic_routes_are_removed(self):
        """Public diagnostics must not come back: they expose DB/egress state."""
        for path in ("/diag/db", "/diag/outbound", "/diag/204"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
