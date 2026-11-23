# SPDX-License-Identifier: Apache-2.0
"""Meta Conversions API adapter."""

import json

from .base import (
    DEAD,
    RETRY,
    META_EVENT_NAMES,
    Adapter,
    custom_data,
    event_time,
    post_json,
    source,
    user_data,
)

#: Fields the Events API accepts inside user_data. Anything else in the stored
#: payload is not forwarded, so an unexpected key cannot leak.
_META_USER_FIELDS = ("em", "ph", "fbp", "fbc", "client_user_agent", "country", "zip")

#: Meta reports throttling and transient trouble as HTTP 400 with an error
#: code in the body, so status alone would dead-letter a retryable failure.
#: 1 unknown, 2 service unavailable, 4/17/32/341/613/80004 rate limits.
_RETRYABLE_CODES = frozenset([1, 2, 4, 17, 32, 341, 613, 80004])


class MetaAdapter(Adapter):
    type = "meta"

    def __init__(self, config=None, timeout=10):
        super().__init__(config, timeout)
        self.pixel_id = str(self.config.get("pixel_id") or "")
        self.access_token = str(self.config.get("access_token") or "")
        self.api_version = str(self.config.get("api_version") or "v20.0")
        self.test_event_code = self.config.get("test_event_code")

    def endpoint(self):
        return "https://graph.facebook.com/{0}/{1}/events".format(
            self.api_version, self.pixel_id
        )

    def map(self, event):
        data = user_data(event)
        payload = {
            "data": [
                {
                    "event_name": META_EVENT_NAMES.get(
                        event["name"], event["name"]),
                    # event_id gives Meta deduplication against a pixel call.
                    "event_id": event["event_id"],
                    "event_time": event_time(event),
                    "action_source": "website",
                    "user_data": {
                        key: data[key]
                        for key in _META_USER_FIELDS
                        if data.get(key)
                    },
                    "custom_data": custom_data(event),
                }
            ],
            # In the body rather than the query string, so it stays out of URLs.
            "access_token": self.access_token,
        }
        url = source(event).get("url")
        if url:
            payload["data"][0]["event_source_url"] = url
        if self.test_event_code:
            payload["test_event_code"] = self.test_event_code
        return payload

    def send(self, payload):
        return post_json(self.endpoint(), payload, timeout=self.timeout)

    def classify_error(self, status_code, body):
        base = super().classify_error(status_code, body)
        if base != DEAD:
            return base
        # A 400 may still be a rate limit or a transient Meta problem; the
        # body carries the code that distinguishes it from a bad request.
        try:
            parsed = json.loads(body or "{}")
        except ValueError:
            return base
        error = parsed.get("error")
        if not isinstance(error, dict):
            return base
        if error.get("code") in _RETRYABLE_CODES:
            return RETRY
        return base
