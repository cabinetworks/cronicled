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
    """Adapters by name.

    `default` stays as a class attribute, always `None`, for one reason
    only: a caller that read `loaded.default` before this ticket must not
    start raising `AttributeError` on a value that used to be a real
    (if now meaningless) answer. It is never set from a config any more —
    see `load_adapters`'s own docstring for why the key it used to come
    from is refused at load time instead."""
    default = None


def default_adapters_path(env=None):
    return os.path.join(config_dir(env), "adapters.json")


def load_adapters(path=None, env=None):
    """The configured adapters, keyed by name. `path` defaults to
    `adapters.json` inside `cronicled.config.config_dir()` (see
    config/adapters.example.json for the shape); an explicit `path` is used
    exactly as given, unaffected by `$CRONICLED_CONFIG_DIR`. `env` defaults to
    `os.environ` but is injectable so a test can supply one without mutating
    the real environment.

    A `"default"` key at the top level is REFUSED, naming itself in the
    message, rather than silently ignored. It used to name the one adapter
    `--adapter` fell back to when no name was given on the command line;
    every scan now searches every configured adapter (see
    `cronicled.runscan.build_producer`), so the flag is gone and the key it
    fed has nothing left to mean. A key that goes on being accepted while
    quietly doing nothing is exactly how configuration drifts from
    behaviour — an operator reading their own `adapters.json` a year from
    now would have no way to tell "this orders something" from "this is
    inert" without reading this loader's source. Raising, and naming the
    key, turns that silent drift into one clear edit: delete the line."""
    if path is None:
        path = default_adapters_path(env)
    adapters = AdapterMap()
    if not os.path.exists(path):
        return adapters
    with open(path) as fh:
        payload = json.load(fh)
    if "default" in payload:
        raise ValueError(
            "adapters.json sets \"default\", which no longer means "
            "anything: every configured adapter is searched on every scan, "
            "and there is no single adapter left to prefer. Remove the "
            "\"default\" key.")
    for spec in payload.get("adapters") or []:
        adapters[spec["name"]] = DeclarativeAdapter(spec)
    return adapters
