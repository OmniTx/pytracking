# SPDX-License-Identifier: Apache-2.0
"""Run migration SQL files against a MySQL database.

Usage::

    python db/run_migration.py                 # every migration, in order
    python db/run_migration.py --file db/migrations/002_deliveries.sql

With no arguments it applies every ``*.sql`` in ``migrations/`` in filename
order, so a hosting panel that only accepts a script path applies the whole
schema. Every migration is idempotent (``CREATE TABLE IF NOT EXISTS``), which
makes re-running safe.

Environment variables::

    DB_HOST=localhost
    DB_NAME=pytracking
    DB_USER=pytracking_user
    DB_PASSWORD=replace-me

Results are printed to stdout because hosting panels commonly surface only
stdout. No dependencies beyond PyMySQL.
"""

import argparse
import glob
import os
import sys

import pymysql

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(SCRIPT_DIR, "migrations")


def all_migrations():
    """Return every migration file, in filename order."""
    return sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run migration SQL files (all by default)."
    )
    parser.add_argument(
        "--file",
        dest="sql_file",
        default=None,
        help="Run only this file (default: every file in migrations/).",
    )
    return parser.parse_args(argv)


def connect():
    """Open the MySQL connection, exiting with a message if it fails."""
    try:
        return pymysql.connect(
            host=os.environ["DB_HOST"],
            database=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            connect_timeout=5,
            autocommit=False,
        )
    except Exception as exc:
        print(f"FATAL: unable to connect to MySQL: {exc}")
        sys.exit(1)


def run_migration(sql_file, connection):
    """Execute *sql_file* on *connection*, committing on success."""
    with open(sql_file, encoding="utf-8") as fh:
        sql = fh.read()

    statements = _split_statements(sql)

    try:
        applied = 0
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
                applied += 1
        connection.commit()
        print(f"MIGRATION OK: applied {applied} statement(s) from {sql_file}")
        return applied
    except Exception as exc:
        connection.rollback()
        print(f"MIGRATION FAILED ({type(exc).__name__}) in {sql_file}: {exc}")
        raise


def _split_statements(sql):
    """Split a SQL file into executable statements.

    Drops ``--`` line comments and blank fragments, then splits on ``;``.
    """
    lines = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


if __name__ == "__main__":
    args = parse_args()
    files = [args.sql_file] if args.sql_file else all_migrations()
    if not files:
        print(f"No migration files found in {MIGRATIONS_DIR}")
        sys.exit(1)
    connection = connect()
    try:
        for path in files:
            run_migration(path, connection)
    except Exception:
        sys.exit(1)
    finally:
        if connection.open:
            connection.close()
