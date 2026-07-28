"""Start the inbox.

The one entry point this package has. It constructs no scheduler and starts
no timer: no scan runs on its own here, and no proposal is produced without a
person asking for one. A person CAN ask for one, from the page itself — the
`Scan` control posts to `/scan`, which starts a `JobRunner` job through
`web.actions.Actions.scan`, built from the same `cronicled.runscan.build_producer`
the CLI uses. That is the one place a scan starts; nothing decides on its own
that one is due.

`--server` names a media server; it has no default because there is no safe
guess for it. Without one, the inbox still starts: a person can browse what a
scan already produced, and dismiss or mute proposals, with nothing here that
needs to reach a media server. Approve and Undo are the two actions that
write to one. Scan never writes to a media server -- it only reads the
library and looks candidates up -- but it does need both a media server
and a configured site adapter to search against, so it refuses with its own
clear message when either is missing (see `web.actions.Actions`) rather
than the whole tool being unusable to someone who only wants to look at
what a scan already produced before wiring either one up.

The `JobRunner` built below outlives any one request: it is constructed once,
here, alongside `Store` and `Stash`, and lives for as long as this process
does — a scan a request started keeps running after that request's own
connection has closed. Like `Store` and `Stash`, it is not explicitly closed
on shutdown; that is unchanged from how this entry point already treated the
other two.
"""

import argparse
import os

from .config import CONFIG_DIR_ENV_VAR, config_dir
from .jobs import JobRunner
from .runscan import configured_adapters
from .store import Store
from .stash import Stash
from .web.actions import Actions
from .web.app import DEFAULT_HOST, DEFAULT_PORT, serve
from .web.rows import to_refusal_rows, to_rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cronicled")
    parser.add_argument("--db", default=os.environ.get(
        "CRONICLED_DB", "cronicled.sqlite3"))
    parser.add_argument("--server", default=os.environ.get("CRONICLED_SERVER"))
    parser.add_argument("--api-key", default=os.environ.get("CRONICLED_API_KEY"))
    # $CRONICLED_HOST/$CRONICLED_PORT give these two the same environment
    # default `--db` already has. DEFAULT_HOST stays 127.0.0.1 here -- the
    # container image is what overrides it, via ENV, not this default.
    parser.add_argument("--host", default=os.environ.get(
        "CRONICLED_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(
        os.environ.get("CRONICLED_PORT", DEFAULT_PORT)))
    parser.add_argument("--config-dir", default=os.environ.get(CONFIG_DIR_ENV_VAR))
    args = parser.parse_args(argv)

    # cronicled.config's loaders (config_dir/load_server/load_adapters) all
    # take an injectable `env` rather than reading os.environ directly, so a
    # CLI override can take effect without mutating the real environment --
    # mutating it here would leak into anything else running in this same
    # process, and outlive this one call. A copy is built, overridden only
    # when --config-dir/$CRONICLED_CONFIG_DIR resolved to something, and
    # passed explicitly wherever this project's own config directory is
    # resolved; os.environ itself is never written to.
    env = dict(os.environ)
    if args.config_dir is not None:
        env[CONFIG_DIR_ENV_VAR] = args.config_dir
    print("config directory: %s" % config_dir(env=env))

    store = Store(args.db)
    if args.server:
        stash = Stash(args.server, args.api_key)
    else:
        stash = None
        print("WARNING: no --server configured. Browsing, dismissing and "
              "muting still work; Approve and Undo will refuse until a "
              "media server is set (--server / --api-key, or "
              "$CRONICLED_SERVER / $CRONICLED_API_KEY).")
    try:
        # A fresh install with no adapters.json configured is a legitimate
        # state, not an error -- see cronicled.adapters.registry's module
        # docstring -- so this stays silent the same way `load_adapters`
        # itself does. `Actions.scan` gives the loud, specific refusal, and
        # only once someone actually presses Scan.
        # `env=env` and not the ambient environment: this is the seam where
        # --config-dir either reaches the adapters or silently does not.
        # Omitting it leaves the flag half-working -- the directory printed
        # above would be the one asked for, while the adapters came from
        # somewhere else -- and nothing would raise to say so.
        #
        # The WHOLE mapping, every configured adapter -- there is no single
        # "the" adapter any more (see `cronicled.runscan.build_producer`): a
        # scan searches every one of them.
        adapters = configured_adapters(env=env)
    except (ValueError, RuntimeError, KeyError):
        adapters = {}
    runner = JobRunner(store)
    actions = Actions(store, stash, runner=runner, adapters=adapters)
    serve(rows=lambda: to_rows(store.items()),
          muted=store.mutes,
          dismissed=lambda: to_rows(store.items(state="dismissed")),
          refused=lambda: to_refusal_rows(store.refusals()),
          superseded=lambda: to_rows(store.items(state="superseded")),
          actions=actions, scan_status=actions.scan_status,
          host=args.host, port=args.port)


if __name__ == "__main__":
    main()
