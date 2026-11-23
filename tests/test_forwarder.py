# SPDX-License-Identifier: Apache-2.0
"""Forwarder tests: backoff, retry, dead-letter, and claim safety.

The store is an in-memory fake implementing the same methods as
``tracker.store.MySQLStore``, so the run logic is tested without MySQL. The
claim fake models the row lock: claimed rows leave the due list, which is what
makes a second concurrent run pick up different work.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.forwarder import Forwarder, backoff_seconds, redact


EVENT = {
    "row_id": 1,
    "tenant_id": 1,
    "event_id": "evt-1",
    "name": "purchase",
    "received_at": None,
    "received_at_unix": 1730000000,
    "payload": {},
    "user_agent": None,
    "source_origin": None,
    "consent_state": 1,
}
DESTINATION = {"id": 1, "tenant_id": 1, "type": "fake", "config": {}}


class FakeStore:
    """In-memory stand-in for MySQLStore."""

    def __init__(self, due, events=None, destinations=None, materialised=0):
        self.due = list(due)
        self.events = events if events is not None else {1: EVENT}
        self.destinations = (destinations if destinations is not None
                             else {1: DESTINATION})
        self.materialised = materialised
        self.delivered = []
        self.retried = []
        self.dead = []

    def materialise(self, limit):
        return self.materialised

    def claim(self, limit, lock_seconds):
        taken, self.due = self.due[:limit], self.due[limit:]
        for row in taken:
            row["attempts"] = row.get("attempts", 0) + 1
        return "token", [dict(row) for row in taken]

    def load_event(self, row_id):
        return self.events.get(row_id)

    def load_destination(self, destination_id):
        return self.destinations.get(destination_id)

    def mark_delivered(self, delivery_id, status_code):
        self.delivered.append((delivery_id, status_code))

    def mark_retry(self, delivery_id, delay, error, status_code):
        self.retried.append((delivery_id, delay, error, status_code))

    def mark_dead(self, delivery_id, error, status_code):
        self.dead.append((delivery_id, error, status_code))


def adapter_getter(verdict="success", status=200, body="ok", raises=None):
    class FakeAdapter:
        type = "fake"

        def __init__(self, config=None):
            self.config = config

        def deliver(self, event):
            if raises:
                raise raises
            return status, body, verdict

    return lambda type_name: FakeAdapter


def make_forwarder(store, **kwargs):
    kwargs.setdefault("base_delay", 10)
    kwargs.setdefault("max_delay", 40)
    kwargs.setdefault("max_attempts", 3)
    return Forwarder(store, adapter_getter=adapter_getter(**kwargs.pop("adapter", {})),
                     **kwargs)


def due_row(delivery_id=1, attempts=0, event_row_id=1, destination_id=1):
    return {"id": delivery_id, "tenant_id": 1, "event_row_id": event_row_id,
            "destination_id": destination_id, "attempts": attempts}


class SuccessTests(unittest.TestCase):
    def test_success_marks_delivered(self):
        store = FakeStore([due_row()], materialised=1)
        summary = make_forwarder(store).run_once()
        self.assertEqual(store.delivered, [(1, 200)])
        self.assertEqual(summary["delivered"], 1)
        self.assertEqual(summary["materialised"], 1)
        self.assertEqual(store.retried, [])
        self.assertEqual(store.dead, [])


class RetryTests(unittest.TestCase):
    def test_5xx_retries_with_backoff(self):
        store = FakeStore([due_row()])
        make_forwarder(store, adapter={"verdict": "retry", "status": 503}).run_once()
        self.assertEqual(len(store.retried), 1)
        delivery_id, delay, error, status = store.retried[0]
        self.assertEqual(delivery_id, 1)
        self.assertEqual(delay, 10)          # base * 2^0
        self.assertEqual(status, 503)
        self.assertEqual(store.dead, [])

    def test_backoff_doubles_and_caps(self):
        self.assertEqual(backoff_seconds(1, base=10, max_delay=1000), 10)
        self.assertEqual(backoff_seconds(2, base=10, max_delay=1000), 20)
        self.assertEqual(backoff_seconds(3, base=10, max_delay=1000), 40)
        self.assertEqual(backoff_seconds(9, base=10, max_delay=1000), 1000)
        self.assertEqual(backoff_seconds(0, base=10, max_delay=1000), 10)

    def test_attempts_exhausted_dead_letters(self):
        # max_attempts=3; the row has already been attempted twice.
        store = FakeStore([due_row(attempts=2)])
        summary = make_forwarder(store,
                                 adapter={"verdict": "retry", "status": 503}).run_once()
        self.assertEqual(store.retried, [])
        self.assertEqual(len(store.dead), 1)
        self.assertIn("gave up after 3 attempt(s)", store.dead[0][1])
        self.assertEqual(summary["dead"], 1)

    def test_transport_exception_is_retried(self):
        store = FakeStore([due_row()])
        make_forwarder(store, adapter={"raises": TimeoutError("timed out")}).run_once()
        self.assertEqual(len(store.retried), 1)
        self.assertIn("TimeoutError", store.retried[0][2])
        self.assertEqual(store.dead, [])


class DeadTests(unittest.TestCase):
    def test_4xx_dead_letters_immediately(self):
        store = FakeStore([due_row()])
        summary = make_forwarder(store,
                                 adapter={"verdict": "dead", "status": 400,
                                          "body": "bad request"}).run_once()
        self.assertEqual(len(store.dead), 1)
        self.assertEqual(store.dead[0][0], 1)
        self.assertIn("bad request", store.dead[0][1])
        self.assertEqual(store.retried, [])
        self.assertEqual(summary["dead"], 1)

    def test_unknown_adapter_type_dead_letters(self):
        store = FakeStore([due_row()])

        def getter(type_name):
            raise KeyError("no adapter for 'nope'")

        summary = Forwarder(store, adapter_getter=getter).run_once()
        self.assertEqual(len(store.dead), 1)
        self.assertIn("no adapter", store.dead[0][1])
        self.assertEqual(summary["dead"], 1)

    def test_missing_event_dead_letters(self):
        store = FakeStore([due_row(event_row_id=999)])
        make_forwarder(store).run_once()
        self.assertEqual(len(store.dead), 1)
        self.assertIn("event row not found", store.dead[0][1])

    def test_missing_destination_dead_letters(self):
        store = FakeStore([due_row(destination_id=999)])
        make_forwarder(store).run_once()
        self.assertEqual(len(store.dead), 1)
        self.assertIn("destination missing", store.dead[0][1])


class ClaimSafetyTests(unittest.TestCase):
    def test_two_runs_never_take_the_same_delivery(self):
        store = FakeStore([due_row(delivery_id=1), due_row(delivery_id=2)])
        first = make_forwarder(store, batch=1)
        second = make_forwarder(store, batch=1)

        first.run_once()
        second.run_once()

        delivered_ids = sorted(delivery_id for delivery_id, _ in store.delivered)
        self.assertEqual(delivered_ids, [1, 2])
        self.assertEqual(store.due, [])

    def test_a_third_run_finds_nothing(self):
        store = FakeStore([due_row()])
        make_forwarder(store).run_once()
        summary = make_forwarder(store).run_once()
        self.assertEqual(summary["claimed"], 0)
        self.assertEqual(len(store.delivered), 1)


class RedactionTests(unittest.TestCase):
    def test_redact_is_none_safe(self):
        self.assertIsNone(redact(None))

    def test_redact_keeps_ordinary_text(self):
        self.assertEqual(redact("connection refused"), "connection refused")


if __name__ == "__main__":
    unittest.main()
