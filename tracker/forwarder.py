# SPDX-License-Identifier: Apache-2.0
"""Forwarder: materialise deliveries, claim due rows, send, back off, dead-letter.

Runs to completion and exits; plan.md line 4 has cron start it every minute, so
there is no worker process. Concurrency safety comes from a claim: an UPDATE
takes the rows it will work on and stamps them with a per-run token, so a
second run cannot pick up the same delivery (plan.md Phase 3).
"""

import re

from .adapters import get_adapter
from .adapters.base import DEAD, RETRY, SUCCESS

DEFAULT_BATCH = 50
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BASE_DELAY = 60       # seconds; doubles per attempt
DEFAULT_MAX_DELAY = 3600      # cap, one hour
DEFAULT_LOCK_SECONDS = 120    # a crashed run releases its rows after this

_SECRET_PATTERNS = [
    re.compile(r"(api_secret=)[^&\s]+"),
    re.compile(r"(access_token=)[^&\s]+"),
    re.compile(r"(Access-Token:\s*)\S+", re.IGNORECASE),
]


def redact(text):
    """Remove credential-looking fragments from error text before storing it."""
    if text is None:
        return None
    cleaned = str(text)
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(r"\1[redacted]", cleaned)
    return cleaned


def backoff_seconds(attempts, base=DEFAULT_BASE_DELAY, max_delay=DEFAULT_MAX_DELAY):
    """Delay before the next attempt: base, base*2, base*4... capped.

    *attempts* is the number already made, so the first retry waits ``base``.
    """
    attempts = max(1, int(attempts))
    delay = base * (2 ** (attempts - 1))
    return min(delay, max_delay)


class Forwarder:
    """One pass over the delivery queue.

    *store* is any object implementing the methods used below; see
    ``tracker.store.MySQLStore``. Tests substitute an in-memory fake.
    """

    def __init__(self, store, adapter_getter=get_adapter,
                 batch=DEFAULT_BATCH,
                 max_attempts=DEFAULT_MAX_ATTEMPTS,
                 base_delay=DEFAULT_BASE_DELAY,
                 max_delay=DEFAULT_MAX_DELAY,
                 lock_seconds=DEFAULT_LOCK_SECONDS):
        self.store = store
        self.adapter_getter = adapter_getter
        self.batch = batch
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.lock_seconds = lock_seconds

    def run_once(self):
        """Materialise, claim and process one batch. Returns a summary dict."""
        materialised = self.store.materialise(self.batch)
        token, rows = self.store.claim(self.batch, self.lock_seconds)
        summary = {
            "materialised": materialised,
            "claimed": len(rows),
            "delivered": 0,
            "retried": 0,
            "dead": 0,
            "errors": [],
        }
        for row in rows:
            outcome = self._process(row)
            summary[outcome] = summary.get(outcome, 0) + 1
        return summary

    # -- internals --------------------------------------------------------

    def _process(self, row):
        delivery_id = row["id"]
        attempts = int(row.get("attempts") or 1)

        event = self.store.load_event(row["event_row_id"])
        if event is None:
            self.store.mark_dead(delivery_id, "event row not found", None)
            return "dead"

        destination = self.store.load_destination(row["destination_id"])
        if destination is None:
            self.store.mark_dead(delivery_id, "destination missing or inactive", None)
            return "dead"

        try:
            adapter_class = self.adapter_getter(destination["type"])
            adapter = adapter_class(destination.get("config") or {})
            status_code, body, verdict = adapter.deliver(event)
        except KeyError as exc:
            # No adapter registered for this destination type.
            self.store.mark_dead(delivery_id, redact(exc), None)
            return "dead"
        except Exception as exc:
            # Transport failure: DNS, TLS, timeout, connection reset.
            status_code, body, verdict = None, None, RETRY
            self.store.mark_retry(
                delivery_id,
                self._delay(attempts),
                redact("{0}: {1}".format(type(exc).__name__, exc)),
                None,
            )
            return "retried"

        if verdict == SUCCESS:
            self.store.mark_delivered(delivery_id, status_code)
            return "delivered"

        error = redact((body or "")[:500])

        if verdict == RETRY:
            if attempts >= self.max_attempts:
                self.store.mark_dead(
                    delivery_id,
                    "gave up after {0} attempt(s): {1}".format(attempts, error),
                    status_code,
                )
                return "dead"
            self.store.mark_retry(delivery_id, self._delay(attempts), error,
                                  status_code)
            return "retried"

        self.store.mark_dead(delivery_id, error, status_code)
        return "dead"

    def _delay(self, attempts):
        return backoff_seconds(attempts, self.base_delay, self.max_delay)
