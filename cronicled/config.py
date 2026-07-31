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


# Jobs renamed for what they cover rather than what they did at the time,
# keyed by the name an existing schedule file may still hold.
#
# Accepted for one release, and only these exact names. `cronicled.schedule.
# resolve` refuses an override naming a producer that does not exist, and that
# refusal is a START-UP failure — before there is a page on which to read it,
# so the symptom of a rename landing on a configured deployment is a crash
# loop rather than a message. This map is what closes that window.
#
# It translates names this project itself changed; it does not make an unknown
# name acceptable. Anything not listed here is handed on exactly as written, so
# `resolve` still refuses it — a typo must stay a start-up stack trace rather
# than becoming a job that quietly never runs.
RENAMED_JOBS = {
    "nightly-library-scan": "scene-scan",
    "performer-descriptions": "performer-scan",
    "tag-merge": "tag-scan",
}


def _refuse_duplicate_keys(pairs, path):
    """`json.load` hook: a repeated key is an error, not the last one winning.

    A file whose earlier half is discarded without a word is a file whose
    author believes something untrue about their own configuration. Observed
    on a real deployment: every job named twice, so three interval entries
    were dead and three passes shared one appointment, and nothing said so.

    This is an `object_pairs_hook`, so it fires for EVERY object in the file,
    at every depth — a job named twice at the top level and a setting named
    twice inside one job's own settings are the same mistake with the same
    consequence, and are refused by the same rule at the same moment.

    Both values are named, because which one is live is the whole question:
    a message saying only that the key repeats leaves the reader to work out
    which half of their file the parser threw away.
    """
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(
                "%s names %r twice, as %r and then %r. JSON keeps only the "
                "last, so the first is silently discarded — delete one."
                % (path, key, seen[key], value))
        seen[key] = value
    return seen


def _migrate_renamed_jobs(overrides, path):
    """`overrides` with any old job name replaced by its current one.

    Every translation is reported, once each. A migration nobody is told
    about is a file that keeps working until the release that stops accepting
    the old name, at which point it stops the process starting — the operator
    has to be told now, while there is still an easy edit to make.

    Two entries that end up naming the SAME job are asked whether they
    actually DISAGREE before either is refused, because those are two
    different files with two different right answers. The reachable shape is
    an operator part-way through the rename, whose file names a job by its
    old name and its new one at once — the two keys differ, so JSON sees
    nothing wrong and `_refuse_duplicate_keys` cannot see it either.

    * **Different settings** are refused, naming both, for the reason a
      duplicate key is: it is one job scheduled two ways, there is no way to
      tell which was meant, and keeping either by iteration order would leave
      the other doing nothing and say so nowhere.
    * **Identical settings** are agreement, not a conflict. Whichever entry
      were kept, the job runs on the same cadence, so nothing is silently
      discarded and the refusal's own justification does not hold. Refusing
      it would stop the process starting over a file that expresses one
      unambiguous intent — in exactly the half-renamed state `RENAMED_JOBS`
      exists to let a configured deployment start in. It is migrated and
      reported instead, naming every spelling so the operator still knows to
      delete one.

    The agreeing report names the written spellings SORTED, and replaces the
    ordinary rename warning rather than adding to it, so that what is printed
    is a function of the file's content and not of which spelling the
    operator happened to write first.
    """
    claimed = {}
    for written, settings in overrides.items():
        current = RENAMED_JOBS.get(written, written)
        claimed.setdefault(current, []).append((written, settings))

    migrated = {}
    for current, entries in claimed.items():
        if len(entries) > 1:
            first_settings = entries[0][1]
            if any(settings != first_settings for _, settings in entries[1:]):
                raise ValueError(
                    "%s schedules the job now called %r more than once, and "
                    "they disagree: %s. Those are one job under two names, so "
                    "one of them would silently do nothing — delete one."
                    % (path, current,
                       " and ".join("%r as %r" % pair for pair in entries)))
            print("WARNING: %s names the job now called %r more than once, as "
                  "%s, and they all ask for the same thing, so it has been "
                  "read once for this run. Nothing is being guessed at — "
                  "still delete all but one."
                  % (path, current,
                     " and ".join(repr(name) for name
                                  in sorted(written for written, _ in entries))))
            migrated[current] = first_settings
            continue
        written, settings = entries[0]
        if written != current:
            print("WARNING: %s names %r, which is now called %r, and has been "
                  "read as the new name for this run. Edit the file: the old "
                  "name is accepted for one release so that the rename does "
                  "not stop a configured deployment starting, and after that "
                  "an override naming it is refused at start-up."
                  % (path, written, current))
        migrated[current] = settings
    return migrated


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

    It also refuses a key written twice, and it does the ONE thing `resolve`
    cannot: translate a job's old name to its current one. Both are here
    rather than there because both are facts about the FILE — the text an
    operator typed — and `resolve` is only ever handed the mapping that text
    parsed to, by which point a repeated key has already been thrown away and
    an old name is indistinguishable from a typo. See `_refuse_duplicate_keys`
    and `RENAMED_JOBS`.
    """
    if path is None:
        path = default_schedule_path(env)
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        overrides = json.load(
            fh, object_pairs_hook=lambda pairs: _refuse_duplicate_keys(
                pairs, path))
    if not isinstance(overrides, dict):
        raise ValueError(
            "%s must hold a JSON object keyed by producer name, for example "
            '{"scene-scan": {"every": 3600}}, but it holds %s'
            % (path, type(overrides).__name__))
    return _migrate_renamed_jobs(overrides, path)


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
