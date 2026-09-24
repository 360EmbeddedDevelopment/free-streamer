#!/usr/bin/env python3
"""Serve the stream control panel on the LAN.

    ./serve.py                     # http://<this-pi>.local:5000 from any phone on the network
    ./serve.py --port 5050         # somewhere else, for testing
    ./serve.py --host 127.0.0.1    # this Pi only

Streams come from streams.json; edit it and reload the page, no restart needed.
"""

from __future__ import annotations

import argparse
import sys

from webapp import catalog, create_app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="serve.py", description=__doc__.splitlines()[0])
    p.add_argument(
        "--host",
        default="0.0.0.0",
        help="address to bind (default: 0.0.0.0, i.e. anyone on the LAN)",
    )
    p.add_argument("--port", type=int, default=5000, help="port to listen on (default: 5000)")
    p.add_argument(
        "--streams",
        default=catalog.DEFAULT_PATH,
        help=f"stream catalog to read (default: {catalog.DEFAULT_PATH})",
    )
    args = p.parse_args(argv)

    cat = catalog.load(args.streams)
    if cat.error:
        # Not fatal: the page shows the error, and fixing the file needs no
        # restart. Say it once here so a systemd log makes the reason obvious.
        print(f"warning: {cat.error}", file=sys.stderr)
    else:
        print(f"{len(cat.streams)} stream(s) from {args.streams}", file=sys.stderr)

    app = create_app(args.streams)
    print(f"control panel on http://{args.host}:{args.port}", file=sys.stderr)
    # use_reloader=False is required, not a preference: the reloader forks a
    # second process, and each would have its own StreamManager and its own idea
    # of what is on the TV.
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
