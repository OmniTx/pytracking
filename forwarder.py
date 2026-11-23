# SPDX-License-Identifier: Apache-2.0
"""Run one forwarder pass.

Started by cron every minute (plan.md line 4). It materialises deliveries,
claims what is due, sends it, and exits; there is no long-running process.
Runs with no arguments by default, so the hosting panel's "Execute python
script" box works with just the path.

Output goes to stdout because the panel shows stdout.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tracker.forwarder import (  # noqa: E402
    DEFAULT_BASE_DELAY,
    DEFAULT_BATCH,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY,
    Forwarder,
)
from tracker.store import MySQLStore  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run one forwarder pass.")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help="deliveries to claim this pass (default: %(default)s)")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                        help="attempts before dead-lettering (default: %(default)s)")
    parser.add_argument("--base-delay", type=int, default=DEFAULT_BASE_DELAY,
                        help="first retry delay in seconds (default: %(default)s)")
    parser.add_argument("--max-delay", type=int, default=DEFAULT_MAX_DELAY,
                        help="retry delay cap in seconds (default: %(default)s)")
    return parser.parse_args(argv)


def connect():
    import pymysql

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
        charset="utf8mb4",
    )


def main(argv=None):
    args = parse_args(argv)
    connection = connect()
    try:
        forwarder = Forwarder(
            MySQLStore(connection),
            batch=args.limit,
            max_attempts=args.max_attempts,
            base_delay=args.base_delay,
            max_delay=args.max_delay,
        )
        summary = forwarder.run_once()
    finally:
        connection.close()

    for key in ("materialised", "claimed", "delivered", "retried", "dead"):
        print("{0}: {1}".format(key, summary.get(key, 0)))
    for message in summary.get("errors", []):
        print("error: {0}".format(message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
