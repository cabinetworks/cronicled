"""Start the inbox.

The one entry point this package has. It constructs no scheduler and starts
no timer: nothing here scans, and no proposal is produced without a person
asking for one elsewhere. This serves what the store already holds.
"""

import argparse
import os

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
    stash = Stash(args.server, args.api_key)
    serve(rows=lambda: to_rows(store.items()),
          actions=Actions(store, stash),
          host=args.host, port=args.port)


if __name__ == "__main__":
    main()
