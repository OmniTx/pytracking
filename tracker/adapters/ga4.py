# SPDX-License-Identifier: Apache-2.0
"""GA4 Measurement Protocol adapter."""

import urllib.parse

from .base import Adapter, custom_data, post_json, user_data

MEASUREMENT_URL = "https://www.google-analytics.com/mp/collect"


class GA4Adapter(Adapter):
    type = "ga4"

    def __init__(self, config=None, timeout=10):
        super().__init__(config, timeout)
        self.measurement_id = str(self.config.get("measurement_id") or "")
        self.api_secret = str(self.config.get("api_secret") or "")

    def endpoint(self):
        # The Measurement Protocol requires the secret in the query string.
        # Keep this URL out of logs and error text.
        query = urllib.parse.urlencode(
            {"measurement_id": self.measurement_id, "api_secret": self.api_secret}
        )
        return "{0}?{1}".format(MEASUREMENT_URL, query)

    def map(self, event):
        data = user_data(event)
        # GA4 keys everything on a client_id. Use the Meta browser id when the
        # page had one, else the event id, so repeats stay consistent.
        client_id = data.get("fbp") or event["event_id"]
        params = dict(custom_data(event))
        params["event_id"] = event["event_id"]
        if event.get("user_agent"):
            params.setdefault("user_agent", event["user_agent"])
        return {
            "client_id": client_id,
            "events": [{"name": event["name"], "params": params}],
        }

    def send(self, payload):
        return post_json(self.endpoint(), payload, timeout=self.timeout)
