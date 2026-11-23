# SPDX-License-Identifier: Apache-2.0
"""Adapter registry.

Built-in adapters cover Meta, GA4 and TikTok. Private add-ons can register
themselves under the ``tracker.adapters`` entry-point group and are loaded on
demand, so the core never imports them directly (plan.md line 9).
"""

from .base import (  # noqa: F401  (re-exported for callers)
    DEAD,
    RETRY,
    SUCCESS,
    Adapter,
    post_json,
)
from .ga4 import GA4Adapter
from .meta import MetaAdapter
from .tiktok import TikTokAdapter

BUILTIN_ADAPTERS = {
    MetaAdapter.type: MetaAdapter,
    GA4Adapter.type: GA4Adapter,
    TikTokAdapter.type: TikTokAdapter,
}

ENTRY_POINT_GROUP = "tracker.adapters"

_plugin_cache = None


def _plugin_adapters():
    """Load entry-point adapters once, tolerating a broken plugin."""
    global _plugin_cache
    if _plugin_cache is not None:
        return _plugin_cache
    found = {}
    try:
        from importlib.metadata import entry_points

        for entry in entry_points(group=ENTRY_POINT_GROUP):
            try:
                adapter_class = entry.load()
            except Exception:
                # A broken add-on must not stop the forwarder from running.
                continue
            key = getattr(adapter_class, "type", None) or entry.name
            found[key] = adapter_class
    except Exception:
        pass
    _plugin_cache = found
    return found


def registered_types():
    """Return every adapter type available to the forwarder."""
    names = set(BUILTIN_ADAPTERS) | set(_plugin_adapters())
    return sorted(names)


def get_adapter(type_name):
    """Return the adapter class for *type_name* or raise KeyError."""
    if type_name in BUILTIN_ADAPTERS:
        return BUILTIN_ADAPTERS[type_name]
    plugins = _plugin_adapters()
    if type_name in plugins:
        return plugins[type_name]
    raise KeyError(
        "no adapter for {0!r}; known types: {1}".format(
            type_name, ", ".join(registered_types()) or "none")
    )
