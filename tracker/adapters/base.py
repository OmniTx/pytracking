# SPDX-License-Identifier: Apache-2.0
"""Adapter base class and the shared HTTP helper.

An adapter does three things, per plan.md Phase 3:

    map(event)               -> provider-shaped payload
    send(payload)            -> (status_code, body_text)
    classify_error(status, body) -> "success" | "retry" | "dead"

``deliver()`` composes them for the forwarder. Subclasses override ``map``
and often ``send``; ``classify_error`` defaults to the convention most
providers use and is overridden where they differ (TikTok answers 200 with a
non-zero ``code`` in the body).
"""

import json
import urllib.error
import urllib.request

SUCCESS = "success"
RETRY = "retry"
DEAD = "dead"

# Statuses worth retrying: request timeout, too early, rate limited, server
# errors. Everything else in the 4xx range is a permanent request problem.
RETRYABLE_STATUSES = frozenset([408, 425, 429])
MAX_ERROR_LEN = 500


def post_json(url, payload, headers=None, timeout=10):
    """POST *payload* as JSON and return ``(status_code, body_text)``.

    ``HTTPError`` is a result, not an exception, so a 4xx/5xx can be
    classified. Transport failures (DNS, TLS, timeout) propagate and the
    forwarder treats them as retryable.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def truncate(text):
    """Trim an error message to what the deliveries column can hold."""
    if text is None:
        return None
    text = str(text)
    return text[:MAX_ERROR_LEN]


class Adapter:
    """Base class for provider adapters."""

    #: Provider key stored in ``destinations.type``.
    type = "base"

    def __init__(self, config=None, timeout=10):
        self.config = dict(config or {})
        self.timeout = timeout

    # -- to override ------------------------------------------------------

    def map(self, event):
        """Return the provider payload for *event*, or raise NotImplementedError."""
        raise NotImplementedError

    def send(self, payload):
        """Deliver *payload*; return ``(status_code, body_text)``."""
        raise NotImplementedError

    # -- usually inherited ------------------------------------------------

    def classify_error(self, status_code, body):
        """Decide whether a response is a success, a retry, or permanent."""
        if status_code is None:
            return RETRY
        if 200 <= status_code < 300:
            return SUCCESS
        if status_code in RETRYABLE_STATUSES or 500 <= status_code < 600:
            return RETRY
        return DEAD

    def deliver(self, event):
        """Map, send, and classify in one call.

        Returns ``(status_code, body, verdict)``. Exceptions from ``send``
        propagate so the forwarder can record the transport failure.
        """
        payload = self.map(event)
        status_code, body = self.send(payload)
        return status_code, body, self.classify_error(status_code, body)


# Provider event names differ from the tracker's internal ones. Channels that
# require Meta's vocabulary share this table; GA4 keeps the internal names.
META_EVENT_NAMES = {
    "page_view": "PageView",
    "add_to_cart": "AddToCart",
    "checkout": "InitiateCheckout",
    "purchase": "Purchase",
}

TIKTOK_EVENT_NAMES = {
    "page_view": "ViewContent",
    "add_to_cart": "AddToCart",
    "checkout": "InitiateCheckout",
    "purchase": "CompletePayment",
}


def user_data(event):
    """Return the stored ``user_data`` mapping, or an empty dict."""
    payload = event.get("payload") or {}
    data = payload.get("user_data")
    return data if isinstance(data, dict) else {}


def custom_data(event):
    """Return the stored ``custom_data`` mapping, or an empty dict."""
    payload = event.get("payload") or {}
    data = payload.get("custom_data")
    return data if isinstance(data, dict) else {}


def source(event):
    """Return the stored ``source`` mapping, or an empty dict."""
    payload = event.get("payload") or {}
    data = payload.get("source")
    return data if isinstance(data, dict) else {}


def event_time(event):
    """Unix seconds for the event: the SDK's value, else the receive time."""
    payload = event.get("payload") or {}
    value = payload.get("event_time")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return int(event.get("received_at_unix") or 0)
