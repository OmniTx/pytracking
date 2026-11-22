# SPDX-License-Identifier: Apache-2.0
"""Phase 0 host-validation tests.

Everything runs offline: the MySQL connection and the outbound HTTPS call
are mocked, so no network or database access is required.

After Phase 1, these tests focus on health/diag routes; receiver validation
is covered in tests/test_receiver.py.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymysql.err import OperationalError

import passenger_wsgi


DB_ENV = {
    "DB_HOST": "db.test.local",
    "DB_NAME": "pytracking_test",
    "DB_USER": "pytracking_test_user",
    "DB_PASSWORD": "placeholder-not-a-real-secret",
}


class ReceiverTests(unittest.TestCase):
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


class DiagDbTests(unittest.TestCase):
    def setUp(self):
        self.client = passenger_wsgi.application.test_client()
        env = patch.dict(os.environ, DB_ENV)
        env.start()
        self.addCleanup(env.stop)

    @patch("passenger_wsgi.pymysql.connect")
    def test_db_ok(self, mock_connect):
        mock_connection = MagicMock()
        mock_connect.return_value = mock_connection

        response = self.client.get("/diag/db")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"db": "ok"})
        mock_connect.assert_called_once()
        kwargs = mock_connect.call_args.kwargs
        self.assertEqual(kwargs["host"], DB_ENV["DB_HOST"])
        self.assertEqual(kwargs["database"], DB_ENV["DB_NAME"])
        self.assertEqual(kwargs["user"], DB_ENV["DB_USER"])
        self.assertEqual(kwargs["password"], DB_ENV["DB_PASSWORD"])
        mock_connection.close.assert_called_once()

    @patch("passenger_wsgi.pymysql.connect")
    def test_db_error_reports_message_without_password(self, mock_connect):
        mock_connect.side_effect = OperationalError(
            1045,
            "Access denied for user '%s'@'localhost' (using password: YES)"
            % DB_ENV["DB_USER"],
        )

        response = self.client.get("/diag/db")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["db"], "error")
        self.assertIn("Access denied", body["message"])
        self.assertNotIn(DB_ENV["DB_PASSWORD"], response.get_data(as_text=True))


class DiagOutboundTests(unittest.TestCase):
    def setUp(self):
        self.client = passenger_wsgi.application.test_client()

    @patch("passenger_wsgi.urllib.request.urlopen")
    def test_success_reports_status_and_elapsed(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        response = self.client.get("/diag/outbound")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], 200)
        self.assertIsInstance(body["elapsed_ms"], float)
        self.assertGreaterEqual(body["elapsed_ms"], 0)
        mock_urlopen.assert_called_once()
        self.assertEqual(
            mock_urlopen.call_args.args[0], "https://graph.facebook.com/"
        )
        self.assertEqual(mock_urlopen.call_args.kwargs["timeout"], 5)

    @patch("passenger_wsgi.urllib.request.urlopen")
    def test_http_error_reports_status_code(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://graph.facebook.com/", 400, "Bad Request", {}, None
        )

        response = self.client.get("/diag/outbound")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], 400)
        self.assertIsInstance(body["elapsed_ms"], float)

    @patch("passenger_wsgi.urllib.request.urlopen")
    def test_transport_error_reports_exception(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("connection refused")

        response = self.client.get("/diag/outbound")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["error"], "URLError")
        self.assertIn("connection refused", body["message"])
        self.assertIsInstance(body["elapsed_ms"], float)

    @patch("passenger_wsgi.urllib.request.urlopen")
    def test_timeout_reports_exception(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")

        response = self.client.get("/diag/outbound")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["error"], "TimeoutError")
        self.assertEqual(body["message"], "timed out")


if __name__ == "__main__":
    unittest.main()
