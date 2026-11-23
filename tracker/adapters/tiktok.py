# SPDX-License-Identifier: Apache-2.0
"""TikTok Events API 1.3 adapter.

TikTok answers HTTP 200 even for many failures and reports the real outcome in
``code`` in the body, so ``classify_error`` inspects it.
"""

import json

from .base import DEAD, SUCCESS, TIKTOK_EVENT_NAMES, Adapter, custom_data, event_time, post_json, source, user_data

EVENT_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"

#: Status codes TikTok uses for rate limiting / transient server trouble.
RETRYABLE_CODES = frozenset([40100, 40101, 50000])


class TikTokAdapter(Adapter):
    type = "tiktok"

    def __init__(self, config=None, timeout=10):
        super().__init__(config, timeout)
        self.pixel_code = str(self.config.get("pixel_code") or "")
        self.access_token = str(self.config.get("access_token") or "")

    def map(self, event):
        data = user_data(event)
        user = {}
        if data.get("em"):
            user["email"] = data["em"]
        if data.get("ph"):
            user["phone"] = data["ph"]
        if data.get("client_user_agent"):
            user["user_agent"] = data["client_user_agent"]
        # TikTok's ttclid is what fbc encodes; pass the raw cookie when present.
        if data.get("ttclid"):
            user["ttclid"] = data["ttclid"]

        item = {
            "event": TIKTOK_EVENT_NAMES.get(event["name"], event["name"]),
            "event_time": event_time(event),
            "event_id": event["event_id"],
            "user": user,
            "properties": custom_data(event),
        }
        url = source(event).get("url")
        if url:
            item["page"] = {"url": url}
        return {
            "event_source": "web",
            "event_source_id": self.pixel_code,
            "data": [item],
        }

    def send(self, payload):
        return post_json(
            EVENT_URL,
            payload,
            headers={"Access-Token": self.access_token},
            timeout=self.timeout,
        )

    def classify_error(self, status_code, body):
        base = super().classify_error(status_code, body)
        if base != SUCCESS:
            return base
        # 2xx is not enough: TikTok reports the real result in the body.
        try:
            parsed = json.loads(body or "{}")
        except ValueError:
            return DEAD
        code = parsed.get("code")
        if code == 0:
            return SUCCESS
        if code in RETRYABLE_CODES:
            return "retry"
        return DEAD
