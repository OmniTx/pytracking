# SPDX-License-Identifier: Apache-2.0
"""Run a migration SQL file against a MySQL database.

Usage::

    python db/run_migration.py
    python db/run_migration.py --file db/migrations/001_init.sql

With no arguments the file defaults to ``001_init.sql`` next to this script,
so it can be run from a hosting panel that only accepts a script path.

Environment variables::

    DB_HOST=localhost
    DB_NAME=pytracking
    DB_USER=pytracking_user
    DB_PASSWORD=replace-me

Results are printed to stdout because hosting panels commonly surface only
stdout. No dependencies beyond PyMySQL.
"""

import argparse
import os
import sys

import pymysql

DEFAULT_SQL_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "migrations", "001_init.sql"
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a Phase-N migration SQL file."
    )
    parser.add_argument(
        "--file",
        dest="sql_file",
        default=DEFAULT_SQL_FILE,
        help="Path to the SQL migration file (default: 001_init.sql).",
    )
    return parser.parse_args(argv)


def run_migration(sql_file):
    """Execute *sql_file* inside a single transaction."""
    connection = None
    try:
        connection = pymysql.connect(
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
    except Exception as exc:
        connection.rollback()
        print(f"MIGRATION FAILED ({type(exc).__name__}): {exc}")
        raise
    finally:
        if connection.open:
            connection.close()


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
    try:
        run_migration(args.sql_file)
    except Exception:
        sys.exit(1)
