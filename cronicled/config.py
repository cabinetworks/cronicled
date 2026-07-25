"""Media-server connection config: environment first, then a JSON file, and
never a compiled-in default. The operator's host and API key identify their own
machine, so a fallback baked into the source would leak it into a public repo —
every deployment must supply both explicitly, one way or the other.

This project's OWN config directory (where `server.json` and `adapters.json`
live) is a separate concern from the above, and is resolved by `config_dir`
below, shared with `cronicled.adapters.registry`. `$STASH_URL`/`$STASH_API_KEY`
deliberately stay their own thing rather than folding into that directory
scheme: they name the media server being managed, not this project, and an
operator may already have them set for a reason of their own.
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
