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

from .adapters.registry import get_adapter
from .jobs import JobRunner
from .runscan import configured_adapters
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
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

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
        adapter = get_adapter(None, configured_adapters())
    except (ValueError, RuntimeError, KeyError):
        adapter = None
    runner = JobRunner(store)
    actions = Actions(store, stash, runner=runner, adapter=adapter)
    serve(rows=lambda: to_rows(store.items()),
          actions=actions, scan_status=actions.scan_status,
          host=args.host, port=args.port)


if __name__ == "__main__":
    main()
