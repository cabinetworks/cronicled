"""Adapters are loaded from the user's config, never compiled in.

A fresh install has no config: `load_adapters` returns an empty mapping rather
than raising, so the app starts and can tell the user what to configure.
"""
import json
import os

from cronicled.adapters.declarative import DeclarativeAdapter

DEFAULT_CONFIG_PATH = os.path.join("config", "adapters.json")


class AdapterMap(dict):
    """Adapters by name, plus which one is the configured default. Carrying the
    default on the mapping keeps it tied to the config it came from — module-level
    state would let a second load silently change the answer for the first."""
    default = None


def load_adapters(path=DEFAULT_CONFIG_PATH):
    adapters = AdapterMap()
    if not os.path.exists(path):
        return adapters
    with open(path) as fh:
        payload = json.load(fh)
    for spec in payload.get("adapters") or []:
        adapters[spec["name"]] = DeclarativeAdapter(spec)
    adapters.default = payload.get("default")
    return adapters


def get_adapter(name, adapters):
    """The named adapter, or the configured default when `name` is None. Raises
    KeyError naming what was available, so a typo is obvious."""
    if name is None:
        name = getattr(adapters, "default", None)
        if name is None and len(adapters) == 1:
            return list(adapters.values())[0]
    if name not in adapters:
        raise KeyError("no adapter %r configured (have: %s)"
                       % (name, ", ".join(sorted(adapters)) or "none"))
    return adapters[name]
