"""Media-server connection config: environment first, then a JSON file, and
never a compiled-in default. The operator's host and API key identify their own
machine, so a fallback baked into the source would leak it into a public repo —
every deployment must supply both explicitly, one way or the other.

This project's OWN config directory (where `server.json` and `adapters.json`
live) is a separate concern from the above, and is resolved by `config_dir`
below, shared with `cronicled.adapters.registry`. `$STASH_URL`/`$STASH_API_KEY`
deliberately stay their own thing rather than folding into that directory
scheme: they name the media server being managed, not this project, and an
operator may already have them set for a reason of their own. Renaming either
half to match the other would stop reading an environment that already works,
so the split stays.

THE RULE A NEW CONFIG LOADER FOLLOWS
------------------------------------
Configuration the thing cannot function without RAISES, naming exactly which
values were missing and where they could have come from. Configuration whose
absence is a legitimate state RETURNS AN EMPTY VALUE, so the app still starts
and can tell the operator what to configure.

That is why `load_server` below raises while
`cronicled.adapters.registry.load_adapters` returns an empty mapping: nothing
can reach the media server without a URL and a key, whereas a fresh install
with no adapters configured is a normal state and not an error. The asymmetry
is deliberate, not an inconsistency to tidy away — making the two agree would
either hand a URL-less client to the network layer or stop a fresh install
from starting. Both halves are pinned by tests in tests/test_config.py.

Two shapes every loader here shares, whichever half of the rule it falls
under: `path=None` resolved INSIDE the function (a default bound in the
signature is fixed at import time and so cannot see `$CRONICLED_CONFIG_DIR`),
and `env=None` defaulting to `os.environ` but injectable, so a test never has
to mutate the real environment.
"""
import json
import os

CONFIG_DIR_ENV_VAR = "CRONICLED_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "config"


def config_dir(env=None):
    """The directory holding this project's own config files.

    Precedence: `$CRONICLED_CONFIG_DIR` if it is set (this is also what the
    Dockerfile sets and declares as a volume, so a container's mounted
    `/config` is picked up automatically); otherwise a `config/` directory
    relative to the current working directory. `env` defaults to
    `os.environ` but is injectable so a test can supply one without mutating
    the real environment.

    This is the ONE place that decision is made — `load_server` and
    `load_adapters` both resolve their default file through it, rather than
    each keeping its own constant, which is how this project has drifted
    before."""
    if env is None:
        env = os.environ
    return env.get(CONFIG_DIR_ENV_VAR) or DEFAULT_CONFIG_DIR


def default_server_path(env=None):
    return os.path.join(config_dir(env), "server.json")


def load_server(path=None, env=None):
    """{"url": ..., "api_key": ...} for the media server.

    `$STASH_URL`/`$STASH_API_KEY` win over the file at `path` (default:
    `server.json` inside `config_dir()`, see config/server.example.json for
    the shape). `env` defaults to `os.environ` but is injectable so a test can
    supply one without mutating the real environment. Raises ValueError naming
    exactly which of "url"/"api_key" neither source supplied."""
    if env is None:
        env = os.environ
    if path is None:
        path = default_server_path(env)

    file_data = {}
    if os.path.exists(path):
        with open(path) as fh:
            file_data = json.load(fh)

    url = env.get("STASH_URL") or file_data.get("url")
    api_key = env.get("STASH_API_KEY") or file_data.get("api_key")

    missing = [name for name, value in (("url", url), ("api_key", api_key)) if not value]
    if missing:
        raise ValueError(
            "missing media-server config: %s (set $STASH_URL/$STASH_API_KEY, or "
            "provide them in %s — see config/server.example.json)"
            % (", ".join(missing), path))
    return {"url": url, "api_key": api_key}


def default_stashbox_path(env=None):
    return os.path.join(config_dir(env), "stashbox.json")


def load_stashbox(path=None, env=None):
    """{"url": ..., "api_key": ...} for a stash-box instance, or `None`.

    A stash-box read is what lets a refusal say more than "nothing scored
    well enough" (see `cronicled.stashbox`), but nothing in this project can
    run at all without it — a fresh install, or an operator who has not set
    one up, is a normal state, not a broken one. So this follows the SAME
    half of the rule `load_adapters` does, not `load_server`'s: absence
    returns `None` rather than raising, and the caller degrades to the
    plainer refusal wording instead of failing to start or breaking a scan.

    `$STASHBOX_URL`/`$STASHBOX_API_KEY` win over the file at `path` (default:
    `stashbox.json` inside `config_dir()`). `env` defaults to `os.environ` but
    is injectable so a test can supply one without mutating the real
    environment.

    Only `url` gates whether this is "configured" at all — a stash-box
    instance that permits anonymous reads has no key to give it, and
    treating a blank `api_key` as "not configured" would refuse a perfectly
    usable, keyless endpoint. `api_key` is carried through as `None` when
    nothing supplies it, exactly as `url` would be if this raised instead of
    returning `None` for it.
    """
    if env is None:
        env = os.environ
    if path is None:
        path = default_stashbox_path(env)

    file_data = {}
    if os.path.exists(path):
        with open(path) as fh:
            file_data = json.load(fh)

    url = env.get("STASHBOX_URL") or file_data.get("url")
    if not url:
        return None
    api_key = env.get("STASHBOX_API_KEY") or file_data.get("api_key")
    return {"url": url, "api_key": api_key}
