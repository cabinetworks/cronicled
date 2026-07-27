"""Start the inbox.

The one entry point this package has. It constructs no scheduler and starts
no timer: nothing here scans, and no proposal is produced without a person
asking for one elsewhere. This serves what the store already holds.

`--server` names a media server; it has no default because there is no safe
guess for it. Without one, the inbox still starts: a person can browse what a
scan already produced, and dismiss or mute proposals, with nothing here that
needs to reach a media server. Approve and Undo are the two actions that
write to one, so those two refuse with a clear message instead (see
`web.actions.Actions`) rather than the whole tool being unusable to someone
who only wants to look at what a scan produced before wiring up a server.
"""

import argparse
import os

from .config import CONFIG_DIR_ENV_VAR, config_dir
from .store import Store
from .stash import Stash
from .web.actions import Actions
from .web.app import DEFAULT_HOST, DEFAULT_PORT, serve
from .web.rows import to_rows


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
    serve(rows=lambda: to_rows(store.items()),
          actions=Actions(store, stash),
          host=args.host, port=args.port)


if __name__ == "__main__":
    main()
