# SPDX-License-Identifier: Apache-2.0
"""Adapter tests: payload mapping and error classification.

Each adapter is pointed at a local fake HTTP server (stdlib http.server), so
``send`` runs for real over a socket without touching a provider. No network
beyond 127.0.0.1.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.adapters import get_adapter, registered_types
from tracker.adapters.base import DEAD, RETRY, SUCCESS
from tracker.adapters.ga4 import GA4Adapter
from tracker.adapters.meta import MetaAdapter
from tracker.adapters.tiktok import TikTokAdapter
from tracker.forwarder import redact


def make_event(name="purchase", **overrides):
    event = {
        "row_id": 1,
        "tenant_id": 1,
        "event_id": "evt-1",
        "name": name,
        "received_at": None,
        "received_at_unix": 1730000000,
        "payload": {
            "event_time": 1730000000,
            "user_data": {
                "em": "a" * 64,
                "ph": "b" * 64,
                "fbp": "fb.1.111",
                "fbc": "fb.1.222",
                "client_user_agent": "unit-test-agent",
            },
            "custom_data": {"value": 49.99, "currency": "USD"},
            "source": {"url": "https://shop.example/p/1"},
        },
        "user_agent": "unit-test-agent",
        "source_origin": "https://shop.example",
        "consent_state": 1,
    }
    event.update(overrides)
    return event


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.last_body = self.rfile.read(length)
        self.server.last_headers = dict(self.headers)
        self.server.last_path = self.path
        body, status = self.server.response
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class HTTPServerFixture:
    """A throwaway HTTP server that returns a canned response."""

    def __init__(self, body="{}", status=200):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.response = (body, status)
        self.server.last_body = b""
        self.server.last_headers = {}
        self.server.last_path = ""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        host, port = self.server.server_address
        return "http://{0}:{1}/collect".format(host, port)

    def body(self):
        return json.loads(self.server.last_body.decode("utf-8"))


class MetaAdapterTests(unittest.TestCase):
    class _Local(MetaAdapter):
        def __init__(self, config, url):
            super().__init__(config)
            self._url = url

        def endpoint(self):
            return self._url

    def make(self, url):
        return self._Local({"pixel_id": "123", "access_token": "secret-token",
                            "test_event_code": "TEST1"}, url)

    def test_map_uses_provider_event_names(self):
        adapter = self.make("http://127.0.0.1:1/")
        for internal, expected in (("page_view", "PageView"),
                                   ("add_to_cart", "AddToCart"),
                                   ("checkout", "InitiateCheckout"),
                                   ("purchase", "Purchase")):
            payload = adapter.map(make_event(internal))
            self.assertEqual(payload["data"][0]["event_name"], expected)

    def test_map_carries_event_id_and_time_for_dedupe(self):
        payload = self.make("http://127.0.0.1:1/").map(make_event())
        item = payload["data"][0]
        self.assertEqual(item["event_id"], "evt-1")
        self.assertEqual(item["event_time"], 1730000000)
        self.assertEqual(item["action_source"], "website")
        self.assertEqual(item["event_source_url"], "https://shop.example/p/1")
        self.assertEqual(payload["test_event_code"], "TEST1")

    def test_map_whitelists_user_data(self):
        event = make_event()
        event["payload"]["user_data"]["unexpected"] = "should-not-forward"
        payload = self.make("http://127.0.0.1:1/").map(event)
        self.assertNotIn("unexpected", payload["data"][0]["user_data"])
        self.assertEqual(payload["data"][0]["user_data"]["em"], "a" * 64)

    def test_send_posts_body_with_access_token(self):
        with HTTPServerFixture('{"events_received":1}', 200) as server:
            status, body, verdict = self.make(server.url).deliver(make_event())
            self.assertEqual(status, 200)
            self.assertEqual(verdict, SUCCESS)
            sent = server.body()
            self.assertEqual(sent["access_token"], "secret-token")
            self.assertEqual(server.server.last_headers.get("Content-Type"),
                             "application/json")

    def test_5xx_is_retry(self):
        with HTTPServerFixture('{"error":"boom"}', 503) as server:
            status, body, verdict = self.make(server.url).deliver(make_event())
            self.assertEqual(status, 503)
            self.assertEqual(verdict, RETRY)

    def test_4xx_is_dead(self):
        with HTTPServerFixture('{"error":"bad"}', 400) as server:
            status, body, verdict = self.make(server.url).deliver(make_event())
            self.assertEqual(status, 400)
            self.assertEqual(verdict, DEAD)

    def test_429_is_retry(self):
        with HTTPServerFixture("{}", 429) as server:
            self.assertEqual(self.make(server.url).deliver(make_event())[2], RETRY)


class GA4AdapterTests(unittest.TestCase):
    class _Local(GA4Adapter):
        def __init__(self, config, url):
            super().__init__(config)
            self._url = url

        def endpoint(self):
            return self._url

    def make(self, url):
        return self._Local({"measurement_id": "G-TEST",
                            "api_secret": "secret-value"}, url)

    def test_map_uses_fbp_as_client_id(self):
        payload = self.make("http://127.0.0.1:1/").map(make_event())
        self.assertEqual(payload["client_id"], "fb.1.111")
        self.assertEqual(payload["events"][0]["name"], "purchase")
        self.assertEqual(payload["events"][0]["params"]["event_id"], "evt-1")

    def test_map_falls_back_to_event_id(self):
        event = make_event()
        del event["payload"]["user_data"]["fbp"]
        self.assertEqual(self.make("http://127.0.0.1:1/").map(event)["client_id"],
                         "evt-1")

    def test_204_is_success(self):
        with HTTPServerFixture("", 204) as server:
            status, body, verdict = self.make(server.url).deliver(make_event())
            self.assertEqual(status, 204)
            self.assertEqual(verdict, SUCCESS)

    def test_500_is_retry(self):
        with HTTPServerFixture("", 500) as server:
            self.assertEqual(self.make(server.url).deliver(make_event())[2], RETRY)


class TikTokAdapterTests(unittest.TestCase):
    def make(self):
        return TikTokAdapter({"pixel_code": "PIX1", "access_token": "tt-token"})

    def test_map_uses_tiktok_event_names(self):
        payload = self.make().map(make_event("purchase"))
        self.assertEqual(payload["data"][0]["event"], "CompletePayment")
        self.assertEqual(payload["event_source_id"], "PIX1")
        self.assertEqual(payload["data"][0]["user"]["email"], "a" * 64)
        self.assertEqual(payload["data"][0]["page"]["url"],
                         "https://shop.example/p/1")

    def test_200_with_code_zero_is_success(self):
        with HTTPServerFixture('{"code":0,"message":"OK"}', 200) as server:
            with patch("tracker.adapters.tiktok.EVENT_URL", server.url):
                status, body, verdict = self.make().deliver(make_event())
                self.assertEqual(status, 200)
                self.assertEqual(verdict, SUCCESS)
                self.assertEqual(server.server.last_headers.get("Access-Token"),
                                 "tt-token")

    def test_200_with_nonzero_code_is_dead(self):
        with HTTPServerFixture('{"code":40002,"message":"bad token"}', 200) as server:
            with patch("tracker.adapters.tiktok.EVENT_URL", server.url):
                self.assertEqual(self.make().deliver(make_event())[2], DEAD)

    def test_500_is_retry(self):
        with HTTPServerFixture("{}", 500) as server:
            with patch("tracker.adapters.tiktok.EVENT_URL", server.url):
                self.assertEqual(self.make().deliver(make_event())[2], RETRY)


class RegistryTests(unittest.TestCase):
    def test_builtin_types_registered(self):
        for name in ("meta", "ga4", "tiktok"):
            self.assertIn(name, registered_types())
            self.assertTrue(issubclass(get_adapter(name), object))

    def test_unknown_type_raises(self):
        with self.assertRaises(KeyError):
            get_adapter("no-such-provider")


class RedactionTests(unittest.TestCase):
    def test_secrets_are_removed_from_error_text(self):
        text = ("HTTP 400 for https://www.google-analytics.com/mp/collect?"
                "measurement_id=G-1&api_secret=super-secret-value and "
                "access_token=abc123")
        cleaned = redact(text)
        self.assertNotIn("super-secret-value", cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertIn("api_secret=[redacted]", cleaned)


if __name__ == "__main__":
    unittest.main()
