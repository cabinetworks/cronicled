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


def default_schedule_path(env=None):
    return os.path.join(config_dir(env), "schedule.json")


def load_schedule(path=None, env=None):
    """Schedule overrides, as `{producer_name: {"every": …, "enabled": …}}`.

    Absence is a legitimate state — every producer already declares its own
    cadence, and an operator who is happy with it configures nothing — so
    this follows `load_adapters`'s half of the rule above and returns an
    empty mapping rather than raising.

    It validates almost nothing on purpose. `cronicled.schedule.resolve`
    already refuses an override naming a producer that does not exist, an
    unknown key, a cadence that is not a positive number and an `enabled`
    that is not a boolean, and it refuses them at the moment the schedule is
    wired up, which is the same moment this is read. A second validator here
    would be a second place for the two to disagree, and the one that reads
    the file is the one that would go stale.

    What it does refuse is a top-level value that is not an object, because
    `resolve` receives that as `dict(overrides)` and a JSON list or string
    would fail there as a `TypeError` or a name nobody wrote — a message
    about this file, naming this file, is what an operator can act on.
    """
    if path is None:
        path = default_schedule_path(env)
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        overrides = json.load(fh)
    if not isinstance(overrides, dict):
        raise ValueError(
            "%s must hold a JSON object keyed by producer name, for example "
            '{"nightly-library-scan": {"every": 3600}}, but it holds %s'
            % (path, type(overrides).__name__))
    return overrides


ZONE_ENV_VAR = "CRONICLED_ZONE"

# What a deployment that names no zone runs and reads in. UTC and not the
# host's zone, for the reason `cronicled.schedule.resolve` refuses to inherit
# the host's: it is a property of wherever this happens to be deployed, so a
# container would keep its appointments in one hour and the operator's laptop
# in another from the same configuration, both correct-looking in every log.
# UTC is a STATED zone that says the same thing everywhere, and an operator
# who wants their own hour names it once — see `load_zone`.
DEFAULT_ZONE = "UTC"


def load_zone(env=None):
    """The name of the ONE zone this deployment uses, as a string.

    It answers two questions with one setting, deliberately: the zone each
    unattended pass's stated time is read in, and the zone every timestamp on
    the page is shown in. Two settings would let a page say 3am while a pass
    ran at a different 3am, which is worse than either being wrong on its own —
    the page would be evidence FOR the schedule an operator was trying to
    check.

    A name, not a `tzinfo`: this reads configuration and does not decide
    whether the configuration is usable. `cronicled.schedule.check_zone` is
    the one rule that answers that, and it is the same rule an override's own
    `zone` goes through — see its docstring for why there must not be a
    second.

    Absence is a legitimate state, so this follows `load_adapters`'s half of
    the rule in this module's docstring and returns `DEFAULT_ZONE` rather than
    raising. That default is a real answer rather than an evasion: every pass
    keeps its appointment at 03:00 UTC and the page reads in UTC, which is
    exactly what this project did before the setting existed.

    A setting that is PRESENT and empty is not absence and is handed back as it
    was written, for `check_zone` to refuse. `or DEFAULT_ZONE` would have folded
    it into the default -- and that is the shape of mistake this project has
    already made once with `marker_tag`: an operator who set the variable meant
    to name a zone, and quietly giving them UTC would restore exactly the
    behaviour they were trying to change while looking like it worked.
    """
    if env is None:
        env = os.environ
    name = env.get(ZONE_ENV_VAR)
    return DEFAULT_ZONE if name is None else name


def default_scan_path(env=None):
    return os.path.join(config_dir(env), "scan.json")


MARKER_TAG_KEY = "marker_tag"


def load_marker_tag(path=None, env=None):
    """The name of the tag that marks a scene as ORGANIZED PROVISIONALLY, or
    `None` when the operator has not named one.

    A library can carry a tag an earlier tool left behind, recording that a
    scene's metadata was guessed — a date, a filename, sometimes a creator —
    and never checked against a catalogue. Such a scene is usually marked
    organized too, so a scan that pools only the unorganized set never looks
    at it again. Naming that tag here is what puts those files back in a
    scan's reach; see `cronicled.scan.ScanProducer` for what it then does
    with them, and for why the tag is read and never written.

    Absence is a legitimate state — most libraries carry no such tag, and a
    scan with none configured pools exactly what it always did — so this
    follows `load_adapters`'s half of the rule in this module's docstring and
    returns `None` rather than raising: an absent file, and a file that names
    no `marker_tag`, are both "nothing configured". A file that IS a config
    file for something else this scan may one day carry is not malformed for
    lacking this key.

    A key that is PRESENT and unusable raises, naming the file. That is the
    other half of the rule, and the distinction it draws is the whole reason
    this is not one `or None`: an empty string, a blank one, or a number is
    an operator who meant to name a tag, and it is falsy — folded into
    absence it would silently restore today's behaviour, which is the exact
    state the operator was trying to change and the one they cannot tell
    apart from success. The tag NAME is not otherwise inspected here; whether
    the server actually holds such a tag is a question only the server can
    answer, and it is asked at scan time (see `ScanProducer._pool`).

    `$CRONICLED_CONFIG_DIR` is honoured through `config_dir`, and `env` is
    injectable, exactly as every loader above.
    """
    if path is None:
        path = default_scan_path(env)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(
            "%s must hold a JSON object, for example {\"%s\": \"needs "
            "review\"}, but it holds %s"
            % (path, MARKER_TAG_KEY, type(payload).__name__))
    if MARKER_TAG_KEY not in payload:
        return None
    marker = payload[MARKER_TAG_KEY]
    if not isinstance(marker, str) or not marker.strip():
        raise ValueError(
            "%s sets %r to %r, which names no tag. Give it the exact name of "
            "the tag your library marks provisionally-organized scenes with, "
            "or remove the key — removing it is how a scan is told there is "
            "no such tag, and an empty one would quietly mean the same thing "
            "while looking like a setting."
            % (path, MARKER_TAG_KEY, marker))
    return marker


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
