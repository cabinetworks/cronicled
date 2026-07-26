"""Adapters are loaded from the user's config, never compiled in.

A fresh install has no config: `load_adapters` returns an empty mapping rather
than raising, so the app starts and can tell the user what to configure. That
is the "absence is a legitimate state" half of the rule stated in
`cronicled.config`'s module docstring; `config.load_server` is the other half
and raises. Read that rule before adding a third loader — the difference
between these two is deliberate.
"""
import json
import os

from cronicled.adapters.declarative import DeclarativeAdapter
from cronicled.config import config_dir


class AdapterMap(dict):
    """Adapters by name, plus which one is the configured default. Carrying the
    default on the mapping keeps it tied to the config it came from — module-level
    state would let a second load silently change the answer for the first."""
    default = None


def default_adapters_path(env=None):
    return os.path.join(config_dir(env), "adapters.json")


def load_adapters(path=None, env=None):
    """The configured adapters, keyed by name. `path` defaults to
    `adapters.json` inside `cronicled.config.config_dir()` (see
    config/adapters.example.json for the shape); an explicit `path` is used
    exactly as given, unaffected by `$CRONICLED_CONFIG_DIR`. `env` defaults to
    `os.environ` but is injectable so a test can supply one without mutating
    the real environment."""
    if path is None:
        path = default_adapters_path(env)
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
